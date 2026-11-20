from collections import Counter
from datetime import UTC, datetime
from random import SystemRandom
from typing import TYPE_CHECKING, TypedDict

from yiju.core.definitions import prompt_senses
from yiju.core.scheduling import local_day

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
    "WHERE c.suspended = 0 "
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
    return local_day(now).isoformat(), local_day(now, 1).isoformat()


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


def _studyable(
    connection: sqlite3.Connection, condition: str, parameters: tuple[object, ...]
) -> list[sqlite3.Row]:
    available = name_counts(connection)
    return [
        row
        for row in connection.execute(
            CARD_QUERY + STUDYABLE + condition + "ORDER BY c.due, c.id", parameters
        )
        if _askable(row)
        and (
            row["sentence_id"] is None
            or needs(row["chinese"], row["rules"], available) is None
        )
    ]


def study_queue(
    connection: sqlite3.Connection, now: datetime, *, card_id: int | None = None
) -> list[sqlite3.Row]:
    moment = now.astimezone(UTC).isoformat()
    rows = _studyable(
        connection,
        "AND c.due <= ? " + ("AND c.id = ? " if card_id is not None else ""),
        (moment, card_id) if card_id is not None else (moment,),
    )
    random = SystemRandom()
    reviews = [row for row in rows if row["reviews"]]
    started = [row for row in rows if not row["reviews"] and row["started"]]
    fresh = [row for row in rows if not row["reviews"] and not row["started"]]
    sentences = [row for row in fresh if row["sentence_id"] is not None]
    words = [row for row in fresh if row["sentence_id"] is None]
    for group in (started, sentences, words):
        random.shuffle(group)
    remaining = max(
        0, int(settings(connection)["new_per_day"]) - introduced_today(connection, now)
    )
    return reviews + (started + sentences + words)[:remaining]


class Upcoming(TypedDict):
    times: list[str]
    later_today: int
    day_end: str
    new_next_day: int


def upcoming(connection: sqlite3.Connection, now: datetime) -> Upcoming:
    end = day_bounds(now)[1]
    times = [
        row["due"]
        for row in _studyable(
            connection,
            "AND c.reviews > 0 AND c.due > ? ",
            (now.astimezone(UTC).isoformat(),),
        )
    ]
    return {
        "times": times,
        "later_today": sum(1 for time in times if time < end),
        "day_end": end,
        "new_next_day": min(
            int(settings(connection)["new_per_day"]),
            len(_studyable(connection, "AND c.reviews = 0 AND c.due < ? ", (end,))),
        ),
    }


def counts(queue: list[sqlite3.Row]) -> dict[str, int]:
    new = sum(1 for row in queue if not row["reviews"])
    return {"reviews": len(queue) - new, "new": new}


def reviewed_today(connection: sqlite3.Connection, now: datetime) -> int:
    return int(
        connection.execute(
            "SELECT count(*) FROM reviews WHERE undone = 0 AND created >= ?",
            (day_bounds(now)[0],),
        ).fetchone()[0]
    )


def overview(connection: sqlite3.Connection, now: datetime) -> dict[str, int]:
    queue = study_queue(connection, now)
    words = dict(
        connection.execute(
            "SELECT CASE WHEN state = 'seen' THEN "
            "CASE WHEN learned THEN 'learned' WHEN familiar THEN 'familiar' "
            "ELSE 'learning' END "
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
        "familiar": words.get("familiar", 0),
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
        "today": reviewed_today(connection, now),
        "pending": waiting(connection),
    }
