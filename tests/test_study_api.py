import json
import re
from typing import TYPE_CHECKING, Any

import pytest
from conftest import (
    SAMPLE,
    add,
    csrf,
    database,
    page_context,
    post_json,
    rate,
    set_strength,
    study_cards,
)

from yiju.web import study

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

    from flask import Flask
    from flask.testing import FlaskClient


@pytest.fixture
def token(client: FlaskClient, app: Flask) -> str:
    with database(app) as db:
        add(db, SAMPLE)
    return csrf(client)


def unlock(app: Flask, *words: str, learned: bool = False) -> None:
    with database(app) as db:
        for word in words:
            set_strength(db, word, stability=10, learned=learned)


def sentence_card(client: FlaskClient, chinese: str) -> dict[str, Any]:
    return next(
        card
        for card in study_cards(client)
        if card["kind"] == "sentence" and card["text"] == chinese
    )


@pytest.mark.usefixtures("token")
def test_study_page_embeds_the_same_batch_as_the_api(client: FlaskClient) -> None:
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
    assert data["count"] == batch["count"] == 4
    assert data["counts"] == {"reviews": 0, "new": 4}
    assert {card["family"] for card in data["cards"]} == {"word:我", "word:去"}
    assert data["learnedAt"] == {"stability": 7.0, "days": 3}
    word = data["cards"][0]
    assert word["status"] == "unlocked"
    assert word["strength"] == {"stability": 0.0, "days": 0, "recall": None}


@pytest.mark.usefixtures("token")
def test_word_cards_carry_readings_and_a_masked_prompt(client: FlaskClient) -> None:
    card = next(
        card
        for card in study_cards(client)
        if card["word"] == "我" and card["direction"] == "recognition"
    )
    assert card["kind"] == "word"
    assert card["new"] is True
    assert card["audio"] == f"/audio/cards/{card['id']}"
    assert set(card["intervals"]) == {"1", "2", "3", "4"}
    assert card["reading"] == [["我", "wǒ"]]
    assert card["readings"][0]["senses"][0]["text"] == "I; me; my"
    assert card["prompt"] == ["I; me; my"]


@pytest.mark.usefixtures("token")
def test_sentence_cards_list_each_word_with_its_status(
    client: FlaskClient, app: Flask
) -> None:
    unlock(app, "我", "去")
    card = sentence_card(client, "我去。")
    assert card["word"] is None
    assert card["translation"] == "I am going."
    assert card["sentence"] == {
        "id": 1,
        "version": card["sentence"]["version"],
        "pinyin": "wǒ qù.",
        "translation": "I am going.",
    }
    assert card["translationParts"] == [["I am going.", ""]]
    assert card["tokens"] == [
        {"text": "我", "status": "learning"},
        {"text": "去", "status": "learning"},
    ]
    assert [word["word"] for word in card["words"]] == ["我", "去"]
    strength = card["words"][0]["strength"]
    assert strength["stability"] == 10.0
    assert strength["days"] == 3
    assert 0 < strength["recall"] <= 100
    assert card["reviews"] == 0
    assert card["stability"] is None
    assert card["recall"] is None


def test_ratings_send_missed_words_and_return_receipts(
    client: FlaskClient, app: Flask, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    unlock(app, "我", "去")
    card = sentence_card(client, "我去。")
    original = study.study_queue
    validated = []

    def targeted(
        connection: sqlite3.Connection, now: datetime, *, card_id: int | None = None
    ) -> list[sqlite3.Row]:
        validated.append(card_id)
        return original(connection, now, card_id=card_id)

    monkeypatch.setattr(study, "study_queue", targeted)
    response = rate(client, token, card, 1, ["去"])
    assert response.status_code == 200
    assert response.json is not None
    receipt = response.json["review"]
    assert validated == [card["id"]]
    assert rate(client, token, card, 1, ["去"]).json == response.json
    assert rate(client, token, card, 3).status_code == 409
    with database(app) as db:
        roles = dict(
            db.execute(
                "SELECT word, role FROM review_words WHERE review_id = ?", (receipt,)
            ).fetchall()
        )
    assert roles == {"去": "marked"}
    monkeypatch.setattr(study, "study_queue", original)
    assert (client.get("/api/study").json or {}).get("lastReview") == receipt
    assert post_json(client, "/api/reviews/undo", {"review": receipt}, token).json == {
        "ok": True
    }
    assert (
        post_json(client, "/api/reviews/undo", {"review": receipt}, token).status_code
        == 409
    )


def test_invalid_rating_requests_return_json_errors(
    client: FlaskClient, app: Flask, token: str
) -> None:
    card = study_cards(client)[0]
    payload = {"card": card["id"], "version": card["version"], "rating": 3}
    for response in [
        client.post("/api/reviews", json=payload),
        post_json(client, "/api/reviews", {**payload, "rating": 5}, token),
        post_json(client, "/api/reviews", {**payload, "version": "0"}, token),
        post_json(client, "/api/reviews", {**payload, "card": "1"}, token),
        post_json(client, "/api/reviews", {**payload, "marked": "去"}, token),
        client.post("/api/reviews", data="[]", headers={"X-CSRF-Token": token}),
        post_json(client, "/api/reviews/undo", {"review": True}, token),
    ]:
        assert response.status_code == 400
        assert response.json is not None
        assert response.json["error"]
    unlock(app, "我", "去")
    sentence = sentence_card(client, "我去。")
    marked = rate(client, token, sentence, 1, ["苹果"])
    assert marked.status_code == 400
    assert marked.json is not None
    assert "Only words" in marked.json["error"]
    with database(app) as db:
        assert db.execute("SELECT count(*) FROM reviews").fetchone()[0] == 0


def test_locked_words_cannot_be_rated(
    client: FlaskClient, app: Flask, token: str
) -> None:
    with database(app) as db:
        locked = db.execute(
            "SELECT * FROM cards WHERE word = '学习' AND card_type = 'recognition'"
        ).fetchone()
    response = rate(client, token, locked)
    assert response.status_code == 409


def test_sentence_edits_in_study_return_the_updated_card(
    client: FlaskClient, app: Flask, token: str
) -> None:
    unlock(app, "我", "去")
    card = sentence_card(client, "我去。")
    sentence = card["sentence"]
    assert isinstance(sentence, dict)
    url = f"/api/sentences/{sentence['id']}"
    payload = {
        "card": card["id"],
        "version": sentence["version"],
        "pinyin": "Wǒ qù!",
        "translation": "I'm going!",
    }
    for invalid in [
        {**payload, "translation": " "},
        {**payload, "version": "0"},
        {**payload, "card": None},
        {**payload, "pinyin": 1},
    ]:
        response = post_json(client, url, invalid, token)
        assert response.status_code == 400
    saved = post_json(client, url, payload, token)
    assert saved.status_code == 200
    assert saved.json is not None
    updated = saved.json["card"]
    assert updated["translation"] == "I'm going!"
    assert updated["sentence"]["pinyin"] == "Wǒ qù!"
    assert updated["reading"] == [["我", "Wǒ"], ["去", "qù"], ["。", ""]]
    assert post_json(client, url, payload, token).status_code == 409


def test_skipping_a_sentence_removes_it_from_study(
    client: FlaskClient, app: Flask, token: str
) -> None:
    unlock(app, "我", "去")
    card = sentence_card(client, "我去。")
    sentence = card["sentence"]
    assert isinstance(sentence, dict)
    response = post_json(
        client,
        f"/api/sentences/{sentence['id']}/skip",
        {"version": sentence["version"]},
        token,
    )
    assert response.json == {"ok": True}
    assert all(card["kind"] == "word" for card in study_cards(client))


@pytest.mark.usefixtures("token")
def test_audio_preference_does_not_change_study_or_overview(
    client: FlaskClient,
) -> None:
    batch = client.get("/api/study").json
    assert batch is not None
    stats = page_context(client, "/")["stats"]
    for value in ["off", "on"]:
        client.set_cookie("yiju-audio", value)
        muted = client.get("/api/study").json
        assert muted is not None
        assert muted["count"] == batch["count"]
        assert page_context(client, "/")["stats"] == stats


def test_text_is_escaped_in_the_embedded_batch(client: FlaskClient, app: Flask) -> None:
    with database(app) as db:
        add(db, "我去。 | </script><script>alert(1)\n")
        set_strength(db, "我", stability=10, learned=False)
        set_strength(db, "去", stability=10, learned=False)
    page = client.get("/study").data.decode()
    assert "</script><script>" not in page
    card = sentence_card(client, "我去。")
    assert card["translation"] == "</script><script>alert(1)"
