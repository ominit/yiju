import json
import sqlite3
from datetime import datetime
from typing import cast

from yiju.core.selection import analyze

from .database import settings
from .learning import known_words

CARD_QUERY = (
    "SELECT c.*, CASE WHEN c.sentence_id IS NULL THEN "
    "(SELECT group_concat(DISTINCT e2.pinyin) FROM entries e2 "
    "WHERE e2.word = c.word) ELSE e.pinyin END AS word_pinyin, "
    "CASE WHEN c.sentence_id IS NULL THEN "
    "(SELECT group_concat(e2.definition, char(10) || char(10)) FROM entries e2 "
    "WHERE e2.word = c.word) ELSE e.definition END AS definition, "
    "s.chinese, s.pinyin AS sentence_pinyin, s.translation, s.tokens "
    "FROM cards c JOIN entries e ON e.id = c.entry_id "
    "LEFT JOIN sentences s ON s.id = c.sentence_id "
)


def study_queue(connection: sqlite3.Connection, now: datetime) -> list[sqlite3.Row]:
    config = settings(connection)
    known = known_words(connection, now)
    started = connection.execute(
        "SELECT count(*) FROM reviews WHERE undone = 0 AND created >= ? "
        "AND json_extract(card_before, '$.reviews') = 0",
        (now.date().isoformat(),),
    ).fetchone()[0]
    remaining = max(0, int(config["new_per_day"]) - started)
    due = cast(
        "list[sqlite3.Row]",
        connection.execute(
            CARD_QUERY + "WHERE c.suspended = 0 AND c.due <= ? "
            "ORDER BY (c.reviews = 0), c.due, c.id",
            (now.isoformat(),),
        ).fetchall(),
    )
    reviews = [row for row in due if row["reviews"] > 0]
    new_sentences: list[tuple[float, sqlite3.Row]] = []
    new_words = []
    for row in due:
        if row["reviews"]:
            continue
        if row["sentence_id"] is None:
            if row["word"] not in known:
                new_words.append(row)
        else:
            analysis = analyze(
                tuple(json.loads(row["tokens"])),
                known,
                row["word"],
                int(config["max_unknown"]),
                include_zero=bool(config["include_zero"]),
            )
            if analysis.eligible:
                new_sentences.append((analysis.score, row))
    new_words.sort(key=lambda row: (len(row["word"]), row["word"], row["id"]))
    new_sentences.sort(key=lambda item: (-item[0], item[1]["id"]))
    return reviews + ([row for _, row in new_sentences] + new_words)[:remaining]


def overview(connection: sqlite3.Connection, now: datetime) -> dict[str, int]:
    queue = study_queue(connection, now)
    return {
        "words": connection.execute(
            "SELECT count(DISTINCT word) FROM entries"
        ).fetchone()[0],
        "known": len(known_words(connection, now)),
        "due": sum(row["reviews"] > 0 for row in queue),
        "new": sum(row["reviews"] == 0 for row in queue),
        "pending": connection.execute(
            "SELECT count(*) FROM sentences WHERE status = 'pending'"
        ).fetchone()[0],
        "today": connection.execute(
            "SELECT count(*) FROM reviews WHERE undone = 0 AND created >= ?",
            (now.date().isoformat(),),
        ).fetchone()[0],
    }
