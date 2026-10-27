import json
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING

from fsrs import Card, Rating, Scheduler, State

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

MEMORY = Scheduler(enable_fuzzing=False, learning_steps=(), relearning_steps=())


@dataclass(frozen=True, slots=True)
class Parameters:
    learned_stability: float = 7.0
    unlearned_stability: float = 3.0
    learned_days: int = 3
    sentence_pass_weight: float = 0.2
    sentence_fail_weight: float = 0.25

    @classmethod
    def from_settings(cls, values: Mapping[str, object]) -> Parameters:
        return cls(
            **{
                field.name: type(getattr(cls(), field.name))(values[field.name])
                for field in fields(cls)
                if field.name in values
            }
        )

    def values(self) -> dict[str, int | float]:
        return {
            "learned_stability": self.learned_stability,
            "unlearned_stability": self.unlearned_stability,
            "learned_days": self.learned_days,
            "sentence_pass_weight": self.sentence_pass_weight,
            "sentence_fail_weight": self.sentence_fail_weight,
        }

    def to_json(self) -> str:
        return json.dumps(self.values(), sort_keys=True)


@dataclass(frozen=True, slots=True)
class Strength:
    memory: Card | None
    success_days: int
    last_success: str | None
    learned: bool

    def recall(self, now: datetime) -> float | None:
        if self.memory is None:
            return None
        return MEMORY.get_card_retrievability(self.memory, now)

    @property
    def stability(self) -> float:
        return (self.memory.stability or 0.0) if self.memory is not None else 0.0


def _blend(memory: Card, full: Card, weight: float, now: datetime) -> Card:
    stability = memory.stability or 0.0
    difficulty = memory.difficulty or 0.0
    return Card(
        card_id=memory.card_id,
        state=State.Review,
        stability=stability + weight * ((full.stability or 0.0) - stability),
        difficulty=difficulty + weight * ((full.difficulty or 0.0) - difficulty),
        due=full.due,
        last_review=now,
    )


def learned(
    stability: float, success_days: int, parameters: Parameters, *, was_learned: bool
) -> bool:
    if was_learned:
        return stability >= parameters.unlearned_stability
    return (
        stability >= parameters.learned_stability
        and success_days >= parameters.learned_days
    )


def review(
    strength: Strength,
    rating: Rating,
    weight: float,
    now: datetime,
    parameters: Parameters,
) -> Strength:
    full, _ = MEMORY.review_card(strength.memory or Card(card_id=0), rating, now)
    if strength.memory is not None and weight < 1:
        full = _blend(strength.memory, full, weight, now)
    days = strength.success_days
    last_success = strength.last_success
    if weight >= 1:
        if rating == Rating.Again:
            days = 0
            last_success = None
        elif (
            rating in (Rating.Good, Rating.Easy)
            and last_success != now.date().isoformat()
        ):
            days += 1
            last_success = now.date().isoformat()
    return Strength(
        full,
        days,
        last_success,
        learned(full.stability or 0.0, days, parameters, was_learned=strength.learned),
    )
