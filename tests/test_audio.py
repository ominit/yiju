import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from conftest import SAMPLE, FakeVoice, add, database

from yiju.storage.database import connect
from yiju.web import audio

if TYPE_CHECKING:
    import pytest
    from flask import Flask
    from flask.testing import FlaskClient

JSON = {"Accept": "application/json"}
GO = "去"


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
        assert response.mimetype == "audio/mpeg"
        assert response.data == b"ID3" + GO.encode()
    assert voice.said == [GO]
    partial = client.get(f"/audio/cards/{cards[0]}", headers={"Range": "bytes=0-3"})
    assert partial.status_code == 206
    assert partial.data == b"ID3\xe5"
    assert client.get("/audio/cards/missing").status_code == 404
    assert voice.said == [GO]


def test_sentence_audio_follows_the_sentence_text(
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
    assert voice.said == ["我去。", "我去学校。"]


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
    assert list(speaker(app).clips.glob("*.mp3")) == []
    voice.failing.clear()
    assert client.get(f"/audio/cards/{card}").status_code == 200
    assert voice.said == [GO, GO]


def test_repeated_failures_pause_speech_for_a_while(
    client: FlaskClient, app: Flask, voice: FakeVoice, monkeypatch: pytest.MonkeyPatch
) -> None:
    card = card_ids(app, "去")[0]
    voice.failing.add(GO)
    for _ in range(audio.OUTAGE_THRESHOLD):
        assert client.get(f"/audio/cards/{card}").status_code == 503
    response = client.get(f"/audio/cards/{card}", headers=JSON)
    assert response.status_code == 503
    assert response.json is not None
    assert "few minutes" in response.json["error"]
    assert voice.said == [GO] * audio.OUTAGE_THRESHOLD
    voice.failing.clear()
    later = time.monotonic() + audio.OUTAGE_SECONDS + 1
    monkeypatch.setattr(audio.time, "monotonic", lambda: later)
    assert client.get(f"/audio/cards/{card}").status_code == 200


def test_words_say_the_word(client: FlaskClient, app: Flask, voice: FakeVoice) -> None:
    card = card_ids(app, "苹果")[0]
    assert client.get(f"/audio/cards/{card}").status_code == 200
    assert voice.said == ["苹果"]


def test_playing_a_clip_keeps_it_and_old_clips_are_swept(
    client: FlaskClient, app: Flask
) -> None:
    card = card_ids(app, "去")[0]
    voices = speaker(app)
    voices.clips.mkdir(parents=True)
    old = time.time() - audio.UNUSED_SECONDS - 60
    stale, wanted, legacy = (
        voices.path("你"),
        voices.path("好"),
        voices.clips / "legacy.wav",
    )
    for path in (stale, wanted, legacy):
        path.write_bytes(b"ID3")
        os.utime(path, (old, old))
    assert client.get(f"/audio/cards/{card}").status_code == 200
    played = voices.path(GO)
    os.utime(played, (old, old))
    assert client.get(f"/audio/cards/{card}").status_code == 200
    voices.sweep({wanted}, time.time())
    assert sorted(voices.clips.iterdir()) == sorted([played, wanted])
