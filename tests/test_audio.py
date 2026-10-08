import asyncio
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from conftest import csrf
from flask import Flask
from flask.testing import FlaskClient

from yiju.storage.database import connect
from yiju.web import audio


@pytest.fixture
def recorded_texts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def fake_speech(text: str, path: Path) -> None:
        calls.append(text)
        await asyncio.sleep(0.02)
        await asyncio.to_thread(path.write_bytes, b"ID3" + text.encode())

    monkeypatch.setattr(audio, "synthesize", fake_speech)
    return calls


def test_audio_cache_is_shared_across_directions_and_supports_ranges(
    client: FlaskClient, app: Flask, xml_export: bytes, recorded_texts: list[str]
) -> None:
    client.post(
        "/import",
        data={"csrf": csrf(client), "file": (io.BytesIO(xml_export), "words.xml")},
    )
    with connect(Path(app.config["DATABASE"])) as db:
        cards = db.execute(
            "SELECT * FROM cards WHERE word = '去' AND sentence_id IS NULL"
        ).fetchall()
    for card in cards:
        response = client.get(f"/audio/cards/{card['id']}")
        assert response.status_code == 200
        assert response.mimetype == "audio/mpeg"
        assert response.data == b"ID3" + "去".encode()
    assert recorded_texts == ["去"]
    partial = client.get(
        f"/audio/cards/{cards[0]['id']}", headers={"Range": "bytes=0-2"}
    )
    assert partial.status_code == 206
    assert partial.data == b"ID3"
    assert client.get("/audio/cards/missing").status_code == 404
    assert recorded_texts == ["去"]


def test_sentence_audio_changes_when_sentence_is_edited(
    client: FlaskClient, app: Flask, xml_export: bytes, recorded_texts: list[str]
) -> None:
    client.post(
        "/import",
        data={"csrf": csrf(client), "file": (io.BytesIO(xml_export), "words.xml")},
    )
    with connect(Path(app.config["DATABASE"])) as db:
        card = db.execute(
            "SELECT * FROM cards WHERE sentence_id IS NOT NULL LIMIT 1"
        ).fetchone()
        original = db.execute(
            "SELECT chinese FROM sentences WHERE id = ?", (card["sentence_id"],)
        ).fetchone()[0]
    url = f"/audio/cards/{card['id']}"
    assert client.get(url).status_code == 200
    with connect(Path(app.config["DATABASE"])) as db:
        db.execute(
            "UPDATE sentences SET chinese = '我去学校。' WHERE id = ?",
            (card["sentence_id"],),
        )
    assert client.get(url).status_code == 200
    assert recorded_texts == [original, "我去学校。"]


def test_concurrent_audio_only_generates_once(
    tmp_path: Path, recorded_texts: list[str]
) -> None:
    directory = tmp_path / "audio"
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(
            pool.map(lambda _: audio.cached_audio("你好", directory), range(4))
        )
    assert len(set(paths)) == 1
    assert recorded_texts == ["你好"]
    assert len(list(directory.iterdir())) == 1


def test_failed_audio_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken_speech(_text: str, path: Path) -> None:
        await asyncio.to_thread(path.write_bytes, b"partial")
        message = "Connection failed"
        raise OSError(message)

    monkeypatch.setattr(audio, "synthesize", broken_speech)
    directory = tmp_path / "audio"
    with pytest.raises(OSError, match="Connection failed"):
        audio.cached_audio("你好", directory)
    assert list(directory.iterdir()) == []


def test_unavailable_voice_returns_retryable_error(
    client: FlaskClient, app: Flask, xml_export: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.post(
        "/import",
        data={"csrf": csrf(client), "file": (io.BytesIO(xml_export), "words.xml")},
    )
    with connect(Path(app.config["DATABASE"])) as db:
        card_id = db.execute("SELECT id FROM cards LIMIT 1").fetchone()[0]

    async def unavailable(_text: str, _path: Path) -> None:
        message = "Voice service is unavailable"
        raise OSError(message)

    monkeypatch.setattr(audio, "synthesize", unavailable)
    response = client.get(f"/audio/cards/{card_id}", headers={"X-Yiju-Partial": "page"})
    assert response.status_code == 503
    assert response.json is not None
    assert "unavailable" in response.json["error"]
    assert list((Path(app.config["DATABASE"]).parent / "audio").iterdir()) == []
