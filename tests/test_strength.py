from datetime import UTC, datetime, timedelta

import pytest
from fsrs import Rating

from yiju.core.strength import Parameters, Strength, learned, review

START = datetime(2026, 10, 1, 9, tzinfo=UTC)
PARAMETERS = Parameters()


def fresh() -> Strength:
    return Strength(None, 0, None, learned=False)


def test_full_reviews_count_success_days_once_per_day() -> None:
    strength = review(fresh(), Rating.Good, 1.0, START, PARAMETERS)
    assert strength.success_days == 1
    strength = review(
        strength, Rating.Good, 1.0, START + timedelta(hours=1), PARAMETERS
    )
    assert strength.success_days == 1
    strength = review(strength, Rating.Hard, 1.0, START + timedelta(days=1), PARAMETERS)
    assert strength.success_days == 1
    strength = review(strength, Rating.Good, 1.0, START + timedelta(days=2), PARAMETERS)
    assert strength.success_days == 2
    strength = review(
        strength, Rating.Again, 1.0, START + timedelta(days=3), PARAMETERS
    )
    assert strength.success_days == 0
    assert strength.last_success is None


def test_partial_reviews_move_strength_part_way_without_counting_days() -> None:
    base = review(fresh(), Rating.Good, 1.0, START, PARAMETERS)
    later = START + timedelta(days=3)
    full = review(base, Rating.Good, 1.0, later, PARAMETERS)
    partial = review(base, Rating.Good, 0.2, later, PARAMETERS)
    assert base.stability < partial.stability < full.stability
    assert partial.stability - base.stability == pytest.approx(
        0.2 * (full.stability - base.stability)
    )
    assert partial.success_days == base.success_days
    failed = review(base, Rating.Again, 0.25, later, PARAMETERS)
    assert failed.stability < base.stability
    assert failed.success_days == base.success_days


def test_learned_needs_strength_and_days_then_holds_until_a_real_drop() -> None:
    assert not learned(20, 2, PARAMETERS, was_learned=False)
    assert not learned(6.9, 5, PARAMETERS, was_learned=False)
    assert learned(7, 3, PARAMETERS, was_learned=False)
    assert learned(4, 0, PARAMETERS, was_learned=True)
    assert not learned(2.9, 9, PARAMETERS, was_learned=True)


def test_time_off_does_not_unlearn_a_word() -> None:
    strength = Strength(None, 0, None, learned=False)
    now = START
    for day in (0, 1, 3, 8, 20):
        now = START + timedelta(days=day)
        strength = review(strength, Rating.Good, 1.0, now, PARAMETERS)
    assert strength.learned
    later = now + timedelta(days=2000)
    assert (strength.recall(later) or 1) < 0.7
    assert review(strength, Rating.Hard, 0.2, later, PARAMETERS).learned


def test_a_lapse_drops_a_learned_word_back_to_learning() -> None:
    strength = Strength(None, 0, None, learned=False)
    now = START
    for day in (0, 1, 3, 8):
        now = START + timedelta(days=day)
        strength = review(strength, Rating.Good, 1.0, now, PARAMETERS)
    assert strength.learned
    lapsed = review(strength, Rating.Again, 1.0, now + timedelta(days=30), PARAMETERS)
    assert not lapsed.learned


def test_parameters_read_from_settings_keep_their_types() -> None:
    parameters = Parameters.from_settings({"learned_days": 4.0, "other": 1})
    assert parameters.learned_days == 4
    assert isinstance(parameters.learned_days, int)
    assert parameters.learned_stability == PARAMETERS.learned_stability
