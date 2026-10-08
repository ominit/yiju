import io
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Never

import pytest
from conftest import csrf, seed_known_word
from flask import Flask
from flask.testing import FlaskClient

from yiju.core.definitions import recall_prompt
from yiju.storage.database import connect, initialize
from yiju.storage.importing import import_xml
from yiju.storage.learning import known_sentences, review_card, undo_review
from yiju.storage.queue import overview, study_queue


def test_reverse_word_schedule_is_independent_and_both_reviews_share_progress(
    connection: sqlite3.Connection, xml_export: bytes
) -> None:
    import_xml(connection, xml_export, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    cards = {
        row["direction"]: dict(row)
        for row in connection.execute(
            "SELECT * FROM cards WHERE word = '我' AND sentence_id IS NULL"
        )
    }
    reverse = cards["production"]
    review_card(connection, reverse["id"], reverse["version"], 3, now)
    assert (
        dict(
            connection.execute(
                "SELECT * FROM cards WHERE id = ?", (cards["recognition"]["id"],)
            ).fetchone()
        )
        == cards["recognition"]
    )
    before = dict(
        connection.execute("SELECT * FROM knowledge WHERE word = '我'").fetchone()
    )
    forward = cards["recognition"]
    review_card(connection, forward["id"], forward["version"], 3, now)
    assert (
        connection.execute(
            "SELECT success_days FROM knowledge WHERE word = '我'"
        ).fetchone()[0]
        == 1
    )
    latest = connection.execute("SELECT max(id) FROM reviews").fetchone()[0]
    undo_review(connection, latest)
    assert (
        dict(connection.execute("SELECT * FROM knowledge WHERE word = '我'").fetchone())
        == before
    )
    undo_review(connection, latest - 1)
    assert connection.execute("SELECT count(*) FROM knowledge").fetchone()[0] == 0
    seed_known_word(connection, "我", now)
    queue = study_queue(connection, now)
    assert any(row["id"] == reverse["id"] for row in queue)
    assert not any(row["id"] == forward["id"] for row in queue)
    import_xml(connection, xml_export, "again.xml")
    assert connection.execute("SELECT count(*) FROM cards").fetchone()[0] == 10


def test_sentence_directions_contribute_to_one_learned_count(
    connection: sqlite3.Connection, xml_export: bytes
) -> None:
    import_xml(connection, xml_export, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    seed_known_word(connection, "我", now)
    cards = {
        row["direction"]: row
        for row in connection.execute(
            "SELECT * FROM cards WHERE word = '我' AND sentence_id IS NOT NULL"
        )
    }
    sentence_id = cards["recognition"]["sentence_id"]
    for direction, days in [
        ("recognition", 0),
        ("production", 0),
        ("production", 1),
        ("recognition", 2),
    ]:
        card = connection.execute(
            "SELECT * FROM cards WHERE id = ?", (cards[direction]["id"],)
        ).fetchone()
        review_card(
            connection, card["id"], card["version"], 3, now + timedelta(days=days)
        )
        if days < 2:
            assert not known_sentences(connection, now + timedelta(days=days))
    later = now + timedelta(days=2)
    assert known_sentences(connection, later) == frozenset({sentence_id})
    assert overview(connection, later)["learned_sentences"] == 1
    reverse = connection.execute(
        "SELECT * FROM cards WHERE id = ?", (cards["production"]["id"],)
    ).fetchone()
    later = datetime.fromisoformat(reverse["due"])
    review_card(connection, reverse["id"], reverse["version"], 1, later)
    assert not known_sentences(connection, later)
    undo_review(
        connection, connection.execute("SELECT max(id) FROM reviews").fetchone()[0]
    )
    assert known_sentences(connection, later) == frozenset({sentence_id})


def test_reverse_sentence_still_appears_after_words_become_familiar(
    connection: sqlite3.Connection, xml_export: bytes
) -> None:
    import_xml(connection, xml_export, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    seed_known_word(connection, "我", now)
    forward = connection.execute(
        "SELECT * FROM cards WHERE word = '我' AND sentence_id IS NOT NULL "
        "AND direction = 'recognition'"
    ).fetchone()
    review_card(connection, forward["id"], forward["version"], 3, now)
    connection.execute("DELETE FROM knowledge WHERE word = '去'")
    connection.commit()
    seed_known_word(connection, "去", now)
    assert any(
        row["sentence_id"] == forward["sentence_id"]
        and row["direction"] == "production"
        for row in study_queue(connection, now)
    )


def test_reverse_cards_require_a_definition_without_answer_hints(
    connection: sqlite3.Connection, xml_export: bytes
) -> None:
    data = xml_export.replace(b"pronoun I; me", "pronoun see 我 [wo3]".encode())
    import_xml(connection, data, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    assert not any(
        row["word"] == "我"
        and row["sentence_id"] is None
        and row["direction"] == "production"
        for row in study_queue(connection, now)
    )
    senses = {
        "version": 1,
        "senses": [
            {"part_of_speech": "", "meaning": "variant of 我 [wo3]", "examples": []},
            {"part_of_speech": "pronoun", "meaning": "I; me", "examples": []},
        ],
    }
    assert recall_prompt(json.dumps(senses)) == "I; me"


def test_upgrade_adds_reverse_cards_without_changing_existing_progress(
    tmp_path: Path, xml_export: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "legacy.sqlite3"
    initialize(path)
    with connect(path) as db:
        import_xml(db, xml_export, "words.xml")
        now = datetime.now(UTC) + timedelta(seconds=1)
        card = db.execute(
            "SELECT * FROM cards WHERE word = '我' AND sentence_id IS NULL "
            "AND direction = 'recognition'"
        ).fetchone()
        review_card(db, card["id"], card["version"], 3, now)
        db.execute("UPDATE cards SET suspended = 1 WHERE word = '学习'")
        db.execute("DELETE FROM cards WHERE direction = 'production'")
        db.execute("ALTER TABLE cards DROP COLUMN direction")
        db.execute("PRAGMA user_version = 4")
        before = {row["id"]: dict(row) for row in db.execute("SELECT * FROM cards")}
        history = [tuple(row) for row in db.execute("SELECT * FROM reviews")]
        knowledge = [tuple(row) for row in db.execute("SELECT * FROM knowledge")]

    def unexpected_parsing(_text: str) -> Never:
        message = "Upgrading cards must not reparse dictionary text."
        raise AssertionError(message)

    monkeypatch.setattr("yiju.storage.database.parse_definition", unexpected_parsing)
    for _ in range(2):
        initialize(path)
        with connect(path) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 5
            assert db.execute("SELECT count(*) FROM cards").fetchone()[0] == 10
            for row in db.execute(
                "SELECT * FROM cards WHERE direction = 'recognition'"
            ):
                assert dict(row) == {**before[row["id"]], "direction": "recognition"}
            for row in db.execute("SELECT * FROM cards WHERE direction = 'production'"):
                assert row["reviews"] == 0
                assert row["suspended"] == int(row["word"] == "学习")
            assert [
                tuple(row) for row in db.execute("SELECT * FROM reviews")
            ] == history
            assert [
                tuple(row) for row in db.execute("SELECT * FROM knowledge")
            ] == knowledge
    with connect(path) as db:
        undo_review(db, db.execute("SELECT max(id) FROM reviews").fetchone()[0])
        assert db.execute("SELECT count(*) FROM knowledge").fetchone()[0] == 0


@pytest.mark.parametrize("sentence", [False, True])
def test_reverse_front_shows_english_and_reveal_has_the_chinese_answer(
    client: FlaskClient, app: Flask, xml_export: bytes, *, sentence: bool
) -> None:
    token = csrf(client)
    client.post(
        "/import", data={"csrf": token, "file": (io.BytesIO(xml_export), "words.xml")}
    )
    with connect(Path(app.config["DATABASE"])) as db:
        seed_known_word(db, "我")
        card = db.execute(
            "SELECT * FROM cards WHERE direction = 'production' AND word = ? "
            "AND (sentence_id IS NOT NULL) = ?",
            ("我" if sentence else "去", int(sentence)),
        ).fetchone()
    front = client.get(f"/study?card={card['id']}").data.decode()
    prompt = re.search(
        r'<p class="recall-prompt question-text"[^>]*>(.*?)</p>', front, re.DOTALL
    )
    assert prompt is not None
    assert prompt[1] == ("I am going." if sentence else "go")
    assert re.search(
        r'class="chinese question-text[^"]*"\s+data-reveal-answer\s+hidden', front
    )
    answer = client.get(f"/study?card={card['id']}&answer=1").data.decode()
    assert re.search(
        r'class="recall-prompt question-text"\s+data-hide-answer\s+hidden', answer
    )
    response = client.post(
        f"/review/{card['id']}",
        data={"csrf": token, "version": card["version"], "rating": 3},
        headers={"X-Yiju-Partial": "study"},
    )
    assert response.status_code == 200
    assert b"<!DOCTYPE" not in response.data


def test_library_does_not_duplicate_words_and_pauses_both_directions(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    token = csrf(client)
    client.post(
        "/import", data={"csrf": token, "file": (io.BytesIO(xml_export), "words.xml")}
    )
    assert client.get("/library").data.count(b'class="panel word-row"') == 3
    with connect(Path(app.config["DATABASE"])) as db:
        card = db.execute(
            "SELECT * FROM cards WHERE word = '去' AND sentence_id IS NULL "
            "AND direction = 'recognition'"
        ).fetchone()
    assert (
        client.post(
            f"/cards/{card['id']}/suspend",
            data={"csrf": token, "version": card["version"]},
        ).status_code
        == 303
    )
    with connect(Path(app.config["DATABASE"])) as db:
        assert [
            row[0]
            for row in db.execute(
                "SELECT suspended FROM cards WHERE word = '去' AND sentence_id IS NULL"
            )
        ] == [1, 1]
