import json
import sqlite3
from collections.abc import Iterable

SENTENCE_COUNTS = (
    "SELECT s.id, s.state, s.hidden, "
    "count(DISTINCT CASE WHEN w.learned = 0 THEN w.word END) AS unlearned, "
    "count(DISTINCT CASE WHEN w.state != 'seen' THEN w.word END) AS unseen "
    "FROM sentences s JOIN sentence_words sw ON sw.sentence_id = s.id "
    "JOIN words w ON w.word = sw.word WHERE ? IS NULL OR s.id IN "
    "(SELECT sentence_id FROM sentence_words "
    "WHERE word IN (SELECT value FROM json_each(?))) GROUP BY s.id"
)
MAX_UNLEARNED = 2


def sentence_state(state: str, unlearned: int, unseen: int, *, hidden: bool) -> str:
    if state == "unlocked" or not unseen:
        return "unlocked"
    if hidden or unlearned > MAX_UNLEARNED:
        return "locked"
    return "eligible"


def _record(
    connection: sqlite3.Connection,
    review_id: int | None,
    kind: str,
    key: str,
    before: str,
) -> None:
    if review_id is not None:
        connection.execute(
            "INSERT OR IGNORE INTO review_changes(review_id, kind, key, state_before) "
            "VALUES (?, ?, ?, ?)",
            (review_id, kind, key, before),
        )


def refresh(
    connection: sqlite3.Connection,
    words: Iterable[str] | None = None,
    review_id: int | None = None,
) -> None:
    affected = None if words is None else json.dumps(sorted(set(words)))
    sentences = connection.execute(SENTENCE_COUNTS, (affected, affected)).fetchall()
    for row in sentences:
        state = sentence_state(
            row["state"], row["unlearned"], row["unseen"], hidden=bool(row["hidden"])
        )
        if state != row["state"]:
            _record(connection, review_id, "sentence", str(row["id"]), row["state"])
            connection.execute(
                "UPDATE sentences SET state = ?, version = version + 1 WHERE id = ?",
                (state, row["id"]),
            )
    candidates = connection.execute(
        "SELECT w.word, w.state, w.manual OR EXISTS (SELECT 1 FROM sentence_words sw "
        "JOIN sentences s ON s.id = sw.sentence_id WHERE sw.word = w.word "
        "AND s.state = 'eligible' AND s.hidden = 0) AS supported "
        "FROM words w WHERE w.state != 'seen' AND (? IS NULL "
        "OR w.word IN (SELECT value FROM json_each(?)) "
        "OR w.word IN (SELECT sw.word FROM sentence_words sw "
        "WHERE sw.sentence_id IN (SELECT value FROM json_each(?))))",
        (
            affected,
            affected,
            json.dumps([row["id"] for row in sentences]),
        ),
    ).fetchall()
    for row in candidates:
        state = "unlocked" if row["supported"] else "locked"
        if state != row["state"]:
            _record(connection, review_id, "word", row["word"], row["state"])
            connection.execute(
                "UPDATE words SET state = ?, version = version + 1 WHERE word = ?",
                (state, row["word"]),
            )
