import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from conftest import (
    csrf,
    import_export,
    page_context,
    post_json,
    rate,
    seed_known_word,
    study_cards,
)
from flask import Flask
from flask.testing import FlaskClient

from yiju.storage.database import connect
from yiju.web import study


def database(app: Flask) -> sqlite3.Connection:
    return connect(Path(app.config["DATABASE"]))


def test_study_page_embeds_the_same_batch_as_the_api(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    import_export(client, xml_export)
    with database(app) as db:
        seed_known_word(db, "我")
    page = client.get("/study").data.decode()
    embedded = re.search(
        r'<script type="application/json" id="study-data">(.*?)</script>',
        page,
        re.DOTALL,
    )
    assert embedded is not None
    data = json.loads(embedded[1])
    batch = client.get("/api/study").json
    assert batch is not None
    assert data["count"] == batch["count"] > 0
    assert {card["family"] for card in data["cards"]} == {
        card["family"] for card in batch["cards"]
    }


def test_batch_has_distinct_families_and_complete_payloads(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    import_export(client, xml_export)
    with database(app) as db:
        seed_known_word(db, "我")
    batch = client.get("/api/study").json
    assert batch is not None
    assert batch["lastReview"] is None
    cards = batch["cards"]
    assert batch["count"] == len(cards) > 1
    assert len({card["id"] for card in cards}) == len(cards)
    assert len({card["family"] for card in cards}) == len(cards)
    assert {card["kind"] for card in cards} == {"word", "sentence"}
    for card in cards:
        assert card["new"] is True
        assert card["flagged"] is False
        assert card["audio"] == f"/audio/cards/{card['id']}"
        assert set(card["intervals"]) == {"1", "2", "3", "4"}
        assert "".join(part[0] for part in card["reading"]) == card["text"]
        if card["kind"] == "word":
            assert card["text"] == card["word"]
            assert card["prompt"]
            assert "promptMore" not in card
            assert card["senses"]
        else:
            assert card["sentence"]["version"] == 0
            assert card["sentence"]["pinyin"] == "Wǒ qù."
            assert card["translation"]
            assert all(token["text"] in card["text"] for token in card["tokens"])


def test_ratings_return_receipts_and_retry_without_duplicates(
    client: FlaskClient, app: Flask, xml_export: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = csrf(client)
    import_export(client, xml_export, token)
    with database(app) as db:
        seed_known_word(db, "我")
        cards = db.execute(
            "SELECT * FROM cards WHERE "
            "(sentence_id IS NOT NULL AND word = '我' AND direction = 'production') "
            "OR (sentence_id IS NULL AND direction = 'production') "
            "ORDER BY (sentence_id IS NULL), id"
        ).fetchall()
    original = study.study_queue
    validated = []

    def targeted_queue(
        connection: sqlite3.Connection, now: datetime, *, card_id: str | None = None
    ) -> list[sqlite3.Row]:
        assert card_id is not None
        validated.append(card_id)
        return original(connection, now, card_id=card_id)

    monkeypatch.setattr(study, "study_queue", targeted_queue)
    receipts = []
    for card in cards:
        response = rate(client, token, card)
        assert response.status_code == 200
        assert response.json is not None
        receipt = response.json["review"]
        receipts.append(receipt)
        assert rate(client, token, card).json == response.json
        conflict = rate(client, token, card, 1)
        assert conflict.status_code == 409
        assert conflict.json is not None
        assert conflict.json["error"]
        with database(app) as db:
            review = db.execute(
                "SELECT * FROM reviews WHERE id = ?", (receipt,)
            ).fetchone()
            assert review["card_id"] == card["id"]
            assert review["rating"] == 3
            assert db.execute(
                "SELECT count(*) FROM review_words WHERE review_id = ?", (receipt,)
            ).fetchone()[0] == (2 if card["sentence_id"] else 1)
    assert validated == [card["id"] for card in cards]
    with database(app) as db:
        assert db.execute("SELECT count(*) FROM reviews").fetchone()[0] == len(cards)
    monkeypatch.setattr(study, "study_queue", original)
    assert (client.get("/api/study").json or {}).get("lastReview") == receipts[-1]
    undone = post_json(client, "/api/reviews/undo", {"review": receipts[-1]}, token)
    assert undone.status_code == 200
    assert (client.get("/api/study").json or {}).get("lastReview") == receipts[-2]
    assert rate(client, token, cards[-1]).status_code == 409


def test_import_review_and_undo_restore_knowledge(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    token = csrf(client)
    response = import_export(client, xml_export, token)
    assert response.status_code == 303
    assert b"Imported" in client.get(response.location).data
    with database(app) as db:
        seed_known_word(db, "我")
        before = dict(
            db.execute("SELECT * FROM knowledge WHERE word = '我'").fetchone()
        )
        card = db.execute(
            "SELECT * FROM cards WHERE sentence_id IS NOT NULL AND word = '我'"
        ).fetchone()
    review = rate(client, token, card).json
    assert review is not None
    with database(app) as db:
        assert db.execute("SELECT count(*) FROM reviews").fetchone()[0] == 1
        assert {row[0] for row in db.execute("SELECT word FROM knowledge")} == {
            "我",
            "去",
        }
    assert post_json(client, "/api/reviews/undo", review, token).status_code == 200
    with database(app) as db:
        assert [dict(row) for row in db.execute("SELECT * FROM knowledge")] == [before]
    assert post_json(client, "/api/reviews/undo", review, token).status_code == 409


def test_invalid_rating_requests_return_json_errors(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    token = csrf(client)
    import_export(client, xml_export, token)
    with database(app) as db:
        card = db.execute("SELECT * FROM cards LIMIT 1").fetchone()
    payload = {"card": card["id"], "version": card["version"], "rating": 3}
    for response in [
        client.post("/api/reviews", json=payload),
        client.post("/api/reviews", json=payload, headers={"X-CSRF-Token": "x"}),
        post_json(client, "/api/reviews", {**payload, "rating": 5}, token),
        post_json(client, "/api/reviews", {**payload, "version": "0"}, token),
        post_json(client, "/api/reviews", {**payload, "card": None}, token),
        client.post("/api/reviews", data="[]", headers={"X-CSRF-Token": token}),
        post_json(client, "/api/reviews/undo", {"review": True}, token),
    ]:
        assert response.status_code == 400
        assert response.json is not None
        assert response.json["error"]
    with database(app) as db:
        assert db.execute("SELECT count(*) FROM reviews").fetchone()[0] == 0


def test_ratings_obey_daily_limit_and_suspension(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    token = csrf(client)
    import_export(client, xml_export, token)
    with database(app) as db:
        first, second = db.execute(
            "SELECT * FROM cards WHERE sentence_id IS NULL "
            "AND direction = 'recognition' AND word IN ('我', '去') ORDER BY word"
        ).fetchall()
        db.execute(
            "UPDATE settings SET value = ? WHERE key = 'new_per_day'", (json.dumps(1),)
        )
        db.commit()
    assert len(study_cards(client)) == 1
    assert rate(client, token, first).status_code == 200
    assert rate(client, token, second).status_code == 409
    assert study_cards(client) == []
    with database(app) as db:
        db.execute("UPDATE settings SET value = '20' WHERE key = 'new_per_day'")
        db.execute("UPDATE cards SET suspended = 1 WHERE id = ?", (second["id"],))
        db.commit()
    assert second["id"] not in {card["id"] for card in study_cards(client)}
    assert rate(client, token, second).status_code == 409


def test_rating_buries_related_directions(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    token = csrf(client)
    import_export(client, xml_export, token)
    with database(app) as db:
        cards = db.execute(
            "SELECT * FROM cards WHERE sentence_id IS NULL AND word = '去'"
        ).fetchall()
    assert rate(client, token, cards[0]).status_code == 200
    assert not any(card["word"] == "去" for card in study_cards(client))
    for sibling in cards[1:]:
        assert rate(client, token, sibling).status_code == 409
    with database(app) as db:
        assert db.execute("SELECT count(*) FROM reviews").fetchone()[0] == 1


def test_audio_preference_does_not_change_study_or_overview(
    client: FlaskClient, xml_export: bytes
) -> None:
    import_export(client, xml_export)
    batch = client.get("/api/study").json
    assert batch is not None
    stats = page_context(client, "/")["stats"]
    assert {card["direction"] for card in batch["cards"]} <= {
        "recognition",
        "production",
    }
    for value in ["off", "on"]:
        client.set_cookie("yiju-audio", value)
        muted = client.get("/api/study").json
        assert muted is not None
        assert muted["count"] == batch["count"]
        assert page_context(client, "/")["stats"] == stats


@pytest.mark.parametrize("sentence", [False, True])
def test_production_cards_prompt_with_english_and_include_the_answer(
    client: FlaskClient, app: Flask, xml_export: bytes, *, sentence: bool
) -> None:
    token = csrf(client)
    data = (
        xml_export
        if sentence
        else xml_export.replace(
            b"verb go",
            "verb 1 go 2 leave\nnoun note (工尺谱), corresponding to 7".encode(),
        )
    )
    import_export(client, data, token)
    with database(app) as db:
        seed_known_word(db, "我")
        db.execute("UPDATE cards SET suspended = (direction != 'production')")
        db.commit()
        card = db.execute(
            "SELECT * FROM cards WHERE direction = 'production' AND word = ? "
            "AND (sentence_id IS NOT NULL) = ?",
            ("我" if sentence else "去", int(sentence)),
        ).fetchone()
    payload = next(item for item in study_cards(client) if item["id"] == card["id"])
    assert payload["direction"] == "production"
    if sentence:
        assert payload["text"] == "我去。"
        assert payload["translation"] == "I am going."
    else:
        assert payload["text"] == "去"
        assert payload["prompt"] == [
            {"pos": "verb", "meaning": "go"},
            {"pos": "verb", "meaning": "leave"},
            {"pos": "noun", "meaning": "note, corresponding to 7"},
        ]
        assert [sense["meaning"] for sense in payload["senses"]] == [
            "go",
            "leave",
            "note (工尺谱), corresponding to 7",
        ]
        assert payload["senses"][2]["examples"] == [
            {"chinese": "我去。", "pinyin": "Wǒ qù.", "translation": "I am going."}
        ]
    assert rate(client, token, card).status_code == 200


def test_prompt_lists_every_sense(client: FlaskClient, xml_export: bytes) -> None:
    data = xml_export.replace(b"verb go", b"verb 1 go 2 leave 3 die 4 remove 5 send")
    import_export(client, data)
    payload = next(card for card in study_cards(client) if card["word"] == "去")
    assert [sense["meaning"] for sense in payload["prompt"]] == [
        "go",
        "leave",
        "die",
        "remove",
        "send",
    ]
    assert "promptMore" not in payload


def test_sentence_edits_in_study_return_the_updated_card(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    token = csrf(client)
    import_export(client, xml_export, token)
    with database(app) as db:
        seed_known_word(db, "我")
        sentence = db.execute(
            "SELECT * FROM sentences WHERE chinese = '我去。'"
        ).fetchone()
        card = db.execute(
            "SELECT * FROM cards WHERE sentence_id = ? AND direction = 'recognition'",
            (sentence["id"],),
        ).fetchone()
    url = f"/api/sentences/{sentence['id']}"
    payload = {
        "card": card["id"],
        "version": sentence["version"],
        "chinese": "我去！",
        "pinyin": "Wǒ qù!",
        "translation": "I'm going!",
    }
    for invalid in [
        {**payload, "translation": " "},
        {**payload, "chinese": "你好"},
        {**payload, "version": "0"},
        {**payload, "card": None},
        {**payload, "pinyin": 1},
    ]:
        response = post_json(client, url, invalid, token)
        assert response.status_code == 400
        assert response.json is not None
        assert response.json["error"]
    saved = post_json(client, url, payload, token)
    assert saved.status_code == 200
    assert saved.json is not None
    updated = saved.json["card"]
    assert updated["id"] == card["id"]
    assert updated["version"] == card["version"] + 1
    assert updated["text"] == "我去！"
    assert updated["translation"] == "I'm going!"
    assert updated["sentence"] == {
        "id": sentence["id"],
        "version": sentence["version"] + 1,
        "pinyin": "Wǒ qù!",
    }
    studied = next(
        item for item in study_cards(client) if item["family"] == updated["family"]
    )
    assert studied["text"] == updated["text"]
    assert studied["translation"] == updated["translation"]
    assert studied["sentence"] == updated["sentence"]
    stale = post_json(client, url, payload, token)
    assert stale.status_code == 409
    assert stale.json is not None
    assert stale.json["error"]
    with database(app) as db:
        row = db.execute(
            "SELECT * FROM sentences WHERE id = ?", (sentence["id"],)
        ).fetchone()
    assert (row["chinese"], row["translation"], row["status"], row["version"]) == (
        "我去！",
        "I'm going!",
        "accepted",
        sentence["version"] + 1,
    )


def test_imported_html_is_escaped_in_the_embedded_batch(
    client: FlaskClient, xml_export: bytes
) -> None:
    data = xml_export.replace(
        b"pronoun I; me", b"pronoun &lt;/script&gt;&lt;script&gt;alert(1)"
    )
    import_export(client, data)
    page = client.get("/study").data.decode()
    assert "<script>alert(1)" not in page
    assert "</script><script>" not in page
    payload = next(card for card in study_cards(client) if card["word"] == "我")
    assert payload["senses"][0]["meaning"] == "</script><script>alert(1)"
