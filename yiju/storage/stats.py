from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from fsrs import Rating

from yiju.core.scheduling import local_day

if TYPE_CHECKING:
    import sqlite3


def activity(connection: sqlite3.Connection, now: datetime) -> dict[str, object]:
    start = now.date() - timedelta(days=29)
    bounds = (local_day(now, -29).isoformat(), local_day(now, 1).isoformat())
    rows = connection.execute(
        "SELECT created, rating, json_extract(card_before, '$.reviews') > 0 "
        "AS scheduled FROM reviews WHERE undone = 0 AND created >= ? AND created < ?",
        bounds,
    ).fetchall()
    by_day: dict[str, dict[int, int]] = {}
    total = 0
    recalled = 0
    scheduled = 0
    for row in rows:
        day = datetime.fromisoformat(row["created"]).astimezone(now.tzinfo).date()
        counts = by_day.setdefault(day.isoformat(), {})
        counts[row["rating"]] = counts.get(row["rating"], 0) + 1
        total += 1
        scheduled += row["scheduled"]
        if row["rating"] >= Rating.Hard:
            recalled += row["scheduled"]
    introduced = connection.execute(
        "SELECT count(DISTINCT w.word) FROM review_words w "
        "JOIN reviews r ON r.id = w.review_id WHERE r.undone = 0 "
        "AND w.role = 'card' AND w.state_before != 'seen' "
        "AND r.created >= ? AND r.created < ?",
        bounds,
    ).fetchone()[0]
    bars = []
    maximum = max((sum(counts.values()) for counts in by_day.values()), default=1)
    for offset in range(30):
        day = start + timedelta(days=offset)
        counts = by_day.get(day.isoformat(), {})
        bottom = 0.0
        segments = []
        for rating in range(1, 5):
            height = counts.get(rating, 0) / maximum * 120
            segments.append(
                {"rating": rating, "y": 140 - bottom - height, "height": height}
            )
            bottom += height
        bars.append(
            {
                "date": day.isoformat(),
                "day": f"{day:%b} {day.day}",
                "total": sum(counts.values()),
                "segments": segments,
            }
        )
    return {
        "reviews": total,
        "recall": round(recalled / scheduled * 100) if scheduled else None,
        "study_days": len(by_day),
        "introduced": introduced,
        "rate": round(introduced / len(by_day), 1) if by_day else 0,
        "bars": bars,
        "maximum": maximum,
    }
