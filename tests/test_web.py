import gzip
import io
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Never
from urllib.parse import parse_qs, urlsplit

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

from yiju.storage.database import connect, settings
from yiju.storage.learning import review_card
from yiju.web import create_app


def database(app: Flask) -> sqlite3.Connection:
    return connect(Path(app.config["DATABASE"]))


def sentence_card(client: FlaskClient, chinese: str) -> dict[str, Any] | None:
    return next(
        (
            card
            for card in study_cards(client)
            if card["kind"] == "sentence" and card["text"] == chinese
        ),
        None,
    )


def test_pages_render_after_import_and_sentence_edits_reach_study(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    token = csrf(client)
    response = import_export(client, xml_export, token)
    assert response.status_code == 303
    assert b"words.xml" in client.get(response.location).data
    with database(app) as db:
        seed_known_word(db, "我")
        sentence = db.execute(
            "SELECT * FROM sentences WHERE chinese = '我去。'"
        ).fetchone()
    for url in [
        "/",
        "/study",
        "/api/study",
        "/import",
        "/sentences",
        "/library",
        "/settings",
    ]:
        assert client.get(url).status_code == 200
    for url, path, query, key in [
        ("/library/words/去", "/library", "去", "去"),
        (f"/sentences/{sentence['id']}", "/sentences", "我去。", sentence["id"]),
    ]:
        redirect = client.get(url)
        assert redirect.status_code == 302
        location = urlsplit(redirect.location)
        assert location.path == path
        assert parse_qs(location.query) == {"q": [query], "open": [key]}
        opened = page_context(client, redirect.location)
        assert opened["opened"] == key
        assert opened["details"] is not None
        assert opened["items"][0].text == query
        fragment = page_context(client, url)
        assert fragment["item"].text == query
        assert fragment["details"]["cards"]
    for url in ["/library/words/missing", "/sentences/missing"]:
        assert client.get(url).status_code == 404
        missing = client.get(url, headers={"X-Fragment": "1"})
        assert missing.status_code == 404
        assert missing.json is not None
        assert missing.json["error"]
    response = client.post(
        f"/sentences/{sentence['id']}",
        data={
            "csrf": token,
            "version": 0,
            "chinese": sentence["chinese"],
            "pinyin": sentence["pinyin"],
            "translation": "I go.",
            "status": "accepted",
        },
    )
    assert response.status_code == 200
    assert "I go." in response.get_data(as_text=True)
    card = sentence_card(client, "我去。")
    assert card is not None
    assert card["translation"] == "I go."
    assert card["sentence"] == {
        "id": sentence["id"],
        "version": 1,
        "pinyin": sentence["pinyin"],
    }


@pytest.mark.parametrize(
    ("path", "title", "section"),
    [
        ("/", "Today · Yiju", "home"),
        ("/study", "Study · Yiju", "study"),
        ("/library", "Words · Library · Yiju", "library"),
        ("/sentences", "Sentences · Library · Yiju", "library"),
        ("/settings", "Settings · Yiju", "settings"),
        ("/import", "Import · Yiju", "library"),
    ],
)
def test_pages_have_titles_and_mark_the_current_section(
    client: FlaskClient, path: str, title: str, section: str
) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert f"<title>{title}</title>" in response.get_data(as_text=True)
    assert page_context(client, path)["section"] == section


def test_pages_compress_when_supported(client: FlaskClient) -> None:
    plain = client.get("/settings")
    compressed = client.get("/settings", headers={"Accept-Encoding": "gzip"})
    assert compressed.headers["Content-Encoding"] == "gzip"
    assert gzip.decompress(compressed.data) == plain.data
    assert len(compressed.data) < len(plain.data) / 2
    assert "Accept-Encoding" in compressed.vary
    excluded = client.get("/settings", headers={"Accept-Encoding": "gzip;q=0"})
    assert "Content-Encoding" not in excluded.headers
    backup = client.get("/backup", headers={"Accept-Encoding": "gzip"})
    assert "Content-Encoding" not in backup.headers


def test_errors_are_json_for_api_requests_and_html_for_pages(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    token = csrf(client)
    import_export(client, xml_export, token)
    data = {
        "csrf": token,
        "max_unknown": 2,
        "new_per_day": 42,
        "known_days": 4,
        "known_stability": 20,
        "retention": 0.9,
        "known_retrievability": 0.8,
    }
    saved = client.post("/settings", data=data, follow_redirects=True)
    assert b"Settings saved." in saved.data
    invalid = client.post("/settings", data={**data, "new_per_day": -1})
    assert invalid.status_code == 400
    assert invalid.mimetype == "text/html"
    assert b"new_per_day" in invalid.data
    invalid = client.post(
        "/settings",
        data={**data, "new_per_day": -1},
        headers={"Accept": "application/json"},
    )
    assert invalid.status_code == 400
    assert invalid.json is not None
    assert "new_per_day" in invalid.json["error"]
    expired = client.post(
        "/settings",
        data={**data, "csrf": "expired"},
        headers={"Accept": "application/json"},
    )
    assert expired.status_code == 400
    assert expired.json is not None
    assert "expired" in expired.json["error"]
    with database(app) as db:
        assert settings(db)["new_per_day"] == 42
        version = db.execute(
            "SELECT version FROM cards WHERE word = '去' AND sentence_id IS NULL "
            "AND direction = 'recognition'"
        ).fetchone()[0]
    stale = {"csrf": token, "version": version + 1}
    conflict = client.post("/words/去/pause", data=stale)
    assert conflict.status_code == 409
    assert b"<!DOCTYPE html>" in conflict.data
    conflict = client.post(
        "/words/去/pause", data=stale, headers={"Accept": "application/json"}
    )
    assert conflict.status_code == 409
    assert conflict.json is not None
    assert "changed" in conflict.json["error"]
    conflict = client.post("/words/去/pause", data=stale, headers={"X-Fragment": "1"})
    assert conflict.status_code == 409
    assert conflict.json is not None
    assert "changed" in conflict.json["error"]
    with database(app) as db:
        sentence = db.execute("SELECT * FROM sentences LIMIT 1").fetchone()
    invalid = client.post(
        f"/sentences/{sentence['id']}",
        data={"csrf": token, "version": sentence["version"], "chinese": "x"},
        headers={"X-Fragment": "1"},
    )
    assert invalid.status_code == 400
    assert invalid.json is not None
    assert invalid.json["error"]
    missing = client.get("/api/missing")
    assert missing.status_code == 404
    assert missing.json is not None


def test_settings_persist_across_clients_and_restarts_and_control_study(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    token = csrf(client)
    import_export(client, xml_export, token)
    values = {
        "max_unknown": 1,
        "new_per_day": 0,
        "known_days": 5,
        "known_stability": 12,
        "retention": 0.95,
        "known_retrievability": 0.85,
    }
    response = client.post(
        "/settings", data={"csrf": token, "include_zero": "on", **values}
    )
    assert response.status_code == 303
    with database(app) as db:
        assert settings(db) == {**values, "include_zero": True}
    restarted = create_app(Path(app.config["DATABASE"]))
    with restarted.test_client() as other:
        page = other.get("/settings").data.decode()
        assert 'name="new_per_day"' in page
        assert 'value="0"' in page
        assert 'value="0.95"' in page
        assert re.search(r'name="include_zero"[^>]*\bchecked', page)
        assert study_cards(other) == []
        response = other.post(
            "/settings", data={"csrf": csrf(other), **values, "new_per_day": 10}
        )
        assert response.status_code == 303
    with database(app) as db:
        assert settings(db) == {**values, "new_per_day": 10, "include_zero": False}
    assert len(study_cards(client)) == 3
    assert not re.search(
        r'name="include_zero"[^>]*\bchecked', client.get("/settings").data.decode()
    )


def test_sentences_are_active_by_default_and_can_be_fixed_or_skipped_in_study(
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
            "SELECT * FROM cards WHERE sentence_id = ?", (sentence["id"],)
        ).fetchone()
    assert sentence["status"] == "accepted"
    studied = sentence_card(client, "我去。")
    assert studied is not None
    assert studied["translation"] == "I am going."
    assert studied["sentence"] == {
        "id": sentence["id"],
        "version": 0,
        "pinyin": sentence["pinyin"],
    }
    data = {
        "csrf": token,
        "version": 0,
        "chinese": sentence["chinese"],
        "pinyin": sentence["pinyin"],
        "translation": "I go.",
    }
    saved = client.post(f"/sentences/{sentence['id']}", data=data)
    assert saved.status_code == 200
    assert "I go." in saved.get_data(as_text=True)
    studied = sentence_card(client, "我去。")
    assert studied is not None
    assert studied["translation"] == "I go."
    skip = f"/api/sentences/{sentence['id']}/skip"
    assert post_json(client, skip, {"version": 1}, token).json == {"ok": True}
    assert post_json(client, skip, {"version": 1}, token).status_code == 409
    assert post_json(client, skip, {}, token).status_code == 400
    assert (
        post_json(client, "/api/sentences/missing/skip", {"version": 0}, token)
    ).status_code == 404
    assert sentence_card(client, "我去。") is None
    assert rate(client, token, card).status_code == 409
    assert b"I go." in client.get("/sentences?filter=hidden").data
    assert b"I go." not in client.get("/sentences").data
    data.update(version=2, status="accepted")
    restored = client.post(f"/sentences/{sentence['id']}", data=data)
    assert restored.status_code == 200
    assert "Skip sentence" in restored.get_data(as_text=True)
    assert sentence_card(client, "我去。") is not None


@pytest.mark.parametrize("include_other_word", [False, True])
def test_sentence_answer_lists_every_word_and_marks_unfamiliar_ones(
    client: FlaskClient, app: Flask, xml_export: bytes, *, include_other_word: bool
) -> None:
    data = (
        xml_export.decode()
        .replace(
            '<headword charset="sc">我</headword>',
            '<headword charset="sc">工作</headword>',
        )
        .replace("wo3", "gong1zuo4")
        .replace("pronoun I; me", "noun work; job")
        .replace(
            "我去。 Wǒ qù. I am going.",
            "分配工作分配学习 Fēnpèi gōngzuò fēnpèi xuéxí assign work and study",
        )
    )
    if include_other_word:
        data = (
            data.replace(
                '<headword charset="sc">去</headword>',
                '<headword charset="sc">分配</headword>',
            )
            .replace("qu4", "fen1pei4")
            .replace("verb go", "verb assign; distribute")
        )
    import_export(client, data.encode())
    with database(app) as db:
        seed_known_word(db, "学习")
    card = sentence_card(client, "分配工作分配学习")
    if not include_other_word:
        assert card is None
        return
    assert card is not None
    assert card["tokens"] == [
        {"text": "分配", "unknown": True},
        {"text": "工作", "unknown": True},
        {"text": "分配", "unknown": True},
        {"text": "学习", "unknown": False},
    ]
    words = card["words"]
    assert isinstance(words, list)
    assert [(word["word"], word["unknown"]) for word in words] == [
        ("分配", True),
        ("工作", True),
        ("学习", False),
    ]
    assert words[0]["reading"] == [["分", "fēn"], ["配", "pèi"]]
    assert [sense["meaning"] for sense in words[0]["senses"]] == ["assign; distribute"]
    assert [sense["meaning"] for sense in words[1]["senses"]] == ["work; job"]
    assert not any(word["flagged"] for word in words)


def test_pages_read_stored_definitions_without_parsing_export_text(
    client: FlaskClient, xml_export: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_export(client, xml_export)

    def unexpected_parsing(_text: str) -> Never:
        message = "Pages must read the stored definition format."
        raise AssertionError(message)

    monkeypatch.setattr("yiju.core.definitions.parse_definition", unexpected_parsing)
    for url in ["/library", "/study", "/api/study", "/library?open=我"]:
        assert client.get(url).status_code == 200
    library = client.get("/library").data.decode()
    assert "I; me" in library
    assert "I am going." not in library
    for word in [
        client.get("/library/words/我", headers={"X-Fragment": "1"}),
        client.get("/library?open=我"),
    ]:
        assert word.status_code == 200
        text = word.get_data(as_text=True)
        assert "pronoun" in text
        assert "I; me" in text
        assert "I am going." in text


def test_learned_sentence_count_and_status_follow_reviews_and_undo(
    client: FlaskClient, app: Flask, xml_export: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = csrf(client)
    import_export(client, xml_export, token)
    now = datetime.now(UTC) + timedelta(seconds=1)
    with database(app) as db:
        seed_known_word(db, "我", now)
        card = db.execute(
            "SELECT * FROM cards WHERE sentence_id IS NOT NULL AND word = '我'"
        ).fetchone()
        for index in range(4):
            review_card(db, card["id"], card["version"], 3, now)
            card = db.execute(
                "SELECT * FROM cards WHERE id = ?", (card["id"],)
            ).fetchone()
            if index < 3:
                now = datetime.fromisoformat(card["due"])
        review_id = db.execute("SELECT max(id) FROM reviews").fetchone()[0]
    monkeypatch.setattr("yiju.web.pages.now_utc", lambda: now)

    def learned(other: FlaskClient) -> tuple[int, int, list[str]]:
        stats = page_context(other, "/")["stats"]
        items = page_context(other, "/sentences")["items"]
        return (
            int(stats["learned_sentences"]),
            int(stats["sentences"]),
            [item.text for item in items if item.state == "learned"],
        )

    assert learned(client) == (1, 2, ["我去。"])
    assert [
        item.text for item in page_context(client, "/sentences?filter=learned")["items"]
    ] == ["我去。"]
    restarted = create_app(Path(app.config["DATABASE"]))
    with restarted.test_client() as other:
        assert learned(other) == (1, 2, ["我去。"])
    assert (
        post_json(client, "/api/reviews/undo", {"review": review_id}, token)
    ).status_code == 200
    assert learned(client) == (0, 2, [])


def test_invalid_upload_and_csrf_do_not_change_database(
    client: FlaskClient, app: Flask
) -> None:
    assert (
        client.post(
            "/import", data={"file": (io.BytesIO(b"bad"), "file.xml")}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/import",
            data={"csrf": csrf(client), "file": (io.BytesIO(b"bad"), "file.xml")},
        ).status_code
        == 400
    )
    with connect(Path(app.config["DATABASE"])) as db:
        assert db.execute("SELECT count(*) FROM imports").fetchone()[0] == 0


def test_backup_contains_data_and_restart_preserves_session(
    client: FlaskClient, xml_export: bytes, tmp_path: Path
) -> None:
    client.post(
        "/import",
        data={"csrf": csrf(client), "file": (io.BytesIO(xml_export), "words.xml")},
    )
    response = client.get("/backup")
    assert response.status_code == 200
    backup = tmp_path / "backup.sqlite3"
    backup.write_bytes(response.data)
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT count(*) FROM entries").fetchone()[0] == 3


def test_imported_html_is_escaped(client: FlaskClient, xml_export: bytes) -> None:
    data = xml_export.replace(
        b"pronoun I; me", b"&lt;script&gt;alert(1)&lt;/script&gt;"
    )
    import_export(client, data)
    for url, headers in [
        ("/library", {}),
        ("/library?open=我", {}),
        ("/library/words/我", {"X-Fragment": "1"}),
    ]:
        response = client.get(url, headers=headers)
        assert response.status_code == 200
        assert b"<script>alert(1)</script>" not in response.data
        assert b"&lt;script&gt;" in response.data
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_unicode_csrf_and_invalid_settings_are_rejected(client: FlaskClient) -> None:
    assert client.post("/settings", data={"csrf": "错"}).status_code == 400
    assert (
        client.post(
            "/settings", data={"csrf": csrf(client), "retention": "nan"}
        ).status_code
        == 400
    )


def test_restart_preserves_data_and_secret(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    token = csrf(client)
    client.post(
        "/import", data={"csrf": token, "file": (io.BytesIO(xml_export), "words.xml")}
    )
    restarted = create_app(Path(app.config["DATABASE"]))
    assert restarted.secret_key == app.secret_key
    with restarted.test_client() as other:
        assert b"3" in other.get("/").data
        assert b"words.xml" in other.get("/imports/1").data
