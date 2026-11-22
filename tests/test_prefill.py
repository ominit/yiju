import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from conftest import SAMPLE, FakeVoice, add, database, import_text, settle

from yiju.storage.database import connect
from yiju.storage.pending import approve, preview, stage_names
from yiju.storage.queue import due_before
from yiju.web import audio, create_app, strokes
from yiju.web.prefill import MAX_ATTEMPTS, Prefill

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient

UNLOCKED = {"我", "去"}
SENTENCES = {"我去。", "我学习中文。"}


class Fakes:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.characters: list[str] = []
        self.character_error: Exception | None = None
        monkeypatch.setattr(strokes, "fetch_character", self.fetch_character)

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


def fill(app: Flask) -> bool:
    complete = worker(app).fill()
    settle(app)
    return complete


def test_fill_voices_cards_due_soon_and_strokes_once(
    app: Flask, prefill: Prefill, fakes: Fakes, voice: FakeVoice
) -> None:
    assert fill(app)
    assert sorted(voice.said) == sorted(UNLOCKED)
    assert set(fakes.characters) == set("我去学习中文苹果")
    assert len(list((prefill.directory / "audio").iterdir())) == len(UNLOCKED)
    assert len(list((prefill.directory / "strokes").iterdir())) == 8
    voice.said.clear()
    fakes.characters.clear()
    assert fill(app)
    assert voice.said == []
    assert fakes.characters == []


@pytest.mark.usefixtures("prefill", "fakes")
def test_skipped_and_name_sentences_are_not_voiced_ahead(
    app: Flask, voice: FakeVoice
) -> None:
    with connect(Path(app.config["DATABASE"])) as db:
        db.execute("UPDATE sentences SET state = 'unlocked'")
    assert fill(app)
    assert set(voice.said) == UNLOCKED | SENTENCES
    voice.said.clear()
    with connect(Path(app.config["DATABASE"])) as db:
        db.execute("UPDATE sentences SET hidden = 1 WHERE chinese = '我去。'")
        db.execute(
            "UPDATE sentences SET chinese = '{A}去。', pinyin = '{A} qù.' "
            "WHERE chinese = '我学习中文。'"
        )
        db.commit()
        stage_names(db, "王芳 female\n")
        approve(db, preview(db).version, datetime.now(UTC))
        due = due_before(db, datetime.now(UTC))
        assert any(row["chinese"] == "{A}去。" for row in due)
    for path in (app.extensions["yiju.speech"].clips).iterdir():
        path.unlink()
    assert fill(app)
    assert set(voice.said) == UNLOCKED


@pytest.mark.usefixtures("prefill", "fakes")
def test_pronoun_sentences_are_voiced_for_each_gender(
    app: Flask, voice: FakeVoice
) -> None:
    with connect(Path(app.config["DATABASE"])) as db:
        db.execute(
            "UPDATE sentences SET state = 'unlocked', "
            "chinese = '{A.他}去。', pinyin = 'tā qù.' WHERE chinese = '我去。'"
        )
        db.execute("UPDATE sentences SET hidden = 1 WHERE chinese = '我学习中文。'")
        db.commit()
        stage_names(db, "王芳 female\n李伟 male\n")
        approve(db, preview(db).version, datetime.now(UTC))
    assert fill(app)
    assert set(voice.said) == UNLOCKED | {"他去。", "她去。"}


@pytest.mark.usefixtures("prefill", "fakes")
def test_fill_sweeps_clips_unused_for_a_week(app: Flask) -> None:
    assert fill(app)
    speaker = app.extensions["yiju.speech"]
    old = time.time() - audio.UNUSED_SECONDS - 60
    stale = speaker.path("你")
    stale.write_bytes(b"ID3")
    for path in speaker.clips.iterdir():
        os.utime(path, (old, old))
    assert fill(app)
    assert sorted(speaker.clips.iterdir()) == sorted(
        speaker.path(said) for said in UNLOCKED
    )


def test_failed_character_is_retried_then_skipped(
    prefill: Prefill, fakes: Fakes
) -> None:
    fakes.character_error = OSError("offline")
    for _ in range(MAX_ATTEMPTS):
        assert not prefill.fill()
    assert prefill.fill()
    assert fakes.characters.count("去") == MAX_ATTEMPTS


def test_missing_character_is_not_retried(prefill: Prefill, fakes: Fakes) -> None:
    fakes.character_error = LookupError("去")
    assert prefill.fill()
    assert prefill.fill()
    assert fakes.characters.count("去") == 1


def test_changes_wake_the_prefill(app: Flask, client: FlaskClient) -> None:
    prefill = worker(app)
    assert not prefill.wake.is_set()
    import_text(client)
    assert prefill.wake.is_set()


@pytest.mark.usefixtures("fakes")
def test_server_prefills_in_the_background(tmp_path: Path, voice: FakeVoice) -> None:
    app = create_app(tmp_path / "web.sqlite3", prefill=True)
    import_text(app.test_client())
    deadline = time.monotonic() + 10
    while set(voice.said) != UNLOCKED and time.monotonic() < deadline:
        time.sleep(0.05)
    assert set(voice.said) == UNLOCKED
