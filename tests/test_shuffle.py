import sqlite3
from datetime import UTC, datetime, timedelta
from random import Random

import pytest

from yiju.storage.importing import import_xml
from yiju.storage.learning import review_card
from yiju.storage.queue import study_queue


def test_new_words_mix_directions_before_daily_limit(
    connection: sqlite3.Connection, xml_export: bytes
) -> None:
    import_xml(connection, xml_export, "words.xml")
    connection.execute("UPDATE settings SET value = '2' WHERE key = 'new_per_day'")
    connection.commit()
    now = datetime.now(UTC) + timedelta(seconds=1)
    for _ in range(10):
        queue = study_queue(connection, now)
        assert len(queue) == 2
        assert {card["direction"] for card in queue} == {"recognition", "production"}
        assert all(card["sentence_id"] is None for card in queue)


def test_fresh_decks_do_not_follow_one_fixed_order(
    connection: sqlite3.Connection, xml_export: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    random = Random(42)
    monkeypatch.setattr("yiju.storage.queue.SystemRandom", lambda: random)
    import_xml(connection, xml_export, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    orders = [study_queue(connection, now) for _ in range(20)]
    assert len({tuple(card["id"] for card in order) for order in orders}) > 1
    assert {order[0]["direction"] for order in orders} == {"recognition", "production"}
    expected = {
        row["id"]
        for row in connection.execute("SELECT id FROM cards WHERE sentence_id IS NULL")
    }
    for order in orders:
        assert {card["id"] for card in order} == expected
        assert len(order) == len(expected)
    assert connection.execute("SELECT count(*) FROM reviews").fetchone()[0] == 0


def test_reviews_keep_due_order_and_can_validate_any_prepared_new_card(
    connection: sqlite3.Connection, xml_export: bytes
) -> None:
    import_xml(connection, xml_export, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    cards = connection.execute(
        "SELECT * FROM cards WHERE sentence_id IS NULL ORDER BY id"
    ).fetchall()
    for index, card in enumerate(cards[:2]):
        review_card(connection, card["id"], card["version"], 3, now)
        connection.execute(
            "UPDATE cards SET due = ? WHERE id = ?",
            ((now - timedelta(minutes=index + 1)).isoformat(), card["id"]),
        )
        connection.commit()
    connection.commit()
    for _ in range(5):
        queue = study_queue(connection, now)
        assert [card["id"] for card in queue[:2]] == [cards[1]["id"], cards[0]["id"]]
        for card in queue[2:]:
            targeted = study_queue(connection, now, card_id=card["id"])
            assert [row["id"] for row in targeted] == [card["id"]]


@pytest.mark.parametrize("remaining", [0, 1])
def test_shuffle_respects_small_daily_limits(
    connection: sqlite3.Connection, xml_export: bytes, remaining: int
) -> None:
    import_xml(connection, xml_export, "words.xml")
    connection.execute(
        "UPDATE settings SET value = ? WHERE key = 'new_per_day'", (str(remaining),)
    )
    connection.commit()
    now = datetime.now(UTC) + timedelta(seconds=1)
    assert len(study_queue(connection, now)) == remaining
