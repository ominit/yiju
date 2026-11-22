import asyncio
import hashlib
import heapq
import itertools
import os
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING, cast

import edge_tts
from aiohttp import ClientError
from edge_tts import communicate
from edge_tts.constants import TRUSTED_CLIENT_TOKEN
from flask import Blueprint, Response, abort, current_app, request, send_file

from yiju.core.names import NAME_SLOT, chinese_text, letters, pronoun_texts
from yiju.storage.names import chosen
from yiju.storage.queue import CARD_QUERY

from . import database

if TYPE_CHECKING:
    import logging
    import sqlite3
    from collections.abc import Mapping

    from yiju.core.names import Name

vars(communicate)["WSS_URL"] = (
    "wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"
    f"?Ocp-Apim-Subscription-Key={TRUSTED_CLIENT_TOKEN}"
)
audio = Blueprint("audio", __name__)
VOICE = "zh-CN-XiaoxiaoNeural"
CACHE_VERSION = "1"
WAIT_SECONDS = 30
OUTAGE_THRESHOLD = 3
OUTAGE_SECONDS = 120
UNUSED_SECONDS = 7 * 24 * 60 * 60
URGENT, LATER = 0, 1
ERRORS = (
    OSError,
    TimeoutError,
    ClientError,
    edge_tts.exceptions.EdgeTTSException,
    ValueError,
)


class SpeechUnavailableError(Exception):
    pass


async def synthesize(text: str, destination: Path) -> None:
    speech = edge_tts.Communicate(text, VOICE, connect_timeout=5, receive_timeout=10)
    await asyncio.wait_for(speech.save(str(destination)), timeout=20)


class Speaker:
    def __init__(self, directory: Path, logger: logging.Logger) -> None:
        self.clips = directory / "audio"
        self.logger = logger
        self.lock = Lock()
        self.work = Event()
        self.queue: list[tuple[int, int, str]] = []
        self.waiting: dict[str, Event] = {}
        self.order = itertools.count()
        self.thread: Thread | None = None
        self.failures = 0
        self.retry_at = 0.0

    def path(self, text: str) -> Path:
        key = hashlib.sha256(f"{CACHE_VERSION}:{VOICE}:{text}".encode()).hexdigest()
        return self.clips / f"{key}.mp3"

    def unavailable(self) -> bool:
        return time.monotonic() < self.retry_at

    def want(self, text: str, *, urgent: bool = False) -> Event | None:
        if self.path(text).is_file() or self.unavailable():
            return None
        with self.lock:
            event = self.waiting.get(text)
            if event is None or urgent:
                heapq.heappush(
                    self.queue, (URGENT if urgent else LATER, next(self.order), text)
                )
            if event is None:
                event = self.waiting[text] = Event()
            if self.thread is None:
                self.thread = Thread(target=self._run, name="yiju-speech", daemon=True)
                self.thread.start()
            self.work.set()
        return event

    def clip(self, text: str) -> Path:
        path = self.path(text)
        if not path.is_file():
            event = self.want(text, urgent=True)
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
                _priority, _order, text = heapq.heappop(self.queue)
                event = self.waiting.get(text)
                if event is not None:
                    return text, event
            self.work.clear()
            return None

    def _run(self) -> None:
        while True:
            self.work.wait()
            job = self._next()
            if job is None:
                continue
            text, event = job
            try:
                self._make(text)
            except ERRORS:
                self.logger.warning("Could not make pronunciation audio", exc_info=True)
                self._failed()
            except Exception:
                self.logger.exception("Could not make pronunciation audio")
            else:
                self.failures = 0
            finally:
                with self.lock:
                    self.waiting.pop(text, None)
                event.set()

    def _failed(self) -> None:
        self.failures += 1
        if self.failures < OUTAGE_THRESHOLD:
            return
        self.failures = 0
        self.retry_at = time.monotonic() + OUTAGE_SECONDS
        with self.lock:
            events = list(self.waiting.values())
            self.waiting.clear()
            self.queue.clear()
        for event in events:
            event.set()

    def _make(self, text: str) -> None:
        destination = self.path(text)
        if destination.is_file():
            return
        self.clips.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(suffix=".part", dir=self.clips)
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


def speaker() -> Speaker:
    return cast("Speaker", current_app.extensions["yiju.speech"])


def card_text(card: sqlite3.Row, names: Mapping[str, Name] | None) -> str:
    if card["sentence_id"] is None:
        return str(card["word"])
    if names is None:
        return str(card["chinese"])
    return chinese_text(card["chinese"], names)


def card_texts(card: sqlite3.Row) -> list[str]:
    if card["sentence_id"] is None:
        return [card["word"]]
    if NAME_SLOT.search(card["chinese"]):
        return []
    if letters(card["chinese"]):
        return pronoun_texts(card["chinese"], card["rules"])
    return [card["chinese"]]


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
        path = speaker().clip(card_text(card, names))
    except SpeechUnavailableError as error:
        abort(503, str(error))
    return send_file(path, mimetype="audio/mpeg", conditional=True)
