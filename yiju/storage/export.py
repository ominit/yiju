import json
import sqlite3
from datetime import datetime

from yiju.core.definitions import readings_payload, summary
from yiju.core.dictionary import decode, readings_pinyin

from .sentences import statuses, words_of
from .words import status, strength

WORD_STATUSES = ("locked", "unlocked", "learning", "learned")
SENTENCE_STATUSES = ("locked", "eligible", "unlocked", "learning", "learned")


def _sentences(connection: sqlite3.Connection, word: str) -> list[dict[str, object]]:
    status_of = statuses(connection)
    return [
        {
            "id": row["id"],
            "chinese": row["chinese"],
            "translation": row["translation"],
            "status": status_of[row["id"]],
            "skipped": bool(row["hidden"]),
        }
        for row in connection.execute(
            "SELECT * FROM sentences WHERE id IN "
            "(SELECT sentence_id FROM sentence_words WHERE word = ?) ORDER BY id",
            (word,),
        )
    ]


def word_info(
    connection: sqlite3.Connection, word: str, now: datetime
) -> dict[str, object] | None:
    row = connection.execute("SELECT * FROM words WHERE word = ?", (word,)).fetchone()
    if row is None:
        return None
    current = strength(row)
    recall = current.recall(now)
    flag = connection.execute("SELECT * FROM flags WHERE word = ?", (word,)).fetchone()
    return {
        "word": word,
        "status": status(row),
        "manual_unlock": bool(row["manual"]),
        "source": row["source"],
        "readings": readings_payload(row["readings"]),
        "strength": {
            "stability_days": current.stability,
            "difficulty": current.memory.difficulty if current.memory else None,
            "success_days": current.success_days,
            "recall_now": recall,
        },
        "flag": {"note": flag["note"], "created": flag["created"]} if flag else None,
        "cards": [
            {
                "type": card["card_type"],
                "reviews": card["reviews"],
                "due": card["due"],
                "paused": bool(card["suspended"]),
            }
            for card in connection.execute(
                "SELECT * FROM cards WHERE word = ? ORDER BY id", (word,)
            )
        ],
        "sentences": _sentences(connection, word),
    }


def list_words(
    connection: sqlite3.Connection,
    *,
    word_status: str | None = None,
    flagged: bool = False,
    backlog: bool = False,
) -> list[dict[str, object]]:
    rows = connection.execute(
        "SELECT w.*, EXISTS (SELECT 1 FROM flags f WHERE f.word = w.word) AS flagged, "
        "NOT EXISTS (SELECT 1 FROM sentence_words sw JOIN sentences s "
        "ON s.id = sw.sentence_id WHERE sw.word = w.word AND s.hidden = 0) AS backlog "
        "FROM words w ORDER BY w.rowid"
    ).fetchall()
    return [
        {
            "word": row["word"],
            "pinyin": readings_pinyin(decode(row["readings"])),
            "meaning": summary(row["readings"]),
            "status": status(row),
            "flagged": bool(row["flagged"]),
            "in_sentences": not row["backlog"],
        }
        for row in rows
        if (word_status is None or status(row) == word_status)
        and (not flagged or row["flagged"])
        and (not backlog or row["backlog"])
    ]


def list_sentences(
    connection: sqlite3.Connection, sentence_status: str | None = None
) -> list[dict[str, object]]:
    status_of = statuses(connection)
    return [
        {
            "id": row["id"],
            "chinese": row["chinese"],
            "pinyin": row["pinyin"],
            "translation": row["translation"],
            "status": status_of[row["id"]],
            "skipped": bool(row["hidden"]),
            "words": list(words_of(connection, row["id"])),
        }
        for row in connection.execute("SELECT * FROM sentences ORDER BY id")
        if sentence_status is None or status_of[row["id"]] == sentence_status
    ]


def flagged_words(connection: sqlite3.Connection) -> list[dict[str, object]]:
    return [
        {
            "word": row["word"],
            "note": row["note"],
            "created": row["created"],
            "updated": row["updated"],
            "source": row["source"],
            "readings": readings_payload(row["readings"]),
            "sentences": _sentences(connection, row["word"]),
        }
        for row in connection.execute(
            "SELECT f.*, w.readings, w.source FROM flags f "
            "JOIN words w ON w.word = f.word ORDER BY f.created, f.word"
        )
    ]


def review_log(
    connection: sqlite3.Connection, since: str | None = None
) -> list[dict[str, object]]:
    return [
        {
            "review": row["review_id"],
            "created": row["created"],
            "card_rating": row["card_rating"],
            "card_type": row["card_type"],
            "card_kind": "sentence" if row["sentence_id"] is not None else "word",
            "sentence": row["sentence_id"],
            "word": row["word"],
            "role": row["role"],
            "rating_applied": row["rating"],
            "weight": row["weight"],
            "recall_before": row["recall_before"],
            "state_before": row["state_before"],
            "memory_before": json.loads(row["memory_before"])
            if row["memory_before"]
            else None,
            "memory_after": json.loads(row["memory_after"]),
            "success_days_before": row["success_days_before"],
            "learned_before": bool(row["learned_before"]),
            "learned_after": bool(row["learned_after"]),
            "parameters": json.loads(row["parameters"]),
        }
        for row in connection.execute(
            "SELECT rw.*, r.created, r.rating AS card_rating, c.card_type, "
            "c.sentence_id, p.parameters FROM review_words rw "
            "JOIN reviews r ON r.id = rw.review_id JOIN cards c ON c.id = r.card_id "
            "JOIN parameter_sets p ON p.id = r.parameter_set "
            "WHERE r.undone = 0 AND (? IS NULL OR r.created >= ?) "
            "ORDER BY r.id, rw.word",
            (since, since),
        )
    ]
