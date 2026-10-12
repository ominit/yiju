from datetime import UTC, datetime

from fsrs import Card, Rating, Scheduler, State


def now_utc() -> datetime:
    return datetime.now(UTC)


def scheduler(retention: float = 0.9) -> Scheduler:
    return Scheduler(desired_retention=retention, enable_fuzzing=False)


def new_card(identity: str, now: datetime) -> Card:
    return Card(card_id=int(identity[:12], 16), due=now)


def is_known(
    card: Card,
    success_days: int,
    now: datetime,
    *,
    minimum_days: int = 3,
    minimum_stability: float = 7,
    minimum_retrievability: float = 0.8,
) -> bool:
    return (
        card.state == State.Review
        and success_days >= minimum_days
        and card.stability is not None
        and card.stability >= minimum_stability
        and scheduler().get_card_retrievability(card, now) >= minimum_retrievability
    )


SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400
DAYS_PER_MONTH = 30
DAYS_PER_YEAR = 365


def interval_label(seconds: float) -> str:
    if seconds < SECONDS_PER_HOUR:
        return f"{max(1, round(seconds / 60))}m"
    if seconds < SECONDS_PER_DAY:
        return f"{round(seconds / 3600)}h"
    days = max(1, round(seconds / SECONDS_PER_DAY))
    if days < DAYS_PER_MONTH:
        return f"{days}d"
    if days < DAYS_PER_YEAR:
        return f"{round(days / DAYS_PER_MONTH, 1):g}mo"
    return f"{round(days / DAYS_PER_YEAR, 1):g}y"


def intervals(card: Card, now: datetime, retention: float) -> dict[int, str]:
    engine = scheduler(retention)
    return {
        rating.value: interval_label(
            (engine.review_card(card, rating, now)[0].due - now).total_seconds(),
        )
        for rating in Rating
    }
