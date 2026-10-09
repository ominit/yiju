import io
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import csrf, seed_known_word
from flask import Flask
from flask.testing import FlaskClient
from fsrs import Card, State

from yiju.core.progress import learning_progress
from yiju.storage.database import DEFAULT_SETTINGS, connect
from yiju.storage.importing import import_xml
from yiju.storage.learning import review_card, undo_review
from yiju.storage.progress import activity, sentence_success_days
from yiju.web.library import Library


def test_progress_matches_learned_thresholds_and_fades_with_recall() -> None:
    now = datetime.now(UTC)
    memory = Card(
        state=State.Review, stability=10, difficulty=3, due=now, last_review=now
    )
    assert learning_progress(None, 0, DEFAULT_SETTINGS, now).state == "new"
    partial = learning_progress(memory.to_json(), 1, DEFAULT_SETTINGS, now)
    assert partial.state == "learning"
    assert partial.percent == 33
    learned = learning_progress(memory.to_json(), 3, DEFAULT_SETTINGS, now)
    assert learned.percent == 100
    forgotten = learning_progress(
        memory.to_json(), 3, DEFAULT_SETTINGS, now + timedelta(days=100)
    )
    assert forgotten.percent < 100
    assert forgotten.state == "learning"
    assert learning_progress(memory.to_json(), 0, DEFAULT_SETTINGS, now).percent == 0
    stricter = {**DEFAULT_SETTINGS, "known_days": 6}
    assert learning_progress(memory.to_json(), 3, stricter, now).percent == 50


def test_sentence_progress_counts_days_once_and_contributes_to_word_progress(
    connection: sqlite3.Connection, xml_export: bytes
) -> None:
    import_xml(connection, xml_export, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    seed_known_word(connection, "我", now)
    cards = connection.execute(
        "SELECT * FROM cards WHERE word = '我' AND sentence_id IS NOT NULL"
    ).fetchall()
    for card in cards:
        review_card(connection, card["id"], card["version"], 3, now)
    assert sentence_success_days(connection) == {cards[0]["sentence_id"]: 1}
    library = Library(connection, now)
    word = next(item for item in library.words() if item.text == "去")
    sentence = next(item for item in library.sentences() if item.text == "我去。")
    assert word.state == "learning"
    assert word.reviews == 3
    assert word.progress.days == 1
    assert sentence.progress.days == 1
    assert sentence.state == "learning"
    latest = connection.execute(
        "SELECT * FROM cards WHERE id = ?", (cards[0]["id"],)
    ).fetchone()
    later = datetime.fromisoformat(latest["due"])
    review_id = review_card(connection, latest["id"], latest["version"], 1, later)
    assert not sentence_success_days(connection)
    sentence = next(
        item for item in Library(connection, later).sentences() if item.text == "我去。"
    )
    assert sentence.progress.percent == 0
    undo_review(connection, review_id)
    assert sentence_success_days(connection) == {cards[0]["sentence_id"]: 1}


def test_library_search_filters_and_pause_keep_browsing_context(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    token = csrf(client)
    client.post(
        "/import", data={"csrf": token, "file": (io.BytesIO(xml_export), "words.xml")}
    )
    with connect(Path(app.config["DATABASE"])) as db:
        seed_known_word(db, "我")
        card = db.execute(
            "SELECT * FROM cards WHERE word = '去' AND sentence_id IS NULL "
            "AND direction = 'recognition'"
        ).fetchone()
    learned = client.get("/library?filter=learned").data.decode()
    assert learned.count('class="library-entry"') == 1
    assert 'data-entry="我"' in learned
    assert "Learning progress 100 percent" in learned
    search = client.get("/library?q=go").data.decode()
    assert search.count('class="library-entry"') == 1
    assert 'data-entry="去"' in search
    pronunciation = client.get("/library?q=xue").data.decode()
    assert pronunciation.count('class="library-entry"') == 1
    assert 'data-entry="学习"' in pronunciation
    sentences = client.get("/sentences?q=I%20am%20going").data.decode()
    assert sentences.count('class="library-entry"') == 1
    assert "我去。" in sentences
    destination = "/library?filter=new&q=go&sort=progress&page=1"
    paused = client.post(
        f"/cards/{card['id']}/suspend",
        data={"csrf": token, "version": card["version"], "library_return": destination},
    )
    assert paused.location == destination
    assert 'data-entry="去"' not in client.get("/library").data.decode()
    assert 'data-entry="去"' in client.get("/library?filter=hidden").data.decode()
    assert client.get("/library?filter=invalid").status_code == 400
    assert client.get("/sentences?sort=invalid").status_code == 400
    assert (
        "No words match these filters"
        in client.get("/library?q=nonexistent").data.decode()
    )
    resumed = client.post(
        f"/cards/{card['id']}/suspend",
        data={
            "csrf": token,
            "version": card["version"] + 1,
            "library_return": "https://example.com/library",
        },
    )
    assert resumed.location == "/library"


def test_activity_counts_sentence_words_once_and_undo_reverses_totals(
    connection: sqlite3.Connection, xml_export: bytes
) -> None:
    import_xml(connection, xml_export, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    empty = activity(connection, now)
    assert empty["reviews"] == 0
    assert empty["recall"] is None
    assert empty["rate"] == 0
    card = connection.execute(
        "SELECT * FROM cards WHERE word = '我' AND sentence_id IS NULL "
        "AND direction = 'recognition'"
    ).fetchone()
    review_card(connection, card["id"], card["version"], 1, now)
    card = connection.execute(
        "SELECT * FROM cards WHERE id = ?", (card["id"],)
    ).fetchone()
    now = datetime.fromisoformat(card["due"])
    review_card(connection, card["id"], card["version"], 3, now)
    reverse = connection.execute(
        "SELECT * FROM cards WHERE word = '我' AND sentence_id IS NULL "
        "AND direction = 'production'"
    ).fetchone()
    review_card(connection, reverse["id"], reverse["version"], 2, now)
    connection.execute("DELETE FROM knowledge WHERE word='我'")
    connection.commit()
    seed_known_word(connection, "我", now)
    sentence = connection.execute(
        "SELECT * FROM cards WHERE word = '我' AND sentence_id IS NOT NULL "
        "AND direction = 'recognition'"
    ).fetchone()
    review_id = review_card(connection, sentence["id"], sentence["version"], 3, now)
    stats = activity(connection, now)
    assert stats["reviews"] == 4
    assert stats["study_days"] == 1
    assert stats["introduced"] == 2
    assert stats["rate"] == 2
    assert stats["recall"] == 100
    undo_review(connection, review_id)
    stats = activity(connection, now)
    assert stats["reviews"] == 3
    assert stats["introduced"] == 1
    assert stats["rate"] == 1
    assert activity(connection, now + timedelta(days=31))["reviews"] == 0


@pytest.mark.parametrize("path", ["/library", "/sentences"])
def test_library_has_empty_states_and_accepts_large_page_numbers(
    client: FlaskClient, xml_export: bytes, path: str
) -> None:
    assert client.get(path).status_code == 200
    client.post(
        "/import",
        data={"csrf": csrf(client), "file": (io.BytesIO(xml_export), "words.xml")},
    )
    assert client.get(path + "?page=999999999").data.count(b'class="library-entry"') > 0


def test_search_handles_entries_without_meanings(
    client: FlaskClient, xml_export: bytes
) -> None:
    data = (
        xml_export.decode()
        .replace("verb study\n我学习中文。 Wǒ xuéxí Zhōngwén. I study Chinese.", "")
        .encode()
    )
    client.post(
        "/import", data={"csrf": csrf(client), "file": (io.BytesIO(data), "words.xml")}
    )
    assert client.get("/library?q=go").status_code == 200
    result = client.get("/library?q=学习")
    assert result.status_code == 200
    assert b"No definition" in result.data


def test_library_examples_load_separately_and_include_every_dictionary_entry(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    with connect(Path(app.config["DATABASE"])) as db:
        import_xml(db, xml_export, "words.xml")
        import_xml(
            db,
            xml_export.replace(
                b"I am going.",
                b"I am leaving. &lt;script&gt;alert(1)&lt;/script&gt;",
            ).replace(b'dictid="TEST"', b'dictid="OTHER"'),
            "more.xml",
        )
        entry_id = db.execute(
            "SELECT id FROM entries WHERE word = '我' ORDER BY rowid"
        ).fetchone()["id"]
    page = client.get("/library").data
    url = f"/library/entries/{entry_id}/examples"
    assert f'data-library-examples="{url}"'.encode() in page
    assert b"I am going." not in page
    assert b"I am leaving." not in page
    assert b"Examples" in page
    examples = client.get(url, headers={"X-Yiju-Partial": "examples"})
    assert examples.status_code == 200
    assert examples.data.count(b'class="dictionary-example"') == 2
    assert b"I am going." in examples.data
    assert b"I am leaving." in examples.data
    assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in examples.data
    assert b"<script>" not in examples.data
    assert 'class="syllable"' in examples.text
    assert b"<!DOCTYPE" not in examples.data
    assert examples.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in examples.headers["Content-Security-Policy"]
    assert client.get("/library/entries/missing/examples").status_code == 404


def test_library_word_cards_do_not_load_sentence_schedules(
    connection: sqlite3.Connection, xml_export: bytes
) -> None:
    import_xml(connection, xml_export, "words.xml")
    statements = []
    connection.set_trace_callback(statements.append)
    library = Library(connection, datetime.now(UTC))
    words = library.words()
    assert all(len(word.cards) == 3 for word in words)
    card_queries = [query for query in statements if "SELECT * FROM cards" in query]
    assert len(card_queries) == 1
    assert "WHERE sentence_id IS NULL" in card_queries[0]
    sentences = library.sentences()
    assert all(len(sentence.cards) == 3 for sentence in sentences)
