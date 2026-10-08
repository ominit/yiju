import asyncio
import hashlib
import os
import tempfile
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


async def synthesize(text: str, destination: Path) -> None:
    speech = edge_tts.Communicate(text, VOICE, connect_timeout=5, receive_timeout=10)
    await asyncio.wait_for(speech.save(str(destination)), timeout=20)


def cached_audio(text: str, directory: Path) -> Path:
    key = hashlib.sha256(f"{CACHE_VERSION}:{VOICE}:{text}".encode()).hexdigest()
    destination = directory / f"{key}.mp3"
    with _locks[int(key[:2], 16) % len(_locks)]:
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
    try:
        path = cached_audio(
            text, Path(current_app.config["DATABASE"]).resolve().parent / "audio"
        )
    except (
        OSError,
        TimeoutError,
        ClientError,
        edge_tts.exceptions.EdgeTTSException,
        ValueError,
    ):
        current_app.logger.exception("Could not generate pronunciation audio")
        abort(503, "Pronunciation is unavailable. Try replaying it in a moment.")
    return send_file(path, mimetype="audio/mpeg", conditional=True)
