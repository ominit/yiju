import json
import sqlite3
from datetime import datetime

from fsrs import Card, Rating, Scheduler

from yiju.core.scheduling import is_known, new_card, scheduler

from .cards import identity
from .database import settings, transaction, vocabulary_words


class ConflictError(ValueError):
    pass


def known_words(connection: sqlite3.Connection, now: datetime) -> frozenset[str]:
    config = settings(connection)
    return frozenset(
        str(row["word"])
        for row in connection.execute("SELECT * FROM knowledge")
        if is_known(
            Card.from_json(row["scheduling"]),
            row["success_days"],
            now,
            minimum_days=int(config["known_days"]),
            minimum_stability=float(config["known_stability"]),
            minimum_retrievability=float(config["known_retrievability"]),
        )
    )


def known_sentences(connection: sqlite3.Connection, now: datetime) -> frozenset[str]:
    config = settings(connection)
    rows = connection.execute(
        "WITH failures AS (SELECT card_id, max(id) AS last_failure FROM reviews "
        "WHERE undone = 0 AND rating = 1 GROUP BY card_id) "
        "SELECT c.sentence_id, c.scheduling, "
        "count(DISTINCT substr(r.created, 1, 10)) AS success_days "
        "FROM cards c JOIN sentences s ON s.id = c.sentence_id "
        "LEFT JOIN failures f ON f.card_id = c.id "
        "LEFT JOIN reviews r ON r.card_id = c.id AND r.undone = 0 "
        "AND r.rating IN (3, 4) AND r.id > coalesce(f.last_failure, 0) "
        "WHERE c.suspended = 0 AND c.reviews > 0 AND s.status = 'accepted' "
        "GROUP BY c.id HAVING success_days >= ?",
        (int(config["known_days"]),),
    )
    return frozenset(
        str(row["sentence_id"])
        for row in rows
        if is_known(
            Card.from_json(row["scheduling"]),
            row["success_days"],
            now,
            minimum_days=int(config["known_days"]),
            minimum_stability=float(config["known_stability"]),
            minimum_retrievability=float(config["known_retrievability"]),
        )
    )


def _review_words(
    connection: sqlite3.Connection, card: sqlite3.Row, now: datetime
) -> tuple[str, ...]:
    if card["sentence_id"] is None:
        return (str(card["word"]),)
    sentence = connection.execute(
        "SELECT tokens FROM sentences WHERE id = ?", (card["sentence_id"],)
    ).fetchone()
    words = tuple(dict.fromkeys(str(word) for word in json.loads(sentence["tokens"])))
    vocabulary = vocabulary_words(connection)
    if not words or any(word not in vocabulary for word in words):
        message = "This sentence contains words outside your vocabulary."
        raise ConflictError(message)
    if not card["reviews"] and known_words(connection, now).isdisjoint(words):
        message = "This sentence needs at least one known word before studying it."
        raise ConflictError(message)
    return words


def _review_word(
    connection: sqlite3.Connection,
    word: str,
    choice: Rating,
    now: datetime,
    engine: Scheduler,
) -> str | None:
    previous = connection.execute(
        "SELECT * FROM knowledge WHERE word = ?", (word,)
    ).fetchone()
    word_card = (
        Card.from_json(previous["scheduling"])
        if previous is not None
        else new_card(identity("word", word), now)
    )
    word_card, _ = engine.review_card(word_card, choice, now)
    days = previous["success_days"] if previous is not None else 0
    last_success = previous["last_success"] if previous is not None else None
    if choice == Rating.Again:
        days = 0
        last_success = None
    elif (
        choice in (Rating.Good, Rating.Easy) and last_success != now.date().isoformat()
    ):
        days += 1
        last_success = now.date().isoformat()
    connection.execute(
        "INSERT INTO knowledge(word, scheduling, success_days, last_success) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(word) DO UPDATE SET "
        "scheduling = excluded.scheduling, success_days = excluded.success_days, "
        "last_success = excluded.last_success",
        (word, word_card.to_json(), days, last_success),
    )
    return json.dumps(dict(previous)) if previous is not None else None


def review_card(
    connection: sqlite3.Connection,
    card_id: str,
    version: int,
    rating: int,
    now: datetime,
) -> None:
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
        engine = scheduler(float(settings(connection)["retention"]))
        updated, _ = engine.review_card(Card.from_json(card["scheduling"]), choice, now)
        words = _review_words(connection, card, now)
        cursor = connection.execute(
            "INSERT INTO reviews(card_id, word, rating, created, card_before) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                card_id,
                card["word"],
                rating,
                now.isoformat(),
                json.dumps(dict(card)),
            ),
        )
        for word in words:
            before = _review_word(connection, word, choice, now, engine)
            connection.execute(
                "INSERT INTO review_words(review_id, word, knowledge_before) "
                "VALUES (?, ?, ?)",
                (cursor.lastrowid, word, before),
            )
        connection.execute(
            "UPDATE cards SET scheduling = ?, due = ?, reviews = reviews + 1, "
            "version = version + 1 WHERE id = ?",
            (updated.to_json(), updated.due.isoformat(), card_id),
        )


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
            "UPDATE cards SET scheduling = ?, due = ?, reviews = ?, version = "
            "version + 1 WHERE id = ?",
            (
                previous["scheduling"],
                previous["due"],
                previous["reviews"],
                latest["card_id"],
            ),
        )
        for row in connection.execute(
            "SELECT word, knowledge_before FROM review_words WHERE review_id = ?",
            (review_id,),
        ).fetchall():
            if row["knowledge_before"] is None:
                connection.execute(
                    "DELETE FROM knowledge WHERE word = ?", (row["word"],)
                )
            else:
                knowledge = json.loads(row["knowledge_before"])
                connection.execute(
                    "UPDATE knowledge SET scheduling = ?, success_days = ?, "
                    "last_success = ? WHERE word = ?",
                    (
                        knowledge["scheduling"],
                        knowledge["success_days"],
                        knowledge["last_success"],
                        row["word"],
                    ),
                )
        connection.execute("UPDATE reviews SET undone = 1 WHERE id = ?", (review_id,))
