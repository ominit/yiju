from typing import TYPE_CHECKING

from conftest import (
    SAMPLE,
    add,
    csrf,
    database,
    page_context,
    set_strength,
    states,
)
from fsrs import Card, State

from yiju.core.scheduling import now_utc
from yiju.storage.queue import overview
from yiju.storage.sentences import statuses
from yiju.storage.stats import activity

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient


def keys(client: FlaskClient, url: str) -> list[str]:
    return [item.key for item in page_context(client, url)["items"]]


def test_words_are_filtered_by_state_and_backlog(
    client: FlaskClient, app: Flask
) -> None:
    with database(app) as db:
        add(db, SAMPLE)
        set_strength(db, "我")
        set_strength(db, "去", stability=2, learned=False)
    assert keys(client, "/library?filter=learned") == ["我"]
    assert keys(client, "/library?filter=learning") == ["去"]
    assert keys(client, "/library?filter=locked") == ["苹果"]
    assert sorted(keys(client, "/library?filter=unlocked")) == ["中文", "学习"]
    assert keys(client, "/library?filter=backlog") == ["苹果"]
    counts = page_context(client, "/library")["counts"]
    assert counts["all"] == 5
    assert counts["backlog"] == 1
    assert keys(client, "/library?q=apple") == ["苹果"]
    assert keys(client, "/library?q=pingguo") == ["苹果"]
    assert keys(client, "/library")[0] == "去"


def test_sentences_are_filtered_by_state(client: FlaskClient, app: Flask) -> None:
    with database(app) as db:
        add(db, SAMPLE)
    assert keys(client, "/sentences?filter=eligible") == ["1"]
    assert keys(client, "/sentences?filter=locked") == ["2"]
    detail = page_context(client, "/sentences/2")
    assert detail["details"]["tokens"] == [
        {"text": "我", "status": "unlocked"},
        {"text": "学习", "status": "locked"},
        {"text": "中文", "status": "locked"},
    ]
    assert "3 words are not learned yet" in detail["details"]["reason"]


def test_sentences_are_learning_until_both_cards_are_strong(
    client: FlaskClient, app: Flask
) -> None:
    def study(card_type: str, stability: float) -> None:
        memory = Card(card_id=0, state=State.Review, stability=stability, due=now)
        db.execute(
            "UPDATE cards SET scheduling = ?, reviews = reviews + 1 "
            "WHERE sentence_id = 1 AND card_type = ?",
            (memory.to_json(), card_type),
        )
        db.commit()

    now = now_utc()
    with database(app) as db:
        add(db, SAMPLE)
        set_strength(db, "我")
        set_strength(db, "去")
        assert statuses(db)[1] == "unlocked"
        study("recognition", 20)
        assert statuses(db)[1] == "learning"
        study("production", 20)
        assert statuses(db)[1] == "learned"
        assert overview(db, now)["sentences_learned"] == 1
        assert keys(client, "/sentences?filter=learned") == ["1"]
        study("production", 2)
        assert statuses(db)[1] == "learning"
        assert keys(client, "/sentences?filter=learning") == ["1"]
    detail = page_context(client, "/sentences/1")
    assert "Learned once both cards reach 7 days" in detail["details"]["reason"]


def test_word_details_explain_the_lock_and_allow_a_manual_unlock(
    client: FlaskClient, app: Flask
) -> None:
    with database(app) as db:
        add(db, SAMPLE)
    detail = page_context(client, "/library/words/苹果")
    assert detail["details"]["reason"] == "No sentence contains this word yet."
    assert detail["details"]["readings"][0].pinyin == "píngguǒ"
    version = detail["item"].row["version"]
    response = client.post(
        "/words/苹果/unlock",
        data={"csrf": csrf(client), "version": version, "manual": "1"},
        headers={"X-Fragment": "1"},
    )
    assert response.status_code == 200
    assert "Undo manual unlock" in response.get_data(as_text=True)
    with database(app) as db:
        assert states(db)["苹果"] == "unlocked"
    detail = page_context(client, "/library/words/去")
    assert detail["details"]["reason"] == "Unlocked by 1 eligible sentence."


def test_seen_words_show_their_strength_and_can_be_paused(
    client: FlaskClient, app: Flask
) -> None:
    with database(app) as db:
        add(db, SAMPLE)
        set_strength(db, "去", stability=5, learned=False)
    detail = page_context(client, "/library/words/去")
    assert detail["details"]["strength"]["stability"] == 5
    assert detail["details"]["strength"]["days"] == 3
    html = client.get("/library/words/去", headers={"X-Fragment": "1"}).get_data(
        as_text=True
    )
    assert "Learned at 7.0 days of strength" in html
    response = client.post(
        "/words/去/pause",
        data={
            "csrf": csrf(client),
            "version": detail["item"].row["version"],
            "paused": "1",
        },
        headers={"X-Fragment": "1"},
    )
    assert "Resume" in response.get_data(as_text=True)
    assert keys(client, "/library?filter=hidden") == ["去"]


def test_sentence_edits_and_skips_from_the_library(
    client: FlaskClient, app: Flask
) -> None:
    with database(app) as db:
        add(db, SAMPLE)
    token = csrf(client)
    version = page_context(client, "/sentences/1")["item"].row["version"]
    response = client.post(
        "/sentences/1",
        data={
            "csrf": token,
            "version": version,
            "pinyin": "Wǒ qù!",
            "translation": "Off I go.",
        },
        headers={"X-Fragment": "1"},
    )
    assert "Off I go." in response.get_data(as_text=True)
    version = page_context(client, "/sentences/1")["item"].row["version"]
    client.post(
        "/sentences/1",
        data={"csrf": token, "version": version, "hidden": "1"},
        headers={"X-Fragment": "1"},
    )
    assert keys(client, "/sentences?filter=hidden") == ["1"]
    with database(app) as db:
        assert states(db)["我"] == "locked"


def test_library_handles_empty_states_and_large_page_numbers(
    client: FlaskClient,
) -> None:
    page = client.get("/library?page=999")
    assert page.status_code == 200
    assert "Add sentences" in page.get_data(as_text=True)
    assert client.get("/library?filter=bogus").status_code == 400
    assert client.get("/sentences/abc", headers={"X-Fragment": "1"}).status_code == 404


def test_activity_counts_new_words_once(client: FlaskClient, app: Flask) -> None:
    with database(app) as db:
        add(db, SAMPLE)
    token = csrf(client)
    batch = client.get("/api/study").json
    assert batch is not None
    for card in batch["cards"]:
        client.post(
            "/api/reviews",
            json={"card": card["id"], "version": card["version"], "rating": 3},
            headers={"X-CSRF-Token": token},
        )
    with database(app) as db:
        stats = activity(db, now_utc())
    assert stats["introduced"] == 2
    assert stats["reviews"] == 4
