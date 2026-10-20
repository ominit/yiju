import sqlite3
from datetime import UTC, datetime, timedelta
from random import Random

import pytest
from conftest import add, set_strength, states

from yiju.storage.learning import review_card
from yiju.storage.queue import counts, overview, study_queue

NOW = datetime(2026, 10, 1, 9, tzinfo=UTC)
PHRASES = "".join(
    f"{phrase} | {meaning}\n"
    for phrase, meaning in [
        ("酸苹果", "sour apple"),
        ("喝水", "drink water"),
        ("吃饭", "eat"),
        ("看书", "read"),
    ]
)


def kinds(queue: list[sqlite3.Row]) -> list[tuple[str | None, str]]:
    return [(row["word"], row["card_type"]) for row in queue]


def words(queue: list[sqlite3.Row]) -> list[sqlite3.Row]:
    return [row for row in queue if row["word"] is not None]


def limit(connection: sqlite3.Connection, value: int) -> None:
    connection.execute(
        "UPDATE settings SET value = ? WHERE key = 'new_per_day'", (str(value),)
    )
    connection.commit()


def rate(
    connection: sqlite3.Connection, row: sqlite3.Row, now: datetime, rating: int = 3
) -> int:
    return review_card(connection, row["id"], row["version"], rating, now)


def test_new_words_bring_both_cards_and_only_unlocked_words_are_offered(
    connection: sqlite3.Connection,
) -> None:
    add(connection, PHRASES + "我 学习 中文 | I study Chinese.\n", NOW)
    queue = study_queue(connection, NOW)
    unlocked = {key for key, value in states(connection).items() if value == "unlocked"}
    assert {row["word"] for row in queue} == unlocked
    assert sorted(kinds(queue)) == sorted(
        (word, card_type)
        for word in unlocked
        for card_type in ("recognition", "production")
    )
    assert not {"我", "学习", "中文"} & {row["word"] for row in queue}


def test_the_daily_limit_counts_new_cards(connection: sqlite3.Connection) -> None:
    add(connection, PHRASES, NOW)
    limit(connection, 2)
    queue = study_queue(connection, NOW)
    assert counts(queue) == {"reviews": 0, "new": 2}
    first = words(queue)[0]
    rate(connection, first, NOW, 4)
    queue = study_queue(connection, NOW)
    assert counts(queue)["new"] == 1
    for row in queue:
        rate(connection, row, NOW, 4)
    assert study_queue(connection, NOW + timedelta(minutes=1)) == []
    assert counts(study_queue(connection, NOW + timedelta(days=1)))["new"] == 2


def test_a_card_whose_other_direction_was_studied_comes_first(
    connection: sqlite3.Connection,
) -> None:
    add(connection, PHRASES, NOW)
    limit(connection, 1)
    first = words(study_queue(connection, NOW))[0]
    rate(connection, first, NOW, 4)
    later = NOW + timedelta(days=1)
    for _ in range(10):
        queue = study_queue(connection, later)
        assert kinds([row for row in queue if not row["reviews"]]) == [
            (
                first["word"],
                "production" if first["card_type"] == "recognition" else "recognition",
            )
        ]
    limit(connection, 20)
    queue = [row for row in study_queue(connection, later) if not row["reviews"]]
    assert queue[0]["word"] == first["word"]
    assert all(row["word"] != first["word"] for row in queue[1:])


def test_new_words_are_drawn_in_random_order(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    random = Random(7)
    monkeypatch.setattr("yiju.storage.queue.SystemRandom", lambda: random)
    add(connection, PHRASES, NOW)
    orders = {tuple(kinds(study_queue(connection, NOW))) for _ in range(20)}
    assert len(orders) > 1


def test_reviews_come_before_new_material(connection: sqlite3.Connection) -> None:
    add(connection, PHRASES, NOW)
    first = study_queue(connection, NOW)[0]
    rate(connection, first, NOW)
    later = NOW + timedelta(days=4)
    queue = study_queue(connection, later)
    assert queue[0]["id"] == first["id"]
    assert all(row["reviews"] == 0 for row in queue[1:])


def test_unlocked_sentences_count_toward_the_limit(
    connection: sqlite3.Connection,
) -> None:
    add(connection, PHRASES, NOW)
    for value in ("酸", "苹果"):
        set_strength(connection, value, now=NOW)
    later = NOW + timedelta(days=1)
    queue = study_queue(connection, later)
    sentences = [row for row in queue if row["sentence_id"] is not None]
    assert {row["chinese"] for row in sentences} == {"酸苹果"}
    assert {row["card_type"] for row in sentences} == {"recognition", "production"}
    limit(connection, 0)
    assert study_queue(connection, later) == []


def test_a_card_is_offered_whenever_it_is_due(connection: sqlite3.Connection) -> None:
    add(connection, PHRASES, NOW)
    for value in ("酸", "苹果"):
        set_strength(connection, value, now=NOW)
    sentence = [
        row for row in study_queue(connection, NOW) if row["sentence_id"] is not None
    ]
    rate(connection, sentence[0], NOW)
    assert [
        row["id"]
        for row in study_queue(connection, NOW)
        if row["sentence_id"] is not None
    ] == [sentence[1]["id"]]


def test_either_card_of_a_new_word_can_come_first(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    random = Random(3)
    monkeypatch.setattr("yiju.storage.queue.SystemRandom", lambda: random)
    add(connection, PHRASES, NOW)
    firsts = {study_queue(connection, NOW)[0]["card_type"] for _ in range(20)}
    assert firsts == {"recognition", "production"}


def test_paused_words_and_skipped_sentences_leave_the_queue(
    connection: sqlite3.Connection,
) -> None:
    add(connection, PHRASES, NOW)
    connection.execute("UPDATE cards SET suspended = 1 WHERE word = '酸'")
    connection.commit()
    assert "酸" not in {row["word"] for row in study_queue(connection, NOW)}


def test_production_cards_need_a_meaning_to_prompt_with(
    connection: sqlite3.Connection,
) -> None:
    add(connection, "西安 | Xi'an\n", NOW)
    set_strength(connection, "西安", now=NOW)
    assert [
        row["card_type"]
        for row in study_queue(connection, NOW + timedelta(days=1))
        if row["word"] == "西安"
    ] == ["recognition"]
    assert (
        study_queue(
            connection,
            NOW + timedelta(days=1),
            card_id=connection.execute(
                "SELECT id FROM cards WHERE word = '西安' AND card_type = 'production'"
            ).fetchone()[0],
        )
        == []
    )


def test_targeted_lookup_finds_a_card_beyond_the_random_draw(
    connection: sqlite3.Connection,
) -> None:
    add(connection, PHRASES, NOW)
    limit(connection, 1)
    rows = connection.execute(
        "SELECT * FROM cards WHERE card_type = 'recognition' AND word IS NOT NULL"
    ).fetchall()
    found = [
        row["id"] for row in rows if study_queue(connection, NOW, card_id=row["id"])
    ]
    assert len(found) == 6


def test_overview_counts_states(connection: sqlite3.Connection) -> None:
    add(connection, PHRASES + "我 学习 中文 | I study Chinese.\n", NOW)
    set_strength(connection, "酸", now=NOW)
    stats = overview(connection, NOW)
    assert stats["learned"] == 1
    assert stats["locked"] == 3
    assert stats["unlocked"] == 5
    assert stats["sentences_eligible"] == 4
    assert stats["sentences_locked"] == 1
    assert stats["pending"] == 0
