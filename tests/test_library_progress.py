import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import csrf, import_export, page_context, seed_known_word
from flask import Flask
from flask.testing import FlaskClient
from fsrs import Card, State

from yiju.core.progress import learning_progress
from yiju.storage.database import DEFAULT_SETTINGS, connect
from yiju.storage.importing import import_xml
from yiju.storage.learning import review_card, undo_review
from yiju.storage.progress import activity, sentence_success_days
from yiju.web.library import PAGE_SIZE, Library


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
    assert word.reviews == 2
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


def listed(client: FlaskClient, url: str) -> list[str]:
    return [item.text for item in page_context(client, url)["items"]]


def test_library_search_filters_and_pause_toggles_word(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    token = csrf(client)
    import_export(client, xml_export, token)
    with connect(Path(app.config["DATABASE"])) as db:
        seed_known_word(db, "我")
        card = db.execute(
            "SELECT * FROM cards WHERE word = '去' AND sentence_id IS NULL "
            "AND direction = 'recognition'"
        ).fetchone()
    assert listed(client, "/library?filter=learned") == ["我"]
    assert page_context(client, "/library/words/我")["item"].state == "learned"
    assert listed(client, "/library?q=go") == ["去"]
    assert listed(client, "/library?q=xue") == ["学习"]
    assert listed(client, "/library?q=XUÉ") == ["学习"]
    assert listed(client, "/library?sort=length") == [
        "去",
        "我",
        "学习",
    ]
    assert listed(client, "/library?sort=recent") == [
        "学习",
        "去",
        "我",
    ]
    assert listed(client, "/sentences?q=I%20am%20going") == ["我去。"]
    paused = client.post(
        "/words/去/pause", data={"csrf": token, "version": card["version"]}
    )
    assert paused.status_code == 200
    assert "Resume" in paused.get_data(as_text=True)
    assert "去" not in listed(client, "/library")
    assert listed(client, "/library?filter=hidden") == ["去"]
    assert client.get("/library?filter=invalid").status_code == 400
    assert client.get("/sentences?sort=invalid").status_code == 400
    assert client.get("/sentences?filter=flagged").status_code == 400
    assert listed(client, "/library?q=nonexistent") == []
    stale = client.post(
        "/words/去/pause", data={"csrf": token, "version": card["version"]}
    )
    assert stale.status_code == 409
    resumed = client.post(
        "/words/去/pause", data={"csrf": token, "version": card["version"] + 1}
    )
    assert resumed.status_code == 200
    assert "Pause" in resumed.get_data(as_text=True)
    assert "Resume" not in resumed.get_data(as_text=True)
    assert "去" in listed(client, "/library")


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
    empty = page_context(client, path)
    assert empty["items"] == []
    assert empty["total"] == 0
    import_export(client, xml_export)
    last = page_context(client, path + "?page=999999999")
    assert last["items"]
    assert last["page"] == last["pages"] == 1


def test_library_paginates_large_lists(
    client: FlaskClient,
) -> None:
    cards = "".join(
        '<card language="chinese"><entry><headword charset="sc">'
        f'{chr(0x4E00 + index)}</headword><pron type="hypy">zi4</pron>'
        f"<defn>noun word {index}</defn></entry></card>"
        for index in range(PAGE_SIZE + 5)
    )
    import_export(
        client,
        f'<plecoflash formatversion="2"><cards>{cards}</cards></plecoflash>'.encode(),
    )
    first = listed(client, "/library?sort=recent")
    second = listed(client, "/library?sort=recent&page=2")
    assert len(first) == PAGE_SIZE
    assert len(second) == 5
    assert not set(first) & set(second)
    context = page_context(client, "/library?page=99")
    assert (context["page"], context["pages"], context["total"]) == (
        2,
        2,
        PAGE_SIZE + 5,
    )


def test_search_handles_entries_without_meanings(
    client: FlaskClient, xml_export: bytes
) -> None:
    data = (
        xml_export.decode()
        .replace("verb study\n我学习中文。 Wǒ xuéxí Zhōngwén. I study Chinese.", "")
        .encode()
    )
    import_export(client, data)
    assert listed(client, "/library?q=go") == ["去"]
    assert listed(client, "/library?q=学习") == ["学习"]
    detail = page_context(client, "/library/words/学习")
    assert not any(sense.meaning for sense in detail["details"]["definition"].senses)


def test_word_page_shows_examples_from_every_dictionary_entry(
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
    assert listed(client, "/library").count("我") == 1
    assert b"I am going." not in client.get("/library").data
    context = page_context(client, "/library/words/我")
    assert [
        example.translation
        for sense in context["details"]["definition"].senses
        for example in sense.examples
    ] == ["I am going.", "I am leaving. <script>alert(1)</script>"]
    for detail in [
        client.get("/library/words/我", headers={"X-Fragment": "1"}),
        client.get("/library?open=我"),
    ]:
        assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in detail.data
        assert b"<script>alert" not in detail.data


def test_library_word_cards_do_not_load_sentence_schedules(
    connection: sqlite3.Connection, xml_export: bytes
) -> None:
    import_xml(connection, xml_export, "words.xml")
    statements = []
    connection.set_trace_callback(statements.append)
    library = Library(connection, datetime.now(UTC))
    words = library.words()
    assert all(len(word.cards) == 2 for word in words)
    card_queries = [query for query in statements if "SELECT * FROM cards" in query]
    assert len(card_queries) == 1
    assert "WHERE sentence_id IS NULL" in card_queries[0]
    sentences = library.sentences()
    assert all(len(sentence.cards) == 2 for sentence in sentences)
