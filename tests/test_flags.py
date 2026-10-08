import io
from pathlib import Path

from conftest import csrf
from flask import Flask
from flask.testing import FlaskClient

from yiju.storage.database import connect, initialize


def test_flag_note_export_and_resolve(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    token = csrf(client)
    for data in [xml_export, xml_export.replace(b"TEST", b"SECOND")]:
        client.post(
            "/import", data={"csrf": token, "file": (io.BytesIO(data), "words.xml")}
        )
    headers = {"X-Yiju-Partial": "page"}
    flag = {"csrf": token, "word": "去", "flagged": "1"}
    assert client.post("/flags/words", data=flag, headers=headers).json == {
        "word": "去",
        "flagged": True,
    }
    note = "Check the definition and pronunciation."
    assert (
        client.post(
            "/flags/words", data={**flag, "note": note}, headers=headers
        ).status_code
        == 200
    )
    client.post("/flags/words", data=flag, headers=headers)
    initialize(Path(app.config["DATABASE"]))
    flagged = client.get("/library?filter=flagged").get_data(as_text=True)
    assert 'data-entry="去"' in flagged
    assert 'data-entry="学习"' not in flagged
    assert note in flagged
    exported = client.get("/flags/words/export")
    data = exported.json
    assert data is not None
    assert data["version"] == 1
    assert len(data["words"]) == 1
    word = data["words"][0]
    assert word["word"] == "去"
    assert word["note"] == note
    assert len(word["entries"]) == 2
    assert {entry["references"][0]["dictionary_id"] for entry in word["entries"]} == {
        "TEST",
        "SECOND",
    }
    assert all(entry["original_definition"] for entry in word["entries"])
    assert all(entry["structured_definition"]["senses"] for entry in word["entries"])
    assert word["sentences"]
    with connect(Path(app.config["DATABASE"])) as db:
        assert db.execute("SELECT sum(reviews) FROM cards").fetchone()[0] == 0
        assert db.execute("SELECT sum(suspended) FROM cards").fetchone()[0] == 0
    assert (
        client.post(
            "/flags/words", data={**flag, "flagged": "0"}, headers=headers
        ).status_code
        == 200
    )
    assert client.get("/flags/words/export").json == {"version": 1, "words": []}


def test_invalid_flags_do_not_change_library(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    token = csrf(client)
    client.post(
        "/import", data={"csrf": token, "file": (io.BytesIO(xml_export), "words.xml")}
    )
    for data, status in [
        ({"csrf": "expired", "word": "去", "flagged": "1"}, 400),
        ({"csrf": token, "word": "missing", "flagged": "1"}, 404),
        ({"csrf": token, "word": "去", "flagged": "bad"}, 400),
        ({"csrf": token, "word": "去", "flagged": "1", "note": "x" * 2001}, 400),
    ]:
        assert (
            client.post(
                "/flags/words", data=data, headers={"X-Yiju-Partial": "page"}
            ).status_code
            == status
        )
    with connect(Path(app.config["DATABASE"])) as db:
        assert db.execute("SELECT count(*) FROM word_flags").fetchone()[0] == 0


def test_flags_migration_preserves_existing_cards(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    path = Path(app.config["DATABASE"])
    client.post(
        "/import",
        data={"csrf": csrf(client), "file": (io.BytesIO(xml_export), "words.xml")},
    )
    with connect(path) as db:
        before = [tuple(row) for row in db.execute("SELECT * FROM cards ORDER BY id")]
        db.execute("DROP TABLE word_flags")
        db.execute("PRAGMA user_version = 5")
    initialize(path)
    initialize(path)
    with connect(path) as db:
        assert [
            tuple(row) for row in db.execute("SELECT * FROM cards ORDER BY id")
        ] == before
        assert db.execute("SELECT count(*) FROM word_flags").fetchone()[0] == 0
