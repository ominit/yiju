import re
import time
from collections import Counter
from contextlib import closing
from datetime import timedelta
from threading import Event, Thread
from typing import TYPE_CHECKING

from yiju.core import dictionary
from yiju.core.scheduling import now_utc
from yiju.core.text import HAN
from yiju.storage.database import connect
from yiju.storage.queue import due_before

from . import audio, strokes

if TYPE_CHECKING:
    import logging
    import sqlite3
    from pathlib import Path

RETRY_SECONDS = 600
REFRESH_SECONDS = 3600
MAX_ATTEMPTS = 3
AHEAD = timedelta(days=1)
STROKE_TEXTS = "SELECT word FROM words UNION SELECT chinese FROM sentences"


class Prefill:
    def __init__(
        self, database: Path, logger: logging.Logger, speaker: audio.Speaker
    ) -> None:
        self.database = database
        self.directory = database.resolve().parent
        self.logger = logger
        self.speaker = speaker
        self.wake = Event()
        self.attempts: Counter[str] = Counter()

    def start(self) -> None:
        self.wake.set()
        Thread(target=self._run, name="yiju-prefill", daemon=True).start()

    def notify(self) -> None:
        self.wake.set()

    def _run(self) -> None:
        timeout = None
        while True:
            self.wake.wait(timeout)
            self.wake.clear()
            try:
                complete = self.fill()
            except Exception:
                self.logger.exception("Could not prefill caches")
                complete = False
            timeout = REFRESH_SECONDS if complete else RETRY_SECONDS

    def fill(self) -> bool:
        with closing(connect(self.database)) as connection:
            cards = due_before(connection, now_utc() + AHEAD)
            characters = sorted(
                {
                    character
                    for row in connection.execute(STROKE_TEXTS)
                    for character in re.findall(f"[{HAN}]", row[0])
                }
            )
        filled_dictionary = self._fill_dictionary()
        filled_strokes = self._fill_strokes(characters)
        filled_audio = self._fill_audio(cards)
        return filled_dictionary and filled_strokes and filled_audio

    def _fill_dictionary(self) -> bool:
        try:
            dictionary.ensure()
        except dictionary.DictionaryUnavailableError:
            self.logger.warning("Could not download CC-CEDICT", exc_info=True)
            return False
        return True

    def _pending(self, key: str, path: Path) -> bool:
        return self.attempts[key] < MAX_ATTEMPTS and not path.is_file()

    def _fill_strokes(self, characters: list[str]) -> bool:
        directory = self.directory / "strokes"
        for character in characters:
            key = f"strokes:{character}"
            if not self._pending(key, strokes.character_path(character, directory)):
                continue
            try:
                strokes.cached_character(character, directory)
            except LookupError:
                self.attempts[key] = MAX_ATTEMPTS
            except strokes.ERRORS:
                self.attempts[key] += 1
                self.logger.warning("Could not prefill stroke order", exc_info=True)
                return False
        return True

    def _fill_audio(self, cards: list[sqlite3.Row]) -> bool:
        keep: set[Path] = set()
        for card in cards:
            for text in audio.card_texts(card):
                keep.add(self.speaker.path(text))
                self.speaker.want(text)
        self.speaker.sweep(keep, time.time())
        return True
