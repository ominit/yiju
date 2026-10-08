import gzip
import io
from pathlib import Path

import pytest
from conftest import csrf
from flask import Flask
from flask.testing import FlaskClient

from yiju.storage.database import connect, settings


@pytest.mark.parametrize(
    ("path", "title", "navigation", "body_class"),
    [
        ("/", "Yiju · 一句", "/", ""),
        ("/study", "Study · Yiju", "/study", "study-layout"),
        ("/library", "Words · Library · Yiju", "/library", ""),
        ("/sentences", "Sentences · Library · Yiju", "/library", ""),
        ("/settings", "Settings · Yiju", "/settings", ""),
        ("/import", "Import words · Yiju", "", ""),
    ],
)
def test_navigation_renders_content_and_metadata(
    client: FlaskClient, path: str, title: str, navigation: str, body_class: str
) -> None:
    response = client.get(path, headers={"X-Yiju-Partial": "page"})
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '<main id="main"' in html
    assert f'data-title="{title}"' in html
    assert f'data-navigation="{navigation}"' in html
    assert f'data-body-class="{body_class}"' in html
    assert "<html" not in html
    assert "site-header" not in html
    assert "<script" not in html
    assert response.headers["Cache-Control"] == "no-store"
    assert "X-Yiju-Partial" in response.vary
    full = client.get(path).get_data(as_text=True)
    assert '<script src="/static/navigation.js" defer></script>' in full
    assert '<header class="site-header">' in full


def test_navigation_import_and_editor(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    response = client.post(
        "/import",
        data={"csrf": csrf(client), "file": (io.BytesIO(xml_export), "words.xml")},
        headers={"X-Yiju-Partial": "page"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Import result" in response.data
    assert b"<html" not in response.data
    with connect(Path(app.config["DATABASE"])) as db:
        sentence = db.execute("SELECT * FROM sentences LIMIT 1").fetchone()
    editor = client.get(
        f"/sentences/{sentence['id']}", headers={"X-Yiju-Partial": "page"}
    )
    assert b'data-navigation="/library"' in editor.data
    assert b"<html" not in editor.data


def test_navigation_settings_save_and_error_preserve_server_state(
    client: FlaskClient, app: Flask
) -> None:
    data = {
        "csrf": csrf(client),
        "max_unknown": 2,
        "new_per_day": 42,
        "known_days": 4,
        "known_stability": 20,
        "retention": 0.9,
        "known_retrievability": 0.8,
    }
    headers = {"X-Yiju-Partial": "page"}
    saved = client.post("/settings", data=data, headers=headers, follow_redirects=True)
    assert b"Study settings saved." in saved.data
    assert b"<html" not in saved.data
    invalid = client.post(
        "/settings", data={**data, "new_per_day": -1}, headers=headers
    )
    assert invalid.status_code == 400
    assert invalid.json is not None
    assert "new_per_day" in invalid.json["error"]
    expired = client.post(
        "/settings", data={**data, "csrf": "expired"}, headers=headers
    )
    assert expired.status_code == 400
    assert expired.json is not None
    assert "expired" in expired.json["error"]
    with connect(Path(app.config["DATABASE"])) as db:
        assert settings(db)["new_per_day"] == 42


def test_navigation_conflicts_return_json(
    client: FlaskClient, xml_export: bytes, app: Flask
) -> None:
    token = csrf(client)
    client.post(
        "/import", data={"csrf": token, "file": (io.BytesIO(xml_export), "words.xml")}
    )
    with connect(Path(app.config["DATABASE"])) as db:
        card = db.execute(
            "SELECT * FROM cards WHERE sentence_id IS NULL LIMIT 1"
        ).fetchone()
    response = client.post(
        f"/cards/{card['id']}/suspend",
        data={"csrf": token, "version": card["version"] + 1},
        headers={"X-Yiju-Partial": "page"},
    )
    assert response.status_code == 409
    assert response.json is not None
    assert "changed" in response.json["error"]


def test_pages_compress_when_supported(client: FlaskClient) -> None:
    headers = {"X-Yiju-Partial": "page"}
    plain = client.get("/settings", headers=headers)
    compressed = client.get("/settings", headers={**headers, "Accept-Encoding": "gzip"})
    assert compressed.headers["Content-Encoding"] == "gzip"
    assert gzip.decompress(compressed.data) == plain.data
    assert len(compressed.data) < len(plain.data) / 2
    assert "Accept-Encoding" in compressed.vary
    excluded = client.get(
        "/settings", headers={**headers, "Accept-Encoding": "gzip;q=0"}
    )
    assert "Content-Encoding" not in excluded.headers
    assert "Content-Encoding" not in client.get("/backup", headers=headers).headers
