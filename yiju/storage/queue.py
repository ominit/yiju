from collections import Counter
from datetime import UTC, datetime, timedelta
from random import SystemRandom
from typing import TYPE_CHECKING

from yiju.core.definitions import prompt_senses

from .database import settings
from .names import counts as name_counts
from .names import needs
from .pending import waiting
from .sentences import askable, statuses

if TYPE_CHECKING:
    import sqlite3

CARD_QUERY = (
    "SELECT c.*, w.readings, s.chinese, s.rules, "
    "s.pinyin AS sentence_pinyin, s.translation, s.version AS sentence_version, "
    "EXISTS (SELECT 1 FROM cards o WHERE o.id != c.id AND o.reviews > 0 "
    "AND (o.word = c.word OR o.sentence_id = c.sentence_id)) AS started "
    "FROM cards c LEFT JOIN words w ON w.word = c.word "
    "LEFT JOIN sentences s ON s.id = c.sentence_id "
)
STUDYABLE = (
    "WHERE c.suspended = 0 AND c.due <= ? "
    "AND ((c.word IS NOT NULL AND w.state != 'locked') "
    "OR (s.state = 'unlocked' AND s.hidden = 0)) "
)


def card_family(card: sqlite3.Row) -> str:
    return (
        f"sentence:{card['sentence_id']}"
        if card["sentence_id"] is not None
        else f"word:{card['word']}"
    )


def day_bounds(now: datetime) -> tuple[str, str]:
    today = now.astimezone(UTC).date()
    return today.isoformat(), (today + timedelta(days=1)).isoformat()


def introduced_today(connection: sqlite3.Connection, now: datetime) -> int:
    return int(
        connection.execute(
            "SELECT count(DISTINCT card_id) FROM reviews WHERE undone = 0 "
            "AND json_extract(card_before, '$.reviews') = 0 "
            "AND created >= ? AND created < ?",
            day_bounds(now),
        ).fetchone()[0]
    )


def _askable(card: sqlite3.Row) -> bool:
    if card["card_type"] != "production":
        return True
    if card["sentence_id"] is not None:
        return askable(card["translation"])
    return bool(prompt_senses(card["readings"]))


def study_queue(
    connection: sqlite3.Connection, now: datetime, *, card_id: int | None = None
) -> list[sqlite3.Row]:
    now = now.astimezone(UTC)
    available = name_counts(connection)
    rows: list[sqlite3.Row] = [
        row
        for row in connection.execute(
            CARD_QUERY
            + STUDYABLE
            + ("AND c.id = ? " if card_id is not None else "")
            + "ORDER BY c.due, c.id",
            (now.isoformat(), card_id) if card_id is not None else (now.isoformat(),),
        )
        if _askable(row)
        and (
            row["sentence_id"] is None
            or needs(row["chinese"], row["rules"], available) is None
        )
    ]
    random = SystemRandom()
    reviews = [row for row in rows if row["reviews"]]
    started = [row for row in rows if not row["reviews"] and row["started"]]
    fresh = [row for row in rows if not row["reviews"] and not row["started"]]
    random.shuffle(started)
    random.shuffle(fresh)
    remaining = max(
        0, int(settings(connection)["new_per_day"]) - introduced_today(connection, now)
    )
    return reviews + (started + fresh)[:remaining]


def counts(queue: list[sqlite3.Row]) -> dict[str, int]:
    new = sum(1 for row in queue if not row["reviews"])
    return {"reviews": len(queue) - new, "new": new}


def overview(connection: sqlite3.Connection, now: datetime) -> dict[str, int]:
    queue = study_queue(connection, now)
    words = dict(
        connection.execute(
            "SELECT CASE WHEN state = 'seen' THEN "
            "CASE WHEN learned THEN 'learned' ELSE 'learning' END "
            "ELSE state END AS status, count(*) FROM words GROUP BY status"
        ).fetchall()
    )
    hidden = {
        row[0] for row in connection.execute("SELECT id FROM sentences WHERE hidden")
    }
    sentences = Counter(
        value for key, value in statuses(connection).items() if key not in hidden
    )
    return {
        "words": sum(words.values()),
        "learned": words.get("learned", 0),
        "learning": words.get("learning", 0),
        "unlocked": words.get("unlocked", 0),
        "locked": words.get("locked", 0),
        "sentences": sum(sentences.values()),
        "sentences_learned": sentences["learned"],
        "sentences_learning": sentences["learning"],
        "sentences_unlocked": sentences["unlocked"],
        "sentences_eligible": sentences["eligible"],
        "sentences_locked": sentences["locked"],
        "due": counts(queue)["reviews"],
        "new": counts(queue)["new"],
        "today": connection.execute(
            "SELECT count(*) FROM reviews WHERE undone = 0 AND created >= ?",
            (day_bounds(now)[0],),
        ).fetchone()[0],
        "pending": waiting(connection),
    }
