import io
import sqlite3
from pathlib import Path

from conftest import csrf
from flask import Flask
from flask.testing import FlaskClient

from yiju.storage.database import connect
from yiju.web import create_app


def test_full_import_preview_review_and_undo(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    token = csrf(client)
    response = client.post(
        "/import", data={"csrf": token, "file": (io.BytesIO(xml_export), "words.xml")}
    )
    assert response.status_code == 303
    assert b"Your words are here" in client.get(response.location).data
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
