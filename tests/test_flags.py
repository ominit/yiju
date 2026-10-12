import io
from pathlib import Path

from conftest import csrf, page_context, study_cards
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
    headers = {"Accept": "application/json"}
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
    assert [
        item.text for item in page_context(client, "/library?filter=flagged")["items"]
    ] == ["去"]
    detail = page_context(client, "/library/words/去")
    assert detail["item"].text in detail["flags"]
    assert detail["details"]["note"] == note
    fragment = client.get("/library/words/去", headers={"X-Fragment": "1"})
    assert note in fragment.get_data(as_text=True)
    assert note in client.get("/library?open=去").get_data(as_text=True)
    flagged_cards = {card["word"]: card["flagged"] for card in study_cards(client)}
    assert flagged_cards["去"] is True
    assert flagged_cards["我"] is False
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
                "/flags/words", data=data, headers={"Accept": "application/json"}
            ).status_code
            == status
        )
    with connect(Path(app.config["DATABASE"])) as db:
        assert db.execute("SELECT count(*) FROM word_flags").fetchone()[0] == 0
