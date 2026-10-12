import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from conftest import csrf, import_export, page_context, seed_known_word
from flask import Flask
from flask.testing import FlaskClient

from yiju.core.definitions import recall_prompt
from yiju.storage.database import connect
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
    queue = study_queue(connection, now, card_id=reverse["id"])
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
    reverse = connection.execute(
        "SELECT * FROM cards WHERE sentence_id = ? AND direction = 'production'",
        (forward["sentence_id"],),
    ).fetchone()
    assert not study_queue(connection, now, card_id=reverse["id"])
    assert study_queue(connection, now + timedelta(days=1), card_id=reverse["id"])


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


def test_library_does_not_duplicate_words_and_pauses_both_directions(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    token = csrf(client)
    import_export(client, xml_export, token)
    assert len(page_context(client, "/library")["items"]) == 3
    with connect(Path(app.config["DATABASE"])) as db:
        card = db.execute(
            "SELECT * FROM cards WHERE word = '去' AND sentence_id IS NULL "
            "AND direction = 'recognition'"
        ).fetchone()
    detail = page_context(client, "/library/words/去")
    assert detail["item"].row["version"] == card["version"]
    assert [row["label"] for row in detail["details"]["cards"]] == ["Read", "Write"]
    paused = client.post(
        "/words/去/pause", data={"csrf": token, "version": card["version"]}
    )
    assert paused.status_code == 200
    assert "Resume" in paused.get_data(as_text=True)
    with connect(Path(app.config["DATABASE"])) as db:
        assert [
            tuple(row)
            for row in db.execute(
                "SELECT suspended, version FROM cards "
                "WHERE word = '去' AND sentence_id IS NULL"
            )
        ] == [(1, 1)] * 2
        assert not db.execute(
            "SELECT count(*) FROM cards WHERE word = '去' AND sentence_id IS NOT NULL "
            "AND suspended = 1"
        ).fetchone()[0]
    detail = page_context(client, "/library/words/去")
    assert detail["item"].hidden
    assert all(row["suspended"] for row in detail["details"]["cards"])
