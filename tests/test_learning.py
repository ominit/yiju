import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest
from conftest import add, set_strength, states

from yiju.storage.database import ConflictError
from yiju.storage.learning import review_card, undo_review

if TYPE_CHECKING:
    import sqlite3

NOW = datetime(2026, 10, 1, 9, tzinfo=UTC)


def card(
    connection: sqlite3.Connection,
    *,
    word: str | None = None,
    sentence: str | None = None,
    card_type: str = "recognition",
) -> sqlite3.Row:
    if word is not None:
        return cast(
            "sqlite3.Row",
            connection.execute(
                "SELECT * FROM cards WHERE word = ? AND card_type = ?",
                (word, card_type),
            ).fetchone(),
        )
    return cast(
        "sqlite3.Row",
        connection.execute(
            "SELECT c.* FROM cards c JOIN sentences s ON s.id = c.sentence_id "
            "WHERE s.chinese = ? AND c.card_type = ?",
            (sentence, card_type),
        ).fetchone(),
    )


def word(connection: sqlite3.Connection, value: str) -> sqlite3.Row:
    return cast(
        "sqlite3.Row",
        connection.execute("SELECT * FROM words WHERE word = ?", (value,)).fetchone(),
    )


def logged(connection: sqlite3.Connection, review_id: int) -> dict[str, sqlite3.Row]:
    return {
        row["word"]: row
        for row in connection.execute(
            "SELECT * FROM review_words WHERE review_id = ?", (review_id,)
        )
    }


def rate(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    rating: int,
    now: datetime = NOW,
    marked: tuple[str, ...] = (),
) -> int:
    return review_card(connection, row["id"], row["version"], rating, now, marked)


@pytest.fixture
def library(connection: sqlite3.Connection) -> sqlite3.Connection:
    add(connection, "我 很 好 | I am well.\n酸苹果 | sour apple\n", NOW)
    return connection


def unlock_sentence(connection: sqlite3.Connection) -> sqlite3.Row:
    for value in ("我", "很", "好"):
        set_strength(connection, value, stability=10, learned=False, now=NOW)
    assert states(connection, "sentences")["我很好"] == "unlocked"
    return card(connection, sentence="我很好")


def test_a_word_card_review_marks_the_word_seen_and_logs_its_strength(
    library: sqlite3.Connection,
) -> None:
    review = rate(library, card(library, word="苹果"), 3)
    row = word(library, "苹果")
    assert row["state"] == "seen"
    assert row["success_days"] == 1
    assert json.loads(row["memory"])["stability"] > 0
    entry = logged(library, review)["苹果"]
    assert entry["role"] == "card"
    assert entry["weight"] == 1
    assert entry["state_before"] == "unlocked"
    assert entry["recall_before"] is None
    assert entry["memory_after"] == row["memory"]
    parameters = library.execute(
        "SELECT p.parameters FROM reviews r JOIN parameter_sets p "
        "ON p.id = r.parameter_set WHERE r.id = ?",
        (review,),
    ).fetchone()[0]
    assert json.loads(parameters)["sentence_pass_weight"] == 0.2


def test_both_word_cards_feed_one_word_memory_with_separate_schedules(
    library: sqlite3.Connection,
) -> None:
    rate(library, card(library, word="苹果"), 3)
    first = json.loads(word(library, "苹果")["memory"])["stability"]
    later = NOW + timedelta(days=2)
    rate(library, card(library, word="苹果", card_type="production"), 3, later)
    assert json.loads(word(library, "苹果")["memory"])["stability"] > first
    recognition = card(library, word="苹果")
    production = card(library, word="苹果", card_type="production")
    assert recognition["due"] != production["due"]
    assert recognition["reviews"] == production["reviews"] == 1


def test_a_passed_sentence_moves_every_word_a_little(
    library: sqlite3.Connection,
) -> None:
    sentence = unlock_sentence(library)
    schedules = {
        value: card(library, word=value)["due"] for value in ("我", "很", "好")
    }
    later = NOW + timedelta(days=5)
    review = rate(library, sentence, 3, later)
    entries = logged(library, review)
    assert set(entries) == {"我", "很", "好"}
    for value, entry in entries.items():
        assert entry["role"] == "sentence"
        assert entry["weight"] == 0.2
        before = json.loads(entry["memory_before"])["stability"]
        after = json.loads(entry["memory_after"])["stability"]
        assert before < after
        assert word(library, value)["success_days"] == 3
        assert card(library, word=value)["due"] == schedules[value]


def test_a_failed_sentence_without_marks_counts_against_every_word_weakly(
    library: sqlite3.Connection,
) -> None:
    sentence = unlock_sentence(library)
    review = rate(library, sentence, 1, NOW + timedelta(days=5))
    entries = logged(library, review)
    assert {entry["weight"] for entry in entries.values()} == {0.25}
    assert {entry["rating"] for entry in entries.values()} == {1}
    for entry in entries.values():
        assert entry["success_days_before"] == 3
    assert all(word(library, value)["success_days"] == 3 for value in entries)


def test_marked_words_take_a_full_failure_and_the_rest_are_left_alone(
    library: sqlite3.Connection,
) -> None:
    sentence = unlock_sentence(library)
    review = rate(library, sentence, 1, NOW + timedelta(days=5), ("好",))
    entries = logged(library, review)
    assert set(entries) == {"好"}
    assert entries["好"]["role"] == "marked"
    assert entries["好"]["weight"] == 1
    assert word(library, "好")["success_days"] == 0
    assert word(library, "我")["success_days"] == 3


def test_marked_words_fail_even_when_the_sentence_passes(
    library: sqlite3.Connection,
) -> None:
    sentence = unlock_sentence(library)
    review = rate(library, sentence, 3, NOW + timedelta(days=5), ("好",))
    entries = logged(library, review)
    assert entries["好"]["rating"] == 1
    assert entries["我"]["rating"] == 3
    assert entries["我"]["weight"] == 0.2


def test_only_words_in_the_sentence_can_be_marked(library: sqlite3.Connection) -> None:
    sentence = unlock_sentence(library)
    with pytest.raises(ValueError, match="Only words"):
        rate(library, sentence, 1, NOW + timedelta(days=5), ("苹果",))
    assert library.execute("SELECT count(*) FROM reviews").fetchone()[0] == 0


def test_a_lapse_that_unlearns_a_word_relocks_the_graph_and_undo_restores_it(
    connection: sqlite3.Connection,
) -> None:
    add(connection, "我 很 好 | I am well.\n", NOW)
    set_strength(connection, "我", stability=4, now=NOW)
    assert states(connection)["很"] == "unlocked"
    words_before = [
        tuple(row) for row in connection.execute("SELECT * FROM words ORDER BY word")
    ]
    review = rate(connection, card(connection, word="我"), 1, NOW + timedelta(days=3))
    assert word(connection, "我")["learned"] == 0
    assert states(connection, "sentences")["我很好"] == "locked"
    assert states(connection)["很"] == "locked"
    changes = {
        (row["kind"], row["key"]): row["state_before"]
        for row in connection.execute(
            "SELECT * FROM review_changes WHERE review_id = ?", (review,)
        )
    }
    assert changes == {
        ("sentence", "1"): "eligible",
        ("word", "很"): "unlocked",
        ("word", "好"): "unlocked",
    }
    undo_review(connection, review)
    assert states(connection, "sentences")["我很好"] == "eligible"
    after = [
        tuple(row)[:-1]
        for row in connection.execute("SELECT * FROM words ORDER BY word")
    ]
    assert after == [row[:-1] for row in words_before]


def test_undo_restores_a_first_review_and_only_the_latest_can_be_undone(
    library: sqlite3.Connection,
) -> None:
    before = card(library, word="苹果")
    first = rate(library, before, 3)
    second = rate(library, card(library, word="酸"), 3)
    with pytest.raises(ConflictError, match="most recent"):
        undo_review(library, first)
    undo_review(library, second)
    undo_review(library, first)
    restored = card(library, word="苹果")
    assert restored["scheduling"] == before["scheduling"]
    assert restored["reviews"] == 0
    assert word(library, "苹果")["state"] == "unlocked"
    assert word(library, "苹果")["memory"] is None


def test_reviews_require_the_current_version_a_due_card_and_an_open_word(
    library: sqlite3.Connection,
) -> None:
    apple = card(library, word="苹果")
    rate(library, apple, 3)
    with pytest.raises(ConflictError, match="changed"):
        rate(library, apple, 3)
    with pytest.raises(ConflictError, match="not due"):
        rate(library, card(library, word="苹果"), 3)
    with pytest.raises(ConflictError, match="not available"):
        rate(library, card(library, word="我"), 3)
    with pytest.raises(ConflictError, match="not available"):
        rate(library, card(library, sentence="我很好"), 3)
