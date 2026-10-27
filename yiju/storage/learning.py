import json
from typing import TYPE_CHECKING

from fsrs import Card, Rating

from yiju.core.scheduling import scheduler
from yiju.core.strength import Parameters, review

from .database import ConflictError, parameter_set, settings, transaction
from .graph import refresh
from .sentences import words_of
from .words import strength

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime


def _targets(
    connection: sqlite3.Connection,
    card: sqlite3.Row,
    choice: Rating,
    marked: tuple[str, ...],
    parameters: Parameters,
) -> list[tuple[str, Rating, float, str]]:
    if card["word"] is not None:
        return [(str(card["word"]), choice, 1.0, "card")]
    words = tuple(dict.fromkeys(words_of(connection, card["sentence_id"])))
    if any(word not in words for word in marked):
        message = "Only words in this sentence can be marked."
        raise ValueError(message)
    targets = []
    for word in words:
        if word in marked:
            targets.append((word, Rating.Again, 1.0, "marked"))
        elif choice != Rating.Again:
            targets.append((word, choice, parameters.sentence_pass_weight, "sentence"))
        elif not marked:
            targets.append(
                (word, Rating.Again, parameters.sentence_fail_weight, "sentence")
            )
    return targets


def _studyable(connection: sqlite3.Connection, card: sqlite3.Row) -> bool:
    if card["word"] is not None:
        row = connection.execute(
            "SELECT state FROM words WHERE word = ?", (card["word"],)
        ).fetchone()
        return row is not None and bool(row["state"] != "locked")
    row = connection.execute(
        "SELECT state, hidden FROM sentences WHERE id = ?", (card["sentence_id"],)
    ).fetchone()
    return row is not None and bool(row["state"] == "unlocked" and not row["hidden"])


def review_card(
    connection: sqlite3.Connection,
    card_id: int,
    version: int,
    rating: int,
    now: datetime,
    marked: tuple[str, ...] = (),
) -> int:
    choice = Rating(rating)
    with transaction(connection):
        card = connection.execute(
            "SELECT * FROM cards WHERE id = ?", (card_id,)
        ).fetchone()
        if card is None or card["version"] != version or card["suspended"]:
            message = "This card has changed. Reload the study page before rating it."
            raise ConflictError(message)
        if card["due"] > now.isoformat():
            message = "This card is not due yet."
            raise ConflictError(message)
        if not _studyable(connection, card):
            message = "This card is not available to study."
            raise ConflictError(message)
        config = settings(connection)
        parameters = Parameters.from_settings(config)
        targets = _targets(connection, card, choice, tuple(marked), parameters)
        updated, _ = scheduler(float(config["retention"])).review_card(
            Card.from_json(card["scheduling"]), choice, now
        )
        cursor = connection.execute(
            "INSERT INTO reviews(card_id, rating, created, card_before, parameter_set) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                card_id,
                rating,
                now.isoformat(),
                json.dumps(dict(card)),
                parameter_set(connection, parameters),
            ),
        )
        review_id = int(cursor.lastrowid or 0)
        changed = []
        for word, word_rating, weight, role in targets:
            row = connection.execute(
                "SELECT * FROM words WHERE word = ?", (word,)
            ).fetchone()
            before = strength(row)
            after = review(before, word_rating, weight, now, parameters)
            state = "seen" if role == "card" else row["state"]
            memory = after.memory.to_json() if after.memory is not None else None
            connection.execute(
                "UPDATE words SET memory = ?, success_days = ?, last_success = ?, "
                "learned = ?, state = ?, version = version + 1 WHERE word = ?",
                (
                    memory,
                    after.success_days,
                    after.last_success,
                    int(after.learned),
                    state,
                    word,
                ),
            )
            connection.execute(
                "INSERT INTO review_words(review_id, word, role, rating, weight, "
                "recall_before, state_before, memory_before, memory_after, "
                "success_days_before, last_success_before, learned_before, "
                "learned_after) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    review_id,
                    word,
                    role,
                    int(word_rating),
                    weight,
                    before.recall(now),
                    row["state"],
                    row["memory"],
                    memory,
                    before.success_days,
                    before.last_success,
                    int(before.learned),
                    int(after.learned),
                ),
            )
            if after.learned != before.learned or state != row["state"]:
                changed.append(word)
        connection.execute(
            "UPDATE cards SET scheduling = ?, due = ?, reviews = reviews + 1, "
            "version = version + 1 WHERE id = ?",
            (updated.to_json(), updated.due.isoformat(), card_id),
        )
        if changed:
            refresh(connection, changed, review_id)
        return review_id


def undo_review(connection: sqlite3.Connection, review_id: int) -> None:
    with transaction(connection):
        latest = connection.execute(
            "SELECT * FROM reviews WHERE undone = 0 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if latest is None or latest["id"] != review_id:
            message = "Only the most recent review can be undone. Reload the page."
            raise ConflictError(message)
        previous = json.loads(latest["card_before"])
        connection.execute(
            "UPDATE cards SET scheduling = ?, due = ?, reviews = ?, "
            "version = version + 1 WHERE id = ?",
            (
                previous["scheduling"],
                previous["due"],
                previous["reviews"],
                latest["card_id"],
            ),
        )
        for change in connection.execute(
            "SELECT * FROM review_changes WHERE review_id = ?", (review_id,)
        ).fetchall():
            table, key = (
                ("words", "word") if change["kind"] == "word" else ("sentences", "id")
            )
            connection.execute(
                f"UPDATE {table} SET state = ?, version = version + 1 "  # noqa: S608
                f"WHERE {key} = ?",
                (change["state_before"], change["key"]),
            )
        for row in connection.execute(
            "SELECT * FROM review_words WHERE review_id = ?", (review_id,)
        ).fetchall():
            connection.execute(
                "UPDATE words SET memory = ?, success_days = ?, last_success = ?, "
                "learned = ?, state = ?, version = version + 1 WHERE word = ?",
                (
                    row["memory_before"],
                    row["success_days_before"],
                    row["last_success_before"],
                    row["learned_before"],
                    row["state_before"],
                    row["word"],
                ),
            )
        connection.execute("UPDATE reviews SET undone = 1 WHERE id = ?", (review_id,))
