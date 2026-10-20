import hashlib
import json
import re
import sqlite3
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, cast

from yiju.core.definitions import readings_payload
from yiju.core.dictionary import decode, encode, lookup, manual
from yiju.core.normalization import normalize_text
from yiju.core.text import HAN, contains_han
from yiju.core.tokenizer import (
    han,
    parse_words,
    segment,
    segmentation_error,
    sentence_pinyin,
    word_pinyin,
)

from .database import ConflictError, library_words, transaction
from .graph import refresh
from .sentences import add_sentence, field_error
from .words import add_word

FORMAT = """\
One item per line. Blank lines and lines starting with # are ignored.

A sentence is the Chinese, then a tab or " | ", then the English translation:
    我很喜欢葡萄。<TAB>I really like grapes.
    我很喜欢葡萄。 | I really like grapes.

Spaces inside the Chinese mark word boundaries and replace automatic
segmentation for that sentence:
    我 很 喜欢 葡萄。 | I really like grapes.

A line with only Chinese words, separated by spaces, adds those words
without a sentence:
    葡萄 苹果
"""
SEPARATOR = re.compile(r"\t| \|(?= |$)")
MAX_ITEMS = 500
EDGES = re.compile(r"^\W+|\W+$")
CHANGED = "New items arrived or something changed while you were checking. Check again."


@dataclass(frozen=True, slots=True)
class Parsed:
    sentences: tuple[tuple[str, str], ...]
    words: tuple[str, ...]


class FormatError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n".join(errors))
        self.errors = errors


def clean_chinese(text: str) -> str:
    return re.sub(f"(?<=[{HAN}])\\s+(?=[{HAN}])", "", normalize_text(text))


def parse(text: str) -> Parsed:
    sentences: list[tuple[str, str]] = []
    words: list[str] = []
    errors = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = SEPARATOR.split(line, maxsplit=1)
        if len(parts) == 2:  # noqa: PLR2004
            chinese, translation = (normalize_text(part) for part in parts)
            if not contains_han(chinese):
                errors.append(
                    f"Line {number}: the part before the separator has no Chinese."
                )
            elif not translation:
                errors.append(f"Line {number}: the translation is empty.")
            else:
                sentences.append((chinese, translation))
            continue
        tokens = parse_words(line)
        if all(han(token) == token for token in tokens):
            words.extend(tokens)
        else:
            errors.append(
                f"Line {number}: expected a sentence with a translation after a tab "
                'or " | ", or Chinese words separated by spaces.'
            )
    if not errors and not sentences and not words:
        errors.append("Nothing to import.")
    if len(sentences) + len(words) > MAX_ITEMS:
        errors.append(f"Import at most {MAX_ITEMS} items at a time.")
    if errors:
        raise FormatError(errors)
    return Parsed(tuple(dict.fromkeys(sentences)), tuple(dict.fromkeys(words)))


def _vocabulary(connection: sqlite3.Connection) -> tuple[str, ...]:
    requested = {
        str(row["word"])
        for row in connection.execute(
            "SELECT word FROM pending_words WHERE requested = 1"
        )
    }
    return tuple(sorted(library_words(connection) | requested))


def stage(connection: sqlite3.Connection, text: str) -> None:
    parsed = parse(text)
    with transaction(connection):
        connection.executemany(
            "INSERT INTO pending_words(word, requested) VALUES (?, 1) "
            "ON CONFLICT(word) DO UPDATE SET requested = 1",
            [(word,) for word in parsed.words],
        )
        vocabulary = _vocabulary(connection)
        for raw, translation in parsed.sentences:
            chinese = clean_chinese(raw)
            words = segment(raw, vocabulary)
            duplicate = connection.execute(
                "SELECT 1 FROM sentences WHERE chinese = ?", (chinese,)
            ).fetchone()
            connection.execute(
                "INSERT INTO pending_sentences(chinese, words, pinyin, translation, "
                "included) VALUES (?, ?, ?, ?, ?) ON CONFLICT(chinese) DO NOTHING",
                (
                    chinese,
                    json.dumps(words, ensure_ascii=False),
                    sentence_pinyin(chinese, words),
                    translation,
                    int(duplicate is None),
                ),
            )


def version(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for table, key in (("pending_sentences", "id"), ("pending_words", "word")):
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY {key}"):  # noqa: S608
            digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


@contextmanager
def editing(
    connection: sqlite3.Connection, checked: str | None = None
) -> Generator[None]:
    with transaction(connection):
        if checked is not None and version(connection) != checked:
            raise ConflictError(CHANGED)
        yield


def _readings(row: sqlite3.Row | None, word: str) -> tuple[str, bool]:
    if row is not None and row["readings"] is not None:
        return str(row["readings"]), True
    return encode(lookup(word)), False


@dataclass(slots=True)
class PreviewWord:
    word: str
    new: bool
    included: bool
    requested: bool
    manual: bool
    found: bool
    readings: list[dict[str, object]]
    encoded: str
    pinyin: str
    meanings: str
    sentences: list[int] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Choice:
    pinyin: str
    meaning: str


@dataclass(frozen=True, slots=True)
class PreviewToken:
    text: str
    new: bool
    choices: tuple[Choice, ...]
    pinyin: str | None


def _choices(word: PreviewWord) -> tuple[Choice, ...]:
    readings = decode(word.encoded)
    if len(readings) < 2:  # noqa: PLR2004
        return ()
    return tuple(
        Choice(reading.pinyin, reading.senses[0].text if reading.senses else "")
        for reading in readings
    )


def spoken(pinyin: str, count: int) -> list[str | None]:
    parts = [
        EDGES.sub("", part)
        for part in pinyin.split()
        if any(character.isalpha() for character in part)
    ]
    if len(parts) != count:
        return [None] * count
    return list(parts)


@dataclass(frozen=True, slots=True)
class PreviewSentence:
    id: int
    chinese: str
    words: tuple[PreviewToken, ...]
    pinyin: str
    translation: str
    included: bool
    duplicate: bool
    issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Preview:
    version: str
    sentences: tuple[PreviewSentence, ...]
    words: tuple[PreviewWord, ...]
    issues: tuple[str, ...]
    adds: dict[str, int]
    ready: bool

    @property
    def empty(self) -> bool:
        return not self.sentences and not self.words

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        for word in data["words"]:
            del word["encoded"]
        return data


def _sentence_issues(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    tokens: tuple[str, ...],
    words: dict[str, PreviewWord],
) -> tuple[list[str], bool]:
    issues = []
    error = segmentation_error(row["chinese"], tokens) or field_error(
        row["chinese"], row["pinyin"], row["translation"]
    )
    if error is not None:
        issues.append(error)
    duplicate = (
        connection.execute(
            "SELECT 1 FROM sentences WHERE chinese = ?", (row["chinese"],)
        ).fetchone()
        is not None
    )
    if duplicate:
        issues.append("This sentence is already in the library.")
    for token in dict.fromkeys(tokens):
        entry = words[token]
        if entry.new and not entry.included:
            issues.append(f"{token} is excluded.")
        elif entry.new and not entry.readings:
            issues.append(f"{token} has no definition.")
    return issues, duplicate


def preview(connection: sqlite3.Connection) -> Preview:
    library = library_words(connection)
    overrides = {
        row["word"]: row for row in connection.execute("SELECT * FROM pending_words")
    }
    words: dict[str, PreviewWord] = {}

    def entry(word: str) -> PreviewWord:
        if word not in words:
            override = overrides.get(word)
            encoded, edited = _readings(override, word)
            readings = decode(encoded)
            words[word] = PreviewWord(
                word,
                word not in library,
                override is None or bool(override["included"]),
                override is not None and bool(override["requested"]),
                edited,
                bool(lookup(word)),
                readings_payload(encoded),
                encoded,
                readings[0].pinyin if readings else word_pinyin(word),
                "\n".join(
                    sense.text for reading in readings for sense in reading.senses
                )
                if edited
                else "",
            )
        return words[word]

    sentences = []
    for row in connection.execute(
        "SELECT * FROM pending_sentences ORDER BY id"
    ).fetchall():
        tokens = tuple(json.loads(row["words"]))
        included = bool(row["included"])
        for token in tokens:
            if included and row["id"] not in entry(token).sentences:
                entry(token).sentences.append(row["id"])
        issues, duplicate = _sentence_issues(
            connection, row, tokens, {token: entry(token) for token in tokens}
        )
        sentences.append(
            PreviewSentence(
                row["id"],
                row["chinese"],
                tuple(
                    PreviewToken(
                        token, entry(token).new, _choices(entry(token)), pinyin
                    )
                    for token, pinyin in zip(
                        tokens, spoken(row["pinyin"], len(tokens)), strict=True
                    )
                ),
                row["pinyin"],
                row["translation"],
                included,
                duplicate,
                tuple(issues) if included else (),
            )
        )
    for word, override in overrides.items():
        if override["requested"]:
            entry(word)
    listed = tuple(
        word
        for word in words.values()
        if word.new and (word.sentences or word.requested)
    )
    issues = tuple(
        f"Sentence {sentence.id}: {issue}"
        for sentence in sentences
        for issue in sentence.issues
    ) + tuple(
        f"{word.word} has no definition."
        for word in listed
        if word.included and not word.readings and not word.sentences
    )
    adds = {
        "sentences": sum(
            sentence.included and not sentence.duplicate for sentence in sentences
        ),
        "words": sum(word.included for word in listed),
    }
    return Preview(
        version(connection),
        tuple(sentences),
        listed,
        issues,
        adds,
        not issues and any(adds.values()),
    )


def _sentence(connection: sqlite3.Connection, sentence_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM pending_sentences WHERE id = ?", (sentence_id,)
    ).fetchone()
    if row is None:
        message = f"There is no pending sentence {sentence_id}."
        raise LookupError(message)
    return cast("sqlite3.Row", row)


def edit_sentence(
    connection: sqlite3.Connection,
    sentence_id: int,
    *,
    words: str | None = None,
    pinyin: str | None = None,
    translation: str | None = None,
    included: bool | None = None,
) -> None:
    row = _sentence(connection, sentence_id)
    tokens = json.loads(row["words"])
    new_pinyin = row["pinyin"] if pinyin is None else normalize_text(pinyin)
    if words is not None:
        parsed = list(parse_words(words))
        if parsed != tokens and (pinyin is None or new_pinyin == row["pinyin"]):
            new_pinyin = sentence_pinyin(row["chinese"], tuple(parsed))
        tokens = parsed
    connection.execute(
        "UPDATE pending_sentences SET words = ?, pinyin = ?, translation = ?, "
        "included = ? WHERE id = ?",
        (
            json.dumps(tokens, ensure_ascii=False),
            new_pinyin,
            row["translation"] if translation is None else normalize_text(translation),
            row["included"] if included is None else int(included),
            sentence_id,
        ),
    )


def edit_word(
    connection: sqlite3.Connection,
    word: str,
    *,
    pinyin: str | None = None,
    meanings: str | None = None,
    included: bool | None = None,
) -> None:
    row = connection.execute(
        "SELECT * FROM pending_words WHERE word = ?", (word,)
    ).fetchone()
    readings = row["readings"] if row is not None else None
    if meanings is not None:
        current = decode(readings) if readings is not None else lookup(word)
        reading = (
            pinyin if pinyin is not None else (current[0].pinyin if current else "")
        )
        readings = encode(manual(reading, meanings)) if meanings.strip() else None
    connection.execute(
        "INSERT INTO pending_words(word, readings, included) VALUES (?, ?, ?) "
        "ON CONFLICT(word) DO UPDATE SET "
        "readings = excluded.readings, included = excluded.included",
        (
            word,
            readings,
            (row["included"] if row is not None else 1)
            if included is None
            else int(included),
        ),
    )


def remove_sentence(connection: sqlite3.Connection, sentence_id: int) -> None:
    _sentence(connection, sentence_id)
    connection.execute("DELETE FROM pending_sentences WHERE id = ?", (sentence_id,))


def _clear(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM pending_sentences")
    connection.execute("DELETE FROM pending_words")


def discard(connection: sqlite3.Connection, checked: str) -> None:
    with editing(connection, checked):
        _clear(connection)


def _add_words(
    connection: sqlite3.Connection, words: Iterable[PreviewWord], now: datetime
) -> set[str]:
    return {
        word.word
        for word in words
        if word.included
        and add_word(
            connection,
            word.word,
            word.encoded,
            "manual" if word.manual else "cedict",
            now,
        )
    }


def _add_sentence(
    connection: sqlite3.Connection, sentence: PreviewSentence, now: datetime
) -> tuple[str, ...]:
    words = tuple(token.text for token in sentence.words)
    if (
        add_sentence(
            connection,
            sentence.chinese,
            words,
            sentence.pinyin,
            sentence.translation,
            now,
        )
        is None
    ):
        return ()
    return words


def approve(
    connection: sqlite3.Connection, checked: str, now: datetime
) -> dict[str, int]:
    with editing(connection, checked):
        result = preview(connection)
        if not result.ready:
            message = "Fix the problems listed above before approving."
            raise ConflictError(message)
        touched = _add_words(connection, result.words, now)
        added_words = len(touched)
        added_sentences = 0
        for sentence in result.sentences:
            if not sentence.included or sentence.duplicate:
                continue
            if words := _add_sentence(connection, sentence, now):
                added_sentences += 1
                touched.update(words)
        _clear(connection)
        refresh(connection, touched)
    return {"words": added_words, "sentences": added_sentences}


def approve_sentence(
    connection: sqlite3.Connection, checked: str, sentence_id: int, now: datetime
) -> dict[str, int]:
    with editing(connection, checked):
        result = preview(connection)
        sentence = next(
            (sentence for sentence in result.sentences if sentence.id == sentence_id),
            None,
        )
        if sentence is None:
            message = f"There is no pending sentence {sentence_id}."
            raise ConflictError(message)
        if not sentence.included:
            message = "Include the sentence before approving it."
            raise ConflictError(message)
        if sentence.issues:
            message = "Fix the problems in this sentence before approving it."
            raise ConflictError(message)
        tokens = {token.text for token in sentence.words}
        added = _add_words(
            connection, (word for word in result.words if word.word in tokens), now
        )
        _add_sentence(connection, sentence, now)
        connection.execute("DELETE FROM pending_sentences WHERE id = ?", (sentence_id,))
        connection.executemany(
            "DELETE FROM pending_words WHERE word = ?", [(word,) for word in added]
        )
        refresh(connection, tokens)
    return {"words": len(added), "sentences": 1}


def waiting(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute(
            "SELECT (SELECT count(*) FROM pending_sentences) + "
            "(SELECT count(*) FROM pending_words WHERE requested = 1)"
        ).fetchone()[0]
    )
