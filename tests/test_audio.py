import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from conftest import SAMPLE, FakeVoice, add, database

from yiju.core.speech import phonemes
from yiju.storage.database import connect
from yiju.web import audio

if TYPE_CHECKING:
    import pytest
    from flask import Flask
    from flask.testing import FlaskClient

JSON = {"Accept": "application/json"}
GO = phonemes("去", "qù")


def speaker(app: Flask) -> audio.Speaker:
    value = app.extensions["yiju.speech"]
    assert isinstance(value, audio.Speaker)
    return value


def card_ids(app: Flask, word: str) -> list[int]:
    with database(app) as db:
        add(db, SAMPLE)
    with connect(Path(app.config["DATABASE"])) as db:
        return [
            row[0] for row in db.execute("SELECT id FROM cards WHERE word = ?", (word,))
        ]


def test_audio_cache_is_shared_across_directions_and_supports_ranges(
    client: FlaskClient, app: Flask, voice: FakeVoice
) -> None:
    cards = card_ids(app, "去")
    for card in cards:
        response = client.get(f"/audio/cards/{card}")
        assert response.status_code == 200
        assert response.mimetype == "audio/wav"
        assert response.data == b"RIFF" + GO.encode()
    assert voice.said == [GO]
    partial = client.get(f"/audio/cards/{cards[0]}", headers={"Range": "bytes=0-3"})
    assert partial.status_code == 206
    assert partial.data == b"RIFF"
    assert client.get("/audio/cards/missing").status_code == 404
    assert voice.said == [GO]


def test_sentence_audio_follows_the_sentence_pinyin(
    client: FlaskClient, app: Flask, voice: FakeVoice
) -> None:
    with database(app) as db:
        add(db, SAMPLE)
    with connect(Path(app.config["DATABASE"])) as db:
        card = db.execute(
            "SELECT c.id, s.id AS sentence FROM cards c "
            "JOIN sentences s ON s.id = c.sentence_id "
            "WHERE s.chinese = '我去。' LIMIT 1"
        ).fetchone()
    url = f"/audio/cards/{card['id']}"
    assert client.get(url).status_code == 200
    with connect(Path(app.config["DATABASE"])) as db:
        db.execute(
            "UPDATE sentences SET chinese = '我去学校。', pinyin = 'wǒ qù xuéxiào.' "
            "WHERE id = ?",
            (card["sentence"],),
        )
    assert client.get(url).status_code == 200
    assert voice.said == [
        phonemes("我去。", "wǒ qù."),
        phonemes("我去学校。", "wǒ qù xuéxiào."),
    ]


def test_concurrent_requests_only_generate_once(app: Flask, voice: FakeVoice) -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(lambda _: speaker(app).clip(GO), range(4)))
    assert len(set(paths)) == 1
    assert voice.said == [GO]
    assert list(speaker(app).clips.iterdir()) == [paths[0]]


def test_failed_audio_is_not_cached(
    client: FlaskClient, app: Flask, voice: FakeVoice
) -> None:
    card = card_ids(app, "去")[0]
    voice.failing.add(GO)
    response = client.get(f"/audio/cards/{card}", headers=JSON)
    assert response.status_code == 503
    assert response.json is not None
    assert "unavailable" in response.json["error"]
    assert list(speaker(app).clips.glob("*.wav")) == []
    voice.failing.clear()
    assert client.get(f"/audio/cards/{card}").status_code == 200
    assert voice.said == [GO, GO]


def test_a_voice_that_will_not_load_is_retried_later(
    client: FlaskClient, app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    card = card_ids(app, "去")[0]
    attempts = 0

    def broken(_directory: Path) -> None:
        nonlocal attempts
        attempts += 1
        message = "offline"
        raise OSError(message)

    monkeypatch.setattr(audio, "Voice", broken)
    assert client.get(f"/audio/cards/{card}").status_code == 503
    assert attempts == 1
    response = client.get(f"/audio/cards/{card}", headers=JSON)
    assert response.status_code == 503
    assert response.json is not None
    assert "few minutes" in response.json["error"]
    assert attempts == 1
    later = time.monotonic() + audio.RETRY_SECONDS + 1
    monkeypatch.setattr(audio.time, "monotonic", lambda: later)
    assert client.get(f"/audio/cards/{card}").status_code == 503
    assert attempts == 2


def test_words_say_their_first_reading(
    client: FlaskClient, app: Flask, voice: FakeVoice
) -> None:
    card = card_ids(app, "苹果")[0]
    assert client.get(f"/audio/cards/{card}").status_code == 200
    assert voice.said == [phonemes("苹果", "píngguǒ")]


def test_playing_a_clip_keeps_it_and_old_clips_are_swept(
    client: FlaskClient, app: Flask
) -> None:
    card = card_ids(app, "去")[0]
    voices = speaker(app)
    voices.clips.mkdir(parents=True)
    old = time.time() - audio.UNUSED_SECONDS - 60
    stale, wanted, legacy = (
        voices.path("ㄋㄧ3"),
        voices.path("ㄏㄠ3"),
        voices.clips / "legacy.mp3",
    )
    for path in (stale, wanted, legacy):
        path.write_bytes(b"RIFF")
        os.utime(path, (old, old))
    assert client.get(f"/audio/cards/{card}").status_code == 200
    played = voices.path(GO)
    os.utime(played, (old, old))
    assert client.get(f"/audio/cards/{card}").status_code == 200
    voices.sweep({wanted}, time.time())
    assert sorted(voices.clips.iterdir()) == sorted([played, wanted])
