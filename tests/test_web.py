import io
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Never

import pytest
from conftest import csrf, seed_known_word
from flask import Flask
from flask.testing import FlaskClient

from yiju.storage.database import connect, settings
from yiju.storage.learning import review_card
from yiju.web import create_app


def test_full_import_preview_review_and_undo(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    token = csrf(client)
    response = client.post(
        "/import", data={"csrf": token, "file": (io.BytesIO(xml_export), "words.xml")}
    )
    assert response.status_code == 303
    with connect(Path(app.config["DATABASE"])) as db:
        seed_known_word(db, "我")
    assert b"Import result" in client.get(response.location).data
    for url in [
        "/",
        "/study",
        "/study?answer=1",
        "/sentences",
        "/library",
        "/settings",
    ]:
        assert client.get(url).status_code == 200
    with connect(Path(app.config["DATABASE"])) as db:
        sentence = db.execute(
            "SELECT * FROM sentences WHERE chinese = '我去。'"
        ).fetchone()
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
    assert response.status_code == 303
    assert b"I go." in client.get("/study?answer=1").data

    with connect(Path(app.config["DATABASE"])) as db:
        card = db.execute(
            "SELECT * FROM cards WHERE sentence_id = ?", (sentence["id"],)
        ).fetchone()
    response = client.post(
        f"/review/{card['id']}",
        data={"csrf": token, "version": card["version"], "rating": 3},
    )
    assert response.status_code == 303
    assert (
        client.post(
            f"/review/{card['id']}",
            data={"csrf": token, "version": card["version"], "rating": 3},
        ).status_code
        == 409
    )
    with connect(Path(app.config["DATABASE"])) as db:
        review_id = db.execute("SELECT id FROM reviews").fetchone()[0]
    assert (
        client.post(
            "/review/undo", data={"csrf": token, "review_id": review_id}
        ).status_code
        == 303
    )


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
    client.post(
        "/import", data={"csrf": csrf(client), "file": (io.BytesIO(data), "words.xml")}
    )
    response = client.get("/library")
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


def test_settings_persist_across_clients_and_restarts_and_control_study(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    token = csrf(client)
    client.post(
        "/import", data={"csrf": token, "file": (io.BytesIO(xml_export), "words.xml")}
    )
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
    with connect(Path(app.config["DATABASE"])) as db:
        assert settings(db) == {**values, "include_zero": True}
    restarted = create_app(Path(app.config["DATABASE"]))
    with restarted.test_client() as other:
        page = other.get("/settings").data.decode()
        assert 'value="0"' in page
        assert 'value="0.95"' in page
        assert re.search(r'name="include_zero"\s+type="checkbox"\s+checked', page)
        assert 'id="reveal-answer"' not in other.get("/study").data.decode()
        response = other.post(
            "/settings", data={"csrf": csrf(other), **values, "new_per_day": 10}
        )
        assert response.status_code == 303
    with connect(Path(app.config["DATABASE"])) as db:
        assert settings(db) == {**values, "new_per_day": 10, "include_zero": False}
    assert b'id="reveal-answer"' in client.get("/study").data


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
        assert b"words.xml" in other.get("/").data


def test_sentences_are_active_by_default_and_can_be_fixed_or_skipped_in_study(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    token = csrf(client)
    client.post(
        "/import", data={"csrf": token, "file": (io.BytesIO(xml_export), "words.xml")}
    )
    with connect(Path(app.config["DATABASE"])) as db:
        seed_known_word(db, "我")
    assert b"I am going." in client.get("/study?answer=1").data
    assert b"Edit sentence" in client.get("/study").data
    assert b"Approve" not in client.get("/sentences").data
    with connect(Path(app.config["DATABASE"])) as db:
        sentence = db.execute(
            "SELECT * FROM sentences WHERE chinese = '我去。'"
        ).fetchone()
        card = db.execute(
            "SELECT * FROM cards WHERE sentence_id = ?", (sentence["id"],)
        ).fetchone()
    assert sentence["status"] == "accepted"
    data = {
        "csrf": token,
        "version": sentence["version"],
        "chinese": sentence["chinese"],
        "pinyin": sentence["pinyin"],
        "translation": "I go.",
        "return_to": "study",
    }
    assert (
        b'name="return_to" value="study"'
        in client.get(f"/sentences/{sentence['id']}?return_to=study").data
    )
    saved = client.post(f"/sentences/{sentence['id']}", data=data)
    assert saved.status_code == 303
    assert saved.location == "/study"
    assert b"I go." in client.get("/study?answer=1").data
    data.update(version=1, status="rejected")
    skipped = client.post(f"/sentences/{sentence['id']}", data=data)
    assert skipped.status_code == 303
    assert skipped.location == "/study"
    assert b"I go." not in client.get("/study?answer=1").data
    assert (
        client.post(
            f"/review/{card['id']}",
            data={"csrf": token, "version": card["version"], "rating": 3},
        ).status_code
        == 409
    )
    assert b"I go." in client.get("/sentences?status=rejected").data
    data.update(version=2, status="accepted", return_to="")
    assert client.post(f"/sentences/{sentence['id']}", data=data).status_code == 303
    assert b"I go." in client.get("/study?answer=1").data


@pytest.mark.parametrize("include_other_word", [False, True])
def test_sentence_answer_shows_each_unfamiliar_word(
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
    client.post(
        "/import",
        data={"csrf": csrf(client), "file": (io.BytesIO(data.encode()), "words.xml")},
    )
    with connect(Path(app.config["DATABASE"])) as db:
        seed_known_word(db, "学习")
        card = db.execute(
            "SELECT c.id FROM cards c JOIN sentences s ON s.id = c.sentence_id "
            "WHERE s.chinese = ?",
            ("分配工作分配学习",),
        ).fetchone()
    if not include_other_word:
        response = client.get(f"/study?card={card['id']}&answer=1")
        assert response.status_code == 302
        assert response.location == "/study"
        return
    front = client.get(f"/study?card={card['id']}").data.decode()
    assert re.search(r'id="answer"\s+hidden', front)
    answer = client.get(f"/study?card={card['id']}&answer=1").data.decode()
    assert answer.count('data-word="分配"') == 1
    assert answer.count('data-word="工作"') == 1
    assert answer.index('data-word="分配"') < answer.index('data-word="工作"')
    assert "work; job" in answer
    assert "assign; distribute" in answer
    assert "fēn pèi" in answer
    assert "No imported definition" not in answer


def test_pages_read_stored_definitions_without_parsing_export_text(
    client: FlaskClient, xml_export: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.post(
        "/import",
        data={"csrf": csrf(client), "file": (io.BytesIO(xml_export), "words.xml")},
    )

    def unexpected_parsing(_text: str) -> Never:
        message = "Pages must read the stored definition format."
        raise AssertionError(message)

    monkeypatch.setattr("yiju.core.definitions.parse_definition", unexpected_parsing)
    for url in ["/library", "/study", "/study?answer=1"]:
        assert client.get(url).status_code == 200
    response = client.get("/library").data
    assert b'class="dictionary-senses"' in response
    assert b'class="dictionary-example"' in response


def test_partial_review_and_undo_return_next_card_and_reject_stale_ratings(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    token = csrf(client)
    client.post(
        "/import", data={"csrf": token, "file": (io.BytesIO(xml_export), "words.xml")}
    )
    with connect(Path(app.config["DATABASE"])) as db:
        seed_known_word(db, "我")
        before = dict(
            db.execute("SELECT * FROM knowledge WHERE word = '我'").fetchone()
        )
        card = db.execute(
            "SELECT * FROM cards WHERE sentence_id IS NOT NULL AND word = '我'"
        ).fetchone()
    data = {"csrf": token, "version": card["version"], "rating": 3}
    headers = {"X-Yiju-Partial": "study"}
    response = client.post(f"/review/{card['id']}", data=data, headers=headers)
    assert response.status_code == 200
    assert b'id="study-session"' in response.data
    assert b"<!DOCTYPE" not in response.data
    stale = client.post(f"/review/{card['id']}", data=data, headers=headers)
    assert stale.status_code == 409
    assert stale.json is not None
    assert stale.json["error"]
    with connect(Path(app.config["DATABASE"])) as db:
        review_id = db.execute("SELECT id FROM reviews WHERE undone = 0").fetchone()[0]
        assert db.execute("SELECT count(*) FROM reviews").fetchone()[0] == 1
        assert {row[0] for row in db.execute("SELECT word FROM knowledge")} == {
            "我",
            "去",
        }
    undone = client.post(
        "/review/undo", data={"csrf": token, "review_id": review_id}, headers=headers
    )
    assert undone.status_code == 200
    assert b'id="study-session"' in undone.data
    assert b"<!DOCTYPE" not in undone.data
    with connect(Path(app.config["DATABASE"])) as db:
        assert [dict(row) for row in db.execute("SELECT * FROM knowledge")] == [before]
    expired = client.post(
        f"/review/{card['id']}", data={**data, "csrf": "expired"}, headers=headers
    )
    assert expired.status_code == 400
    assert expired.json is not None
    assert expired.json["error"]


def test_learned_sentence_count_and_badge_follow_reviews_and_undo(
    client: FlaskClient, app: Flask, xml_export: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = csrf(client)
    client.post(
        "/import", data={"csrf": token, "file": (io.BytesIO(xml_export), "words.xml")}
    )
    now = datetime.now(UTC) + timedelta(seconds=1)
    with connect(Path(app.config["DATABASE"])) as db:
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
    assert "1 learned · 2 active" in client.get("/").data.decode()
    assert b">Learned</span>" in client.get("/sentences").data
    restarted = create_app(Path(app.config["DATABASE"]))
    with restarted.test_client() as other:
        assert "1 learned · 2 active" in other.get("/").data.decode()
    assert (
        client.post(
            "/review/undo", data={"csrf": token, "review_id": review_id}
        ).status_code
        == 303
    )
    assert "0 learned · 2 active" in client.get("/").data.decode()
    assert b">Learned</span>" not in client.get("/sentences").data
