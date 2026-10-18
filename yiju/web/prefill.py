import logging
import re
from collections import Counter
from contextlib import closing
from pathlib import Path
from threading import Event, Thread

from yiju.core.text import HAN
from yiju.storage.database import connect

from . import audio, strokes

RETRY_SECONDS = 600
MAX_ATTEMPTS = 3
AUDIO_TEXTS = (
    "SELECT COALESCE(s.chinese, c.word) AS text FROM cards c "
    "LEFT JOIN sentences s ON s.id = c.sentence_id "
    "WHERE s.id IS NULL OR s.status = 'accepted' "
    "GROUP BY text ORDER BY MIN(c.due), text"
)
STROKE_TEXTS = "SELECT word FROM entries UNION SELECT chinese FROM sentences"


class Prefill:
    def __init__(self, database: Path, logger: logging.Logger) -> None:
        self.database = database
        self.directory = database.resolve().parent
        self.logger = logger
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
            timeout = None if complete else RETRY_SECONDS

    def fill(self) -> bool:
        with closing(connect(self.database)) as connection:
            texts = [row[0] for row in connection.execute(AUDIO_TEXTS)]
            characters = sorted(
                {
                    character
                    for row in connection.execute(STROKE_TEXTS)
                    for character in re.findall(f"[{HAN}]", row[0])
                }
            )
        filled_strokes = self._fill_strokes(characters)
        filled_audio = self._fill_audio(texts)
        return filled_strokes and filled_audio

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

    def _fill_audio(self, texts: list[str]) -> bool:
        directory = self.directory / "audio"
        for text in texts:
            key = f"audio:{text}"
            if not self._pending(key, audio.audio_path(text, directory)):
                continue
            if not audio.available(text, directory):
                return False
            try:
                audio.generate(text, directory)
            except audio.ERRORS:
                self.attempts[key] += 1
                self.logger.warning("Could not prefill pronunciation", exc_info=True)
                return False
        return True
