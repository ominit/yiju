from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from math import floor

from fsrs import Card

from .scheduling import is_known, scheduler

COMPLETE = 100


@dataclass(frozen=True, slots=True)
class LearningProgress:
    percent: int = 0
    days: int = 0
    stability: float = 0
    recall: int | None = None
    started: bool = False

    @property
    def state(self) -> str:
        if self.percent == COMPLETE:
            return "learned"
        return "learning" if self.started else "new"


def learning_progress(
    scheduling: str | None,
    days: int,
    config: Mapping[str, int | float | bool],
    now: datetime,
) -> LearningProgress:
    if scheduling is None:
        return LearningProgress()
    card = Card.from_json(scheduling)
    stability = card.stability or 0
    recall = scheduler().get_card_retrievability(card, now)
    known = is_known(
        card,
        days,
        now,
        minimum_days=int(config["known_days"]),
        minimum_stability=float(config["known_stability"]),
        minimum_retrievability=float(config["known_retrievability"]),
    )
    fraction = min(
        days / int(config["known_days"]),
        stability / float(config["known_stability"]),
        recall / float(config["known_retrievability"]),
    )
    return LearningProgress(
        100 if known else min(99, max(0, floor(fraction * 100))),
        days,
        round(stability, 1),
        round(recall * 100),
        started=True,
    )
