from pathlib import Path
from typing import TYPE_CHECKING

from conftest import SAMPLE, add, csrf, database, page_context, study_cards

from yiju.storage.database import initialize

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient

JSON = {"Accept": "application/json"}


def test_flag_note_export_and_resolve(client: FlaskClient, app: Flask) -> None:
    with database(app) as db:
        add(db, SAMPLE)
    token = csrf(client)
    flag = {"csrf": token, "word": "去", "flagged": "1"}
    assert client.post("/flags/words", data=flag, headers=JSON).json == {
        "word": "去",
        "flagged": True,
    }
    note = "Check the definition and pronunciation."
    assert (
        client.post(
            "/flags/words", data={**flag, "note": note}, headers=JSON
        ).status_code
        == 200
    )
    client.post("/flags/words", data=flag, headers=JSON)
    initialize(Path(app.config["DATABASE"]))
    assert [
        item.text for item in page_context(client, "/library?filter=flagged")["items"]
    ] == ["去"]
    detail = page_context(client, "/library/words/去")
    assert detail["details"]["note"] == note
    assert note in client.get("/library?open=去").get_data(as_text=True)
    flagged = {card["word"]: card["flagged"] for card in study_cards(client)}
    assert flagged == {"我": False, "去": True}
    exported = client.get("/flags/words/export").json
    assert exported is not None
    assert exported["version"] == 2
    (word,) = exported["words"]
    assert word["word"] == "去"
    assert word["note"] == note
    assert word["source"] == "cedict"
    assert word["readings"][0]["pinyin"] == "qù"
    assert [sentence["chinese"] for sentence in word["sentences"]] == ["我去。"]
    client.post("/flags/words", data={**flag, "flagged": "0"}, headers=JSON)
    assert client.get("/flags/words/export").json == {"version": 2, "words": []}


def test_invalid_flags_do_not_change_library(client: FlaskClient, app: Flask) -> None:
    with database(app) as db:
        add(db, SAMPLE)
    token = csrf(client)
    for data, status in [
        ({"csrf": "expired", "word": "去", "flagged": "1"}, 400),
        ({"csrf": token, "word": "missing", "flagged": "1"}, 404),
        ({"csrf": token, "word": "去", "flagged": "bad"}, 400),
        ({"csrf": token, "word": "去", "flagged": "1", "note": "x" * 2001}, 400),
    ]:
        assert (
            client.post("/flags/words", data=data, headers=JSON).status_code == status
        )
    with database(app) as db:
        assert db.execute("SELECT count(*) FROM flags").fetchone()[0] == 0
