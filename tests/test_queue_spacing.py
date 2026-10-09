import io
import re
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import csrf, seed_known_word
from flask import Flask
from flask.testing import FlaskClient

from yiju.storage.cards import create_sentence_cards
from yiju.storage.database import connect
from yiju.storage.importing import import_xml
from yiju.storage.learning import review_card, undo_review
from yiju.storage.queue import card_family, overview, study_queue


@pytest.fixture
def large_export() -> bytes:
    cards = "".join(
        '<card language="chinese"><entry><headword charset="sc">'
        f'{chr(0x4E00 + index)}</headword><pron type="hypy">zi4</pron>'
        f"<defn>noun word {index}</defn></entry></card>"
        for index in range(30)
    )
    return f'<plecoflash formatversion="2"><cards>{cards}</cards></plecoflash>'.encode()


@pytest.mark.parametrize("include_listening", [False, True])
def test_family_selection_fills_daily_limit(
    connection: sqlite3.Connection, large_export: bytes, *, include_listening: bool
) -> None:
    import_xml(connection, large_export, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    queue = study_queue(connection, now, include_listening=include_listening)
    assert len(queue) == 20
    assert len({card_family(card) for card in queue}) == 20
    assert any(card["direction"] == "listening" for card in queue) == include_listening
    for card in queue:
        assert study_queue(connection, now, card_id=card["id"])
    assert overview(connection, now, include_listening=include_listening)["new"] == 20


def test_due_family_does_not_consume_the_new_card_quota(
    connection: sqlite3.Connection, large_export: bytes
) -> None:
    import_xml(connection, large_export, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    connection.execute(
        "UPDATE cards SET reviews = 1 WHERE id = "
        "(SELECT id FROM cards WHERE direction = 'listening' LIMIT 1)"
    )
    connection.commit()
    queue = study_queue(connection, now)
    assert len(queue) == 21
    assert queue[0]["reviews"] == 1
    assert len({card_family(card) for card in queue}) == 21


@pytest.mark.parametrize("sentence", [False, True])
def test_review_buries_siblings_but_allows_repeats_and_undo(
    connection: sqlite3.Connection, xml_export: bytes, *, sentence: bool
) -> None:
    import_xml(connection, xml_export, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    seed_known_word(connection, "我", now)
    cards = connection.execute(
        "SELECT * FROM cards WHERE word = ? AND (sentence_id IS NOT NULL) = ?",
        ("我" if sentence else "去", sentence),
    ).fetchall()
    card = next(row for row in cards if row["direction"] == "listening")
    review = review_card(connection, card["id"], card["version"], 1, now)
    due = datetime.fromisoformat(
        connection.execute(
            "SELECT due FROM cards WHERE id = ?", (card["id"],)
        ).fetchone()["due"]
    )
    queue = study_queue(connection, due)
    assert [row["id"] for row in queue if card_family(row) == card_family(card)] == [
        card["id"]
    ]
    assert study_queue(connection, due, card_id=card["id"])
    for sibling in cards:
        if sibling["id"] != card["id"]:
            assert not study_queue(connection, due, card_id=sibling["id"])
    undo_review(connection, review)
    for sibling in cards:
        assert study_queue(connection, now, card_id=sibling["id"])


def test_burial_ends_at_utc_midnight(
    connection: sqlite3.Connection, xml_export: bytes
) -> None:
    import_xml(connection, xml_export, "words.xml")
    now = datetime.now(UTC).replace(hour=23, minute=59, second=0, microsecond=0)
    cards = connection.execute(
        "SELECT * FROM cards WHERE word = '去' AND sentence_id IS NULL "
        "ORDER BY direction"
    ).fetchall()
    review_card(connection, cards[0]["id"], cards[0]["version"], 3, now)
    local = now.astimezone(timezone(timedelta(hours=8)))
    assert not study_queue(connection, local, card_id=cards[1]["id"])
    tomorrow = local + timedelta(minutes=1)
    assert study_queue(connection, tomorrow, card_id=cards[1]["id"])


def test_only_exact_sentence_siblings_are_buried(
    connection: sqlite3.Connection, xml_export: bytes
) -> None:
    import_xml(connection, xml_export, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    seed_known_word(connection, "我", now)
    card = connection.execute(
        "SELECT * FROM cards WHERE word = '我' AND sentence_id IS NOT NULL "
        "AND direction = 'recognition'"
    ).fetchone()
    sentence_id = "other-sentence"
    connection.execute(
        "INSERT INTO sentences SELECT ?, entry_id, '我去!', pinyin, translation, "
        "tokens, status, version FROM sentences WHERE id = ?",
        (sentence_id, card["sentence_id"]),
    )
    create_sentence_cards(connection, sentence_id, card["entry_id"], card["word"], now)
    connection.commit()
    review_card(connection, card["id"], card["version"], 3, now)
    queue = study_queue(connection, now)
    assert any(row["sentence_id"] == sentence_id for row in queue)
    assert any(row["word"] == card["word"] and not row["sentence_id"] for row in queue)


def test_due_family_representative_is_stable_and_targeted_lookup_remains_valid(
    connection: sqlite3.Connection, xml_export: bytes
) -> None:
    import_xml(connection, xml_export, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    connection.execute("UPDATE cards SET reviews = 1 WHERE sentence_id IS NULL")
    connection.commit()
    cards = connection.execute(
        "SELECT * FROM cards WHERE sentence_id IS NULL ORDER BY due, id"
    ).fetchall()
    expected = {}
    for card in cards:
        expected.setdefault(card["word"], card["id"])
    for _ in range(3):
        queue = study_queue(connection, now)
        assert [card["id"] for card in queue] == list(expected.values())
    for card in cards:
        assert study_queue(connection, now, card_id=card["id"])


def test_muted_buffer_refills_and_preserves_requested_card(
    client: FlaskClient, app: Flask, large_export: bytes
) -> None:
    client.post(
        "/import",
        data={"csrf": csrf(client), "file": (io.BytesIO(large_export), "words.xml")},
    )
    with connect(Path(app.config["DATABASE"])) as db:
        card = db.execute(
            "SELECT * FROM cards WHERE direction = 'production' ORDER BY id LIMIT 1"
        ).fetchone()
    headers = {"X-Yiju-Partial": "study", "X-Yiju-Buffer": "1", "X-Yiju-Audio": "muted"}
    response = client.get(f"/study?card={card['id']}", headers=headers)
    assert response.status_code == 200
    batch = response.json
    assert batch is not None
    assert batch["include_listening"] is False
    assert batch["count"] == 20
    assert len(batch["cards"]) == 12
    assert f'data-card-id="{card["id"]}"' in batch["cards"][0]
    families = []
    for prepared in batch["cards"]:
        assert 'data-direction="listening"' not in prepared
        match = re.search(r'data-card-family="([^"]+)"', prepared)
        assert match is not None
        families.append(match[1])
    assert len(set(families)) == len(families)


def test_audio_query_fallback_filters_listening(client: FlaskClient) -> None:
    response = client.get("/study?audio=muted", headers={"X-Yiju-Buffer": "1"})
    assert response.json is not None
    assert response.json["include_listening"] is False


def test_overview_respects_muted_eligibility(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    client.post(
        "/import",
        data={"csrf": csrf(client), "file": (io.BytesIO(xml_export), "words.xml")},
    )
    with connect(Path(app.config["DATABASE"])) as db:
        db.execute("UPDATE cards SET suspended = (direction != 'listening')")
        db.commit()
    assert b"3 new cards available" in client.get("/").data
    assert (
        b"0 new cards available"
        in client.get("/", headers={"X-Yiju-Audio": "muted"}).data
    )
