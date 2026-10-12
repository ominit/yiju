import asyncio
import hashlib
import os
import tempfile
import time
from pathlib import Path
from threading import Lock

import edge_tts
from aiohttp import ClientError
from flask import Blueprint, Response, abort, current_app, send_file

from . import database

audio = Blueprint("audio", __name__)
VOICE = "zh-CN-XiaoxiaoNeural"
CACHE_VERSION = "1"
_locks = tuple(Lock() for _ in range(16))
OUTAGE_THRESHOLD = 3
OUTAGE_SECONDS = 120


class Outage:
    def __init__(self) -> None:
        self.failures = 0
        self.until = 0.0

    def active(self) -> bool:
        return time.monotonic() < self.until

    def failed(self) -> None:
        self.failures += 1
        if self.failures >= OUTAGE_THRESHOLD:
            self.failures = 0
            self.until = time.monotonic() + OUTAGE_SECONDS

    def recovered(self) -> None:
        self.failures = 0


_outage = Outage()


async def synthesize(text: str, destination: Path) -> None:
    speech = edge_tts.Communicate(text, VOICE, connect_timeout=5, receive_timeout=10)
    await asyncio.wait_for(speech.save(str(destination)), timeout=20)


def audio_path(text: str, directory: Path) -> Path:
    key = hashlib.sha256(f"{CACHE_VERSION}:{VOICE}:{text}".encode()).hexdigest()
    return directory / f"{key}.mp3"


def cached_audio(text: str, directory: Path) -> Path:
    destination = audio_path(text, directory)
    with _locks[int(destination.stem[:2], 16) % len(_locks)]:
        if destination.is_file():
            return destination
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(suffix=".mp3", dir=directory)
        os.close(descriptor)
        staging = Path(temporary)
        try:
            asyncio.run(synthesize(text, staging))
            if not staging.stat().st_size:
                message = "The voice returned an empty recording."
                raise ValueError(message)
            staging.replace(destination)
        finally:
            staging.unlink(missing_ok=True)
    return destination


@audio.get("/audio/cards/<card_id>")
def pronunciation(card_id: str) -> Response:
    row = (
        database()
        .execute(
            "SELECT c.word, s.chinese FROM cards c "
            "LEFT JOIN sentences s ON s.id = c.sentence_id WHERE c.id = ?",
            (card_id,),
        )
        .fetchone()
    )
    if row is None:
        abort(404)
    text = row["chinese"] or row["word"]
    directory = Path(current_app.config["DATABASE"]).resolve().parent / "audio"
    if _outage.active() and not audio_path(text, directory).is_file():
        abort(503, "Pronunciation is unavailable. Try again in a few minutes.")
    try:
        path = cached_audio(text, directory)
    except (
        OSError,
        TimeoutError,
        ClientError,
        edge_tts.exceptions.EdgeTTSException,
        ValueError,
    ):
        _outage.failed()
        current_app.logger.exception("Could not generate pronunciation audio")
        abort(503, "Pronunciation is unavailable. Try replaying it in a moment.")
    _outage.recovered()
    return send_file(path, mimetype="audio/mpeg", conditional=True)
