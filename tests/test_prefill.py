import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from conftest import SAMPLE, SENTENCES, WORDS, add, database, import_text

from yiju.storage.database import connect
from yiju.web import audio, create_app, strokes
from yiju.web.prefill import MAX_ATTEMPTS, Prefill

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient


class Fakes:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.texts: list[str] = []
        self.characters: list[str] = []
        self.failing_text: str | None = None
        self.character_error: Exception | None = None
        monkeypatch.setattr(audio, "_outage", audio.Outage())
        monkeypatch.setattr(audio, "synthesize", self.synthesize)
        monkeypatch.setattr(strokes, "fetch_character", self.fetch_character)

    async def synthesize(self, text: str, path: Path) -> None:
        self.texts.append(text)
        if text == self.failing_text:
            message = "Voice service is unavailable"
            raise OSError(message)
        await asyncio.to_thread(path.write_bytes, b"ID3" + text.encode())

    async def fetch_character(self, character: str) -> dict[str, object]:
        self.characters.append(character)
        if character == "去" and self.character_error is not None:
            raise self.character_error
        return {"strokes": ["M 0 0"], "medians": [[[0, 0]]]}


def worker(app: Flask) -> Prefill:
    prefill = app.extensions["yiju.prefill"]
    assert isinstance(prefill, Prefill)
    return prefill


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Fakes:
    return Fakes(monkeypatch)


@pytest.fixture
def prefill(app: Flask) -> Prefill:
    with database(app) as db:
        add(db, SAMPLE)
    return worker(app)


def test_fill_caches_audio_and_strokes_once(prefill: Prefill, fakes: Fakes) -> None:
    assert prefill.fill()
    assert set(fakes.texts) == WORDS | SENTENCES
    assert len(fakes.texts) == len(WORDS | SENTENCES)
    assert set(fakes.characters) == set("我去学习中文苹果")
    assert len(list((prefill.directory / "audio").iterdir())) == len(fakes.texts)
    assert len(list((prefill.directory / "strokes").iterdir())) == 8
    fakes.texts.clear()
    fakes.characters.clear()
    assert prefill.fill()
    assert fakes.texts == []
    assert fakes.characters == []


def test_skipped_sentences_are_not_voiced(
    app: Flask, prefill: Prefill, fakes: Fakes
) -> None:
    with connect(Path(app.config["DATABASE"])) as db:
        db.execute("UPDATE sentences SET hidden = 1 WHERE chinese = '我去。'")
    assert prefill.fill()
    assert set(fakes.texts) == WORDS | {"我学习中文。"}


def test_failed_character_is_retried_then_skipped(
    prefill: Prefill, fakes: Fakes
) -> None:
    fakes.character_error = OSError("offline")
    for _ in range(MAX_ATTEMPTS):
        assert not prefill.fill()
    assert prefill.fill()
    assert fakes.characters.count("去") == MAX_ATTEMPTS
    assert set(fakes.texts) == WORDS | SENTENCES


def test_missing_character_is_not_retried(prefill: Prefill, fakes: Fakes) -> None:
    fakes.character_error = LookupError("去")
    assert prefill.fill()
    assert prefill.fill()
    assert fakes.characters.count("去") == 1


def test_audio_outage_pauses_prefill(prefill: Prefill, fakes: Fakes) -> None:
    fakes.failing_text = "我"
    for _ in range(audio.OUTAGE_THRESHOLD):
        assert not prefill.fill()
    attempts = len(fakes.texts)
    assert not prefill.fill()
    assert len(fakes.texts) == attempts


def test_changes_wake_the_prefill(app: Flask, client: FlaskClient) -> None:
    prefill = worker(app)
    assert not prefill.wake.is_set()
    import_text(client)
    assert prefill.wake.is_set()


def test_server_prefills_in_the_background(tmp_path: Path, fakes: Fakes) -> None:
    app = create_app(tmp_path / "web.sqlite3", prefill=True)
    import_text(app.test_client())
    deadline = time.monotonic() + 10
    while set(fakes.texts) != WORDS | SENTENCES and time.monotonic() < deadline:
        time.sleep(0.05)
    assert set(fakes.texts) == WORDS | SENTENCES
