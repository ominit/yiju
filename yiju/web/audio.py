import asyncio
import hashlib
import heapq
import io
import itertools
import json
import os
import tempfile
import time
import wave
from contextlib import suppress
from pathlib import Path
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING, BinaryIO, cast

import numpy as np
import onnxruntime
from aiohttp import ClientError, ClientSession, ClientTimeout
from flask import Blueprint, Response, abort, current_app, request, send_file

from yiju.core.dictionary import decode
from yiju.core.names import letters, spoken
from yiju.core.speech import phonemes
from yiju.storage.names import chosen
from yiju.storage.queue import CARD_QUERY

from . import database

if TYPE_CHECKING:
    import logging
    import sqlite3
    from collections.abc import Mapping

    from yiju.core.names import Name

audio = Blueprint("audio", __name__)
VOICE = "zf_027"
CACHE_VERSION = "2"
RELEASE = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/"
)
MODEL = (
    "kokoro-v1.1-zh.onnx",
    "859f9ded9f53be16c24857cdab3254a45da53c3afd5ba6ef134c7de3f822e326",
)
VOICES = (
    "voices-v1.1-zh.bin",
    "14cb6186c99e4f6016871405f62046c5df863ae27465cbdc4ee08be7dd703acd",
)
SAMPLE_RATE = 24000
MAX_TOKENS = 510
SILENCE = 10 ** (-60 / 20)
FRAME = 512
WAIT_SECONDS = 30
RETRY_SECONDS = 300
UNUSED_SECONDS = 7 * 24 * 60 * 60
URGENT, LATER = 0, 1
ERRORS = (OSError, TimeoutError, ClientError, ValueError, RuntimeError, KeyError)


class SpeechUnavailableError(Exception):
    pass


async def fetch(url: str, stream: BinaryIO) -> str:
    hasher = hashlib.sha256()
    timeout = ClientTimeout(total=None, connect=15, sock_read=60)
    async with (
        ClientSession(timeout=timeout) as session,
        session.get(url) as response,
    ):
        response.raise_for_status()
        async for chunk in response.content.iter_chunked(1 << 20):
            hasher.update(chunk)
            stream.write(chunk)
    return hasher.hexdigest()


def model_file(directory: Path, name: str, digest: str) -> Path:
    path = directory / name
    if path.is_file():
        return path
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(suffix=".part", dir=directory)
    staging = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            fetched = asyncio.run(fetch(RELEASE + name, stream))
        if fetched != digest:
            message = f"{name} did not match its checksum."
            raise ValueError(message)
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)
    return path


def trim(samples: np.ndarray) -> np.ndarray:
    frames = len(samples) // FRAME
    if not frames:
        return samples
    loudness = np.sqrt(
        np.mean(np.square(samples[: frames * FRAME].reshape(frames, FRAME)), axis=1)
    )
    loud = np.flatnonzero(loudness > loudness.max() * SILENCE)
    if not len(loud):
        return samples
    return samples[loud[0] * FRAME : (loud[-1] + 1) * FRAME]


def wav(samples: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(SAMPLE_RATE)
        stream.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return buffer.getvalue()


class Voice:
    def __init__(self, directory: Path) -> None:
        model, voices = (model_file(directory, *spec) for spec in (MODEL, VOICES))
        self.session = onnxruntime.InferenceSession(
            str(model), providers=["CPUExecutionProvider"]
        )
        config = json.loads(
            self.session.get_modelmeta().custom_metadata_map["kokoro_config"]
        )
        self.vocabulary = cast("dict[str, int]", config["vocab"])
        with np.load(voices) as archive:
            self.styles = np.asarray(archive[VOICE], dtype=np.float32)

    def speak(self, said: str) -> bytes:
        tokens = [
            self.vocabulary[symbol] for symbol in said if symbol in self.vocabulary
        ]
        if not tokens or len(tokens) > MAX_TOKENS:
            message = "The sentence is too long to say in one go."
            raise ValueError(message)
        outputs = self.session.run(
            None,
            {
                "input_ids": np.array([[0, *tokens, 0]], dtype=np.int64),
                "style": self.styles[len(tokens) - 1],
                "speed": np.ones(1, dtype=np.float32),
            },
        )
        return wav(trim(np.asarray(outputs[0], dtype=np.float32).ravel()))


class Speaker:
    def __init__(self, directory: Path, logger: logging.Logger) -> None:
        self.directory = directory
        self.clips = directory / "audio"
        self.logger = logger
        self.lock = Lock()
        self.work = Event()
        self.queue: list[tuple[int, int, str]] = []
        self.waiting: dict[str, Event] = {}
        self.order = itertools.count()
        self.thread: Thread | None = None
        self.voice: Voice | None = None
        self.retry_at = 0.0

    def path(self, said: str) -> Path:
        key = hashlib.sha256(f"{CACHE_VERSION}:{VOICE}:{said}".encode()).hexdigest()
        return self.clips / f"{key}.wav"

    def unavailable(self) -> bool:
        return self.voice is None and time.monotonic() < self.retry_at

    def want(self, said: str, *, urgent: bool = False) -> Event | None:
        if self.path(said).is_file() or self.unavailable():
            return None
        with self.lock:
            event = self.waiting.get(said)
            if event is None or urgent:
                heapq.heappush(
                    self.queue, (URGENT if urgent else LATER, next(self.order), said)
                )
            if event is None:
                event = self.waiting[said] = Event()
            if self.thread is None:
                self.thread = Thread(target=self._run, name="yiju-speech", daemon=True)
                self.thread.start()
            self.work.set()
        return event

    def clip(self, said: str) -> Path:
        path = self.path(said)
        if not path.is_file():
            event = self.want(said, urgent=True)
            if event is None and not path.is_file():
                message = "Pronunciation is unavailable. Try again in a few minutes."
                raise SpeechUnavailableError(message)
            if event is not None and not event.wait(WAIT_SECONDS):
                message = "Pronunciation is still being made. Try replaying it."
                raise SpeechUnavailableError(message)
            if not path.is_file():
                message = "Pronunciation is unavailable. Try replaying it in a moment."
                raise SpeechUnavailableError(message)
        with suppress(OSError):
            os.utime(path)
        return path

    def sweep(self, keep: set[Path], now: float) -> None:
        if not self.clips.is_dir():
            return
        for path in self.clips.iterdir():
            if path in keep:
                continue
            with suppress(OSError):
                if path.is_file() and path.stat().st_mtime < now - UNUSED_SECONDS:
                    path.unlink()

    def _next(self) -> tuple[str, Event] | None:
        with self.lock:
            while self.queue:
                _priority, _order, said = heapq.heappop(self.queue)
                event = self.waiting.get(said)
                if event is not None:
                    return said, event
            self.work.clear()
            return None

    def _run(self) -> None:
        while True:
            self.work.wait()
            job = self._next()
            if job is None:
                continue
            said, event = job
            try:
                self._make(said)
            except SpeechUnavailableError:
                self.logger.warning("Could not load the voice", exc_info=True)
                self._abandon()
            except Exception:
                self.logger.exception("Could not make pronunciation audio")
            finally:
                with self.lock:
                    self.waiting.pop(said, None)
                event.set()

    def _abandon(self) -> None:
        with self.lock:
            events = list(self.waiting.values())
            self.waiting.clear()
            self.queue.clear()
        for event in events:
            event.set()

    def _load(self) -> Voice:
        if self.voice is None:
            if time.monotonic() < self.retry_at:
                message = "The voice failed to load recently."
                raise SpeechUnavailableError(message)
            try:
                self.voice = Voice(self.directory / "voice")
            except ERRORS as error:
                self.retry_at = time.monotonic() + RETRY_SECONDS
                raise SpeechUnavailableError(str(error)) from error
        return self.voice

    def _make(self, said: str) -> None:
        destination = self.path(said)
        if destination.is_file():
            return
        data = self._load().speak(said)
        self.clips.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(suffix=".part", dir=self.clips)
        staging = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
            staging.replace(destination)
        finally:
            staging.unlink(missing_ok=True)


def speaker() -> Speaker:
    return cast("Speaker", current_app.extensions["yiju.speech"])


def card_speech(card: sqlite3.Row, names: Mapping[str, Name] | None) -> str:
    if card["sentence_id"] is None:
        readings = decode(card["readings"]) if card["readings"] else ()
        return phonemes(card["word"], readings[0].pinyin if readings else "")
    text, pinyin = spoken(
        card["chinese"], card["sentence_pinyin"], card["rules"], names
    )
    return phonemes(text, pinyin)


@audio.get("/audio/cards/<int:card_id>")
def pronunciation(card_id: int) -> Response:
    connection = database()
    card = connection.execute(CARD_QUERY + "WHERE c.id = ?", (card_id,)).fetchone()
    if card is None:
        abort(404)
    names = None
    if card["sentence_id"] is not None and letters(card["chinese"]):
        names = chosen(connection, card["chinese"], request.args.get("names", ""))
        if names is None:
            abort(404, "Pick names for every person in this sentence.")
    try:
        said = card_speech(card, names)
    except ValueError:
        abort(404, "Yiju cannot pronounce this card.")
    try:
        path = speaker().clip(said)
    except SpeechUnavailableError as error:
        abort(503, str(error))
    return send_file(path, mimetype="audio/wav", conditional=True)
