import io
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from conftest import csrf, seed_known_word
from flask import Flask
from flask.testing import FlaskClient

from yiju.storage.database import connect
from yiju.web import actions

HEADERS = {"X-Yiju-Partial": "study", "X-Yiju-Buffer": "1"}


def test_prepared_cards_have_distinct_families_and_hidden_answers(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    client.post(
        "/import",
        data={"csrf": csrf(client), "file": (io.BytesIO(xml_export), "words.xml")},
    )
    with connect(Path(app.config["DATABASE"])) as db:
        seed_known_word(db, "我")
        selected = db.execute(
            "SELECT id FROM cards WHERE word = '我' AND sentence_id IS NOT NULL "
            "AND direction = 'production'"
        ).fetchone()["id"]
    page = client.get("/study").data.decode()
    assert '<template id="study-prepared">' in page
    response = client.get(f"/study?card={selected}", headers=HEADERS)
    assert response.status_code == 200
    batch = response.json
    assert batch is not None
    assert len(batch["cards"]) > 1
    assert any("recall-prompt" in card for card in batch["cards"])
    assert any("recall-prompt" not in card for card in batch["cards"])
    keys = []
    families = []
    for card in batch["cards"]:
        assert re.search(r'<div\b[^>]*\bid="answer"[^>]*\s+hidden(?:\s|>)', card)
        assert re.search(r'class="rating-area"[\s\S]*?hidden>', card)
        key = re.search(r'data-card-id="([^"]+)"', card)
        assert key is not None
        keys.append(key[1])
        family = re.search(r'data-card-family="([^"]+)"', card)
        assert family is not None
        families.append(family[1])
    assert len(keys) == len(set(keys))
    assert len(families) == len(set(families))


def test_background_ratings_return_correct_receipts_and_retry_without_duplicates(
    client: FlaskClient, app: Flask, xml_export: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = csrf(client)
    client.post(
        "/import", data={"csrf": token, "file": (io.BytesIO(xml_export), "words.xml")}
    )
    with connect(Path(app.config["DATABASE"])) as db:
        seed_known_word(db, "我")
        cards = db.execute(
            "SELECT * FROM cards WHERE "
            "(sentence_id IS NOT NULL AND word = '我' AND direction = 'production') "
            "OR (sentence_id IS NULL AND direction = 'listening') "
            "ORDER BY (sentence_id IS NULL), id"
        ).fetchall()
    original = actions.study_queue
    validated = []

    def targeted_queue(
        connection: sqlite3.Connection, now: datetime, *, card_id: str | None = None
    ) -> list[sqlite3.Row]:
        assert card_id is not None
        validated.append(card_id)
        return original(connection, now, card_id=card_id)

    monkeypatch.setattr(actions, "study_queue", targeted_queue)
    receipts = []
    for card in cards:
        data = {"csrf": token, "version": card["version"], "rating": 3}
        url = f"/review/{card['id']}"
        response = client.post(url, data=data, headers=HEADERS)
        assert response.status_code == 200
        assert response.json is not None
        receipt = response.json["review_id"]
        receipts.append(receipt)
        retry = client.post(url, data=data, headers=HEADERS)
        assert retry.json == response.json
        assert (
            client.post(url, data={**data, "rating": 1}, headers=HEADERS).status_code
            == 409
        )
        with connect(Path(app.config["DATABASE"])) as db:
            review = db.execute(
                "SELECT * FROM reviews WHERE id = ?", (receipt,)
            ).fetchone()
            assert review["card_id"] == card["id"]
            assert review["rating"] == 3
            assert db.execute(
                "SELECT count(*) FROM review_words WHERE review_id = ?", (receipt,)
            ).fetchone()[0] == (2 if card["sentence_id"] else 1)
    assert validated == [card["id"] for card in cards]
    with connect(Path(app.config["DATABASE"])) as db:
        assert db.execute("SELECT count(*) FROM reviews").fetchone()[0] == len(cards)
    assert (
        client.post(
            "/review/undo",
            data={"csrf": token, "review_id": receipts[-1]},
            headers=HEADERS,
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/review/{cards[-1]['id']}",
            data={"csrf": token, "version": cards[-1]["version"], "rating": 3},
            headers=HEADERS,
        ).status_code
        == 409
    )


def test_prepared_ratings_still_obey_daily_limit_and_suspension(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    token = csrf(client)
    client.post(
        "/import", data={"csrf": token, "file": (io.BytesIO(xml_export), "words.xml")}
    )
    with connect(Path(app.config["DATABASE"])) as db:
        cards = db.execute("SELECT * FROM cards WHERE sentence_id IS NULL").fetchall()
        db.execute(
            "UPDATE settings SET value = ? WHERE key = 'new_per_day'", (json.dumps(1),)
        )
        db.commit()
    data = {"csrf": token, "version": 0, "rating": 3}
    assert (
        client.post(f"/review/{cards[0]['id']}", data=data, headers=HEADERS).status_code
        == 200
    )
    assert (
        client.post(f"/review/{cards[1]['id']}", data=data, headers=HEADERS).status_code
        == 409
    )
    with connect(Path(app.config["DATABASE"])) as db:
        db.execute("UPDATE settings SET value = '20' WHERE key = 'new_per_day'")
        db.execute("UPDATE cards SET suspended = 1 WHERE id = ?", (cards[1]["id"],))
        db.commit()
    assert (
        client.post(f"/review/{cards[1]['id']}", data=data, headers=HEADERS).status_code
        == 409
    )
    assert (
        client.post(
            f"/review/{cards[2]['id']}",
            data={**data, "csrf": "invalid"},
            headers=HEADERS,
        ).status_code
        == 400
    )


def test_buffered_rating_buries_related_directions(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    token = csrf(client)
    client.post(
        "/import", data={"csrf": token, "file": (io.BytesIO(xml_export), "words.xml")}
    )
    with connect(Path(app.config["DATABASE"])) as db:
        cards = db.execute(
            "SELECT * FROM cards WHERE sentence_id IS NULL AND word = '去'"
        ).fetchall()
    data = {"csrf": token, "version": 0, "rating": 3}
    assert (
        client.post(f"/review/{cards[0]['id']}", data=data, headers=HEADERS).status_code
        == 200
    )
    for sibling in cards[1:]:
        assert (
            client.post(
                f"/review/{sibling['id']}", data=data, headers=HEADERS
            ).status_code
            == 409
        )
    with connect(Path(app.config["DATABASE"])) as db:
        assert db.execute("SELECT count(*) FROM reviews").fetchone()[0] == 1
