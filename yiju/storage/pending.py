import hashlib
import json
import re
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, cast

from yiju.core.definitions import brief_readings, readings_payload
from yiju.core.dictionary import decode, encode, lookup, manual
from yiju.core.names import (
    GENDERS,
    SLOT,
    clean_rules,
    english_spelling,
    is_slot,
    name_error,
    name_pinyin,
    slot_error,
    strip_slots,
)
from yiju.core.normalization import normalize_text
from yiju.core.text import contains_han
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
from .names import add_name, counts, needs
from .names import field_error as name_field_error
from .sentences import add_sentence, field_error
from .words import add_word

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Generator, Iterable
    from datetime import datetime

FORMAT = """\
One item per line. Blank lines and lines starting with # are ignored.

A sentence is the Chinese, then a tab or " | ", then the English translation:
    <Chinese><TAB><English>
    <Chinese> | <English>

Spaces inside the Chinese mark word boundaries and replace automatic
segmentation for that sentence. A space next to punctuation changes nothing.
The spaces around " | " belong to the separator:
    <word> <word> <word> | <English>

A line with only Chinese words, separated by spaces, adds those words
without a sentence:
    <word> <word>

A person is a slot, which study fills with a random name from the Names tab.
Write {A}, {B} and so on where the names go, with the same letters in the
Chinese and the translation.

{A.他} becomes 他 or 她 to match A. In the translation, {A.they}, {A.them},
{A.their}, {A.theirs} and {A.themself} become he or she, him or her and so
on. A capital letter carries over, so {A.They} starts a sentence. Write the
verb that goes with he or she, as in "{A.They} is".

An optional third part sets gender rules, separated by spaces: A=male,
A=female, A=B for the same gender and A!=B for different genders:
    <Chinese> | <English> | A=male B!=A
"""
NAME_FORMAT = """\
One name per line: the full name with the surname first, a space, then male
or female.
    <characters> male
    <characters> female

Yiju fills in the pinyin and the English spelling. Both can be changed on the
check screen.
"""
SEPARATOR = re.compile(r"\t| \|(?= |$)")
NAME_LINE = re.compile(r"(\S+)\s+(male|female)")
MAX_ITEMS = 500
EDGES = re.compile(r"^\W+|\W+$")
CHANGED = "New items arrived or something changed while you were checking. Check again."


@dataclass(frozen=True, slots=True)
class Parsed:
    sentences: tuple[tuple[str, str, str], ...]
    words: tuple[str, ...]


class FormatError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n".join(errors))
        self.errors = errors


def clean_chinese(text: str) -> str:
    return re.sub(r"(?<![A-Za-z0-9])\s+|\s++(?![A-Za-z0-9])", "", normalize_text(text))


def _lines(text: str) -> Iterable[tuple[int, str]]:
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if line and not line.startswith("#"):
            yield number, line


def parse(text: str) -> Parsed:
    sentences: list[tuple[str, str, str]] = []
    words: list[str] = []
    errors = []
    for number, line in _lines(text):
        parts = SEPARATOR.split(line, maxsplit=2)
        if len(parts) > 1:
            chinese, translation, *rest = (normalize_text(part) for part in parts)
            rules = clean_rules(rest[0]) if rest else ""
            if not contains_han(strip_slots(chinese)):
                errors.append(
                    f"Line {number}: the part before the separator has no Chinese."
                )
            elif not translation:
                errors.append(f"Line {number}: the translation is empty.")
            else:
                sentences.append((chinese, translation, rules))
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


@dataclass(frozen=True, slots=True)
class Staged:
    sentences: tuple[int, ...] = ()
    words: tuple[str, ...] = ()
    names: tuple[int, ...] = ()
    waiting: tuple[str, ...] | None = None


def _staged(connection: sqlite3.Connection, table: str, chinese: str) -> int:
    row = connection.execute(
        f"SELECT id FROM {table} WHERE chinese = ?",  # noqa: S608
        (chinese,),
    ).fetchone()
    return int(row["id"])


def stage(connection: sqlite3.Connection, text: str) -> Staged:
    parsed = parse(text)
    ids: list[int] = []
    waiting: list[str] = []
    with transaction(connection):
        connection.executemany(
            "INSERT INTO pending_words(word, requested) VALUES (?, 1) "
            "ON CONFLICT(word) DO UPDATE SET requested = 1",
            [(word,) for word in parsed.words],
        )
        vocabulary = _vocabulary(connection)
        for raw, translation, rules in parsed.sentences:
            chinese = clean_chinese(raw)
            words = segment(raw, vocabulary)
            inserted = connection.execute(
                "INSERT INTO pending_sentences(chinese, words, pinyin, translation, "
                "rules) VALUES (?, ?, ?, ?, ?) ON CONFLICT(chinese) DO NOTHING",
                (
                    chinese,
                    json.dumps(words, ensure_ascii=False),
                    sentence_pinyin(chinese, words),
                    translation,
                    rules,
                ),
            )
            if not inserted.rowcount:
                waiting.append(chinese)
            ids.append(_staged(connection, "pending_sentences", chinese))
    return Staged(tuple(ids), parsed.words, (), tuple(waiting))


def parse_names(text: str) -> tuple[tuple[str, str], ...]:
    names: dict[str, str] = {}
    errors = []
    for number, line in _lines(text):
        match = NAME_LINE.fullmatch(normalize_text(line))
        error = (
            'expected the characters, a space, then "male" or "female".'
            if match is None
            else name_error(match.group(1))
        )
        if error is not None or match is None:
            errors.append(f"Line {number}: {error}")
        else:
            names[match.group(1)] = match.group(2)
    if not errors and not names:
        errors.append("Nothing to import.")
    if len(names) > MAX_ITEMS:
        errors.append(f"Import at most {MAX_ITEMS} names at a time.")
    if errors:
        raise FormatError(errors)
    return tuple(names.items())


def stage_names(connection: sqlite3.Connection, text: str) -> Staged:
    parsed = parse_names(text)
    ids: list[int] = []
    waiting: list[str] = []
    with transaction(connection):
        for chinese, gender in parsed:
            pinyin = name_pinyin(chinese)
            inserted = connection.execute(
                "INSERT INTO pending_names(chinese, pinyin, english, gender) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(chinese) DO NOTHING",
                (chinese, pinyin, english_spelling(pinyin), gender),
            )
            if not inserted.rowcount:
                waiting.append(chinese)
            ids.append(_staged(connection, "pending_names", chinese))
    return Staged(names=tuple(ids), waiting=tuple(waiting))


def version(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for table, key in (
        ("pending_sentences", "id"),
        ("pending_words", "word"),
        ("pending_names", "id"),
    ):
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
    pinyin: str | None
    new: bool
    slot: bool
    meanings: tuple[str, ...] | None
    choices: tuple[Choice, ...]


def _meanings(word: PreviewWord, pinyin: str | None) -> tuple[str, ...] | None:
    if pinyin is None:
        return None
    return next(
        (
            tuple(cast("list[str]", reading["meanings"]))
            for reading in brief_readings(word.encoded)
            if str(reading["pinyin"]).lower() == pinyin.lower()
        ),
        None,
    )


def _choices(word: PreviewWord) -> tuple[Choice, ...]:
    readings = decode(word.encoded)
    if len(readings) < 2:  # noqa: PLR2004
        return ()
    return tuple(
        Choice(reading.pinyin, reading.senses[0].text if reading.senses else "")
        for reading in readings
    )


def _groups(pinyin: str, chinese: str) -> list[str]:
    groups = []
    for raw in pinyin.split():
        part = EDGES.sub("", raw)
        if any(character.isalpha() for character in part) and (
            SLOT.search(raw) is not None or part not in chinese
        ):
            groups.append(part)
    return groups


def spoken(pinyin: str, chinese: str, count: int) -> list[str | None]:
    parts = _groups(pinyin, chinese)
    if len(parts) != count:
        return [None] * count
    return list(parts)


@dataclass(frozen=True, slots=True)
class PreviewSentence:
    id: int
    chinese: str
    pinyin: str
    translation: str
    rules: str
    people: bool
    duplicate: bool
    needs_names: str | None
    issues: tuple[str, ...]
    words: tuple[PreviewToken, ...]


@dataclass(frozen=True, slots=True)
class PreviewName:
    id: int
    chinese: str
    pinyin: str
    english: str
    gender: str
    duplicate: bool
    issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Preview:
    ready: bool
    issues: tuple[str, ...]
    adds: dict[str, int]
    sentences: tuple[PreviewSentence, ...]
    words: tuple[PreviewWord, ...]
    names: tuple[PreviewName, ...]
    version: str

    @property
    def empty(self) -> bool:
        return not self.sentences and not self.words and not self.names

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        for sentence in data["sentences"]:
            sentence["words"] = [
                {"text": token["text"], "slot": True}
                if token["slot"]
                else {
                    key: value
                    for key, value in token.items()
                    if key != "slot" and (key != "choices" or value)
                }
                for token in sentence["words"]
            ]
        data["words"] = [
            {
                "word": word.word,
                "found": word.found,
                "requested": word.requested,
                "manual": word.manual,
                "sentences": word.sentences,
            }
            | ({} if word.sentences else {"readings": brief_readings(word.encoded)})
            for word in self.words
        ]
        return data


def _grouping_error(pinyin: str, chinese: str, tokens: tuple[str, ...]) -> str | None:
    found = len(_groups(pinyin, chinese))
    if found == len(tokens):
        return None
    return (
        "The pinyin needs one group per word or slot, in order. This sentence has "
        f"{len(tokens)} and the pinyin has {found}."
    )


def _sentence_issues(
    row: sqlite3.Row, tokens: tuple[str, ...], words: dict[str, PreviewWord]
) -> tuple[str, ...]:
    error = (
        segmentation_error(row["chinese"], tokens)
        or _grouping_error(row["pinyin"], row["chinese"], tokens)
        or field_error(row["chinese"], row["pinyin"], row["translation"])
        or slot_error(row["chinese"], row["translation"], row["rules"], row["pinyin"])
    )
    return (() if error is None else (error,)) + tuple(
        f"{word} has no definition."
        for word, entry in words.items()
        if entry.new and not entry.readings
    )


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

    names = _preview_names(connection)
    available = counts(connection) + Counter(
        name.gender for name in names if not name.duplicate
    )
    sentences = []
    for row in connection.execute(
        "SELECT * FROM pending_sentences ORDER BY id"
    ).fetchall():
        tokens = tuple(json.loads(row["words"]))
        words_only = tuple(dict.fromkeys(t for t in tokens if not is_slot(t)))
        duplicate = (
            connection.execute(
                "SELECT 1 FROM sentences WHERE chinese = ?", (row["chinese"],)
            ).fetchone()
            is not None
        )
        if not duplicate:
            for token in words_only:
                entry(token).sentences.append(row["id"])
        sentences.append(
            PreviewSentence(
                id=row["id"],
                chinese=row["chinese"],
                pinyin=row["pinyin"],
                translation=row["translation"],
                rules=row["rules"],
                people=any(is_slot(token) for token in tokens) or bool(row["rules"]),
                duplicate=duplicate,
                needs_names=needs(row["chinese"], row["rules"], available),
                issues=()
                if duplicate
                else _sentence_issues(
                    row, tokens, {token: entry(token) for token in words_only}
                ),
                words=tuple(
                    PreviewToken(
                        token, None, new=False, slot=True, meanings=None, choices=()
                    )
                    if is_slot(token)
                    else PreviewToken(
                        token,
                        pinyin,
                        new=entry(token).new,
                        slot=False,
                        meanings=_meanings(entry(token), pinyin),
                        choices=_choices(entry(token)),
                    )
                    for token, pinyin in zip(
                        tokens,
                        spoken(row["pinyin"], row["chinese"], len(tokens)),
                        strict=True,
                    )
                ),
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
        if not word.readings and not word.sentences
    )
    issues += tuple(issue for name in names for issue in name.issues)
    adds = {
        "sentences": sum(not sentence.duplicate for sentence in sentences),
        "words": len(listed),
        "names": sum(not name.duplicate for name in names),
    }
    return Preview(
        ready=not issues and any(adds.values()),
        issues=issues,
        adds=adds,
        sentences=tuple(sentences),
        words=listed,
        names=names,
        version=version(connection),
    )


def _preview_names(connection: sqlite3.Connection) -> tuple[PreviewName, ...]:
    names = []
    for row in connection.execute(
        "SELECT p.*, EXISTS (SELECT 1 FROM names n WHERE n.chinese = p.chinese) "
        "AS duplicate FROM pending_names p ORDER BY p.id"
    ):
        duplicate = bool(row["duplicate"])
        error = name_field_error(
            row["chinese"], row["pinyin"], row["english"], row["gender"]
        )
        names.append(
            PreviewName(
                row["id"],
                row["chinese"],
                row["pinyin"],
                row["english"],
                row["gender"],
                duplicate,
                (error,) if error is not None and not duplicate else (),
            )
        )
    return tuple(names)


def _sentence(connection: sqlite3.Connection, sentence_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM pending_sentences WHERE id = ?", (sentence_id,)
    ).fetchone()
    if row is None:
        message = f"There is no pending sentence {sentence_id}."
        raise LookupError(message)
    return cast("sqlite3.Row", row)


def words_error(
    connection: sqlite3.Connection, sentence_id: int, words: str
) -> str | None:
    row = _sentence(connection, sentence_id)
    return segmentation_error(row["chinese"], parse_words(words))


def edit_sentence(
    connection: sqlite3.Connection,
    sentence_id: int,
    *,
    words: str | None = None,
    pinyin: str | None = None,
    translation: str | None = None,
    rules: str | None = None,
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
        "rules = ? WHERE id = ?",
        (
            json.dumps(tokens, ensure_ascii=False),
            new_pinyin,
            row["translation"] if translation is None else normalize_text(translation),
            row["rules"] if rules is None else clean_rules(rules),
            sentence_id,
        ),
    )


def edit_word(
    connection: sqlite3.Connection,
    word: str,
    *,
    pinyin: str | None = None,
    meanings: str | None = None,
) -> None:
    if meanings is None:
        return
    row = connection.execute(
        "SELECT * FROM pending_words WHERE word = ?", (word,)
    ).fetchone()
    readings = row["readings"] if row is not None else None
    current = decode(readings) if readings is not None else lookup(word)
    reading = pinyin if pinyin is not None else (current[0].pinyin if current else "")
    connection.execute(
        "INSERT INTO pending_words(word, readings) VALUES (?, ?) "
        "ON CONFLICT(word) DO UPDATE SET readings = excluded.readings",
        (word, encode(manual(reading, meanings)) if meanings.strip() else None),
    )


def remove_sentence(connection: sqlite3.Connection, sentence_id: int) -> None:
    _sentence(connection, sentence_id)
    connection.execute("DELETE FROM pending_sentences WHERE id = ?", (sentence_id,))


def remove_word(connection: sqlite3.Connection, word: str) -> None:
    entry = next(
        (item for item in preview(connection).words if item.word == word), None
    )
    if entry is None or not entry.requested:
        message = f"{word} is not a word waiting on its own."
        raise LookupError(message)
    if entry.sentences:
        message = f"{word} is in a waiting sentence, so it goes with that sentence."
        raise ValueError(message)
    connection.execute("DELETE FROM pending_words WHERE word = ?", (word,))


def _pending_name(connection: sqlite3.Connection, name_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM pending_names WHERE id = ?", (name_id,)
    ).fetchone()
    if row is None:
        message = f"There is no pending name {name_id}."
        raise LookupError(message)
    return cast("sqlite3.Row", row)


def edit_name(
    connection: sqlite3.Connection,
    name_id: int,
    *,
    pinyin: str | None = None,
    english: str | None = None,
    gender: str | None = None,
) -> None:
    row = _pending_name(connection, name_id)
    if gender is not None and gender not in GENDERS:
        message = "Gender must be male or female."
        raise ValueError(message)
    connection.execute(
        "UPDATE pending_names SET pinyin = ?, english = ?, gender = ? WHERE id = ?",
        (
            row["pinyin"] if pinyin is None else normalize_text(pinyin),
            row["english"] if english is None else normalize_text(english),
            row["gender"] if gender is None else gender,
            name_id,
        ),
    )


def remove_name(connection: sqlite3.Connection, name_id: int) -> None:
    _pending_name(connection, name_id)
    connection.execute("DELETE FROM pending_names WHERE id = ?", (name_id,))


def _clear(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM pending_sentences")
    connection.execute("DELETE FROM pending_words")
    connection.execute("DELETE FROM pending_names")


def discard(connection: sqlite3.Connection, checked: str) -> None:
    with editing(connection, checked):
        _clear(connection)


def _add_words(
    connection: sqlite3.Connection, words: Iterable[PreviewWord], now: datetime
) -> set[str]:
    return {
        word.word
        for word in words
        if add_word(
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
    words = tuple(token.text for token in sentence.words if not token.slot)
    if (
        add_sentence(
            connection,
            sentence.chinese,
            words,
            sentence.pinyin,
            sentence.translation,
            now,
            sentence.rules,
        )
        is None
    ):
        return ()
    return words


def _add_name(connection: sqlite3.Connection, name: PreviewName, now: datetime) -> bool:
    return add_name(
        connection, name.chinese, name.pinyin, name.english, name.gender, now
    )


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
            if sentence.duplicate:
                continue
            if words := _add_sentence(connection, sentence, now):
                added_sentences += 1
                touched.update(words)
        added_names = sum(
            _add_name(connection, name, now)
            for name in result.names
            if not name.duplicate
        )
        _clear(connection)
        refresh(connection, touched)
    return {"words": added_words, "sentences": added_sentences, "names": added_names}


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
        if sentence.duplicate:
            message = "This sentence is already in your library."
            raise ConflictError(message)
        if sentence.issues:
            message = "Fix the problems in this sentence before approving it."
            raise ConflictError(message)
        tokens = {token.text for token in sentence.words if not token.slot}
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


def approve_name(
    connection: sqlite3.Connection, checked: str, name_id: int, now: datetime
) -> None:
    with editing(connection, checked):
        name = next(
            (name for name in preview(connection).names if name.id == name_id), None
        )
        if name is None:
            message = f"There is no pending name {name_id}."
            raise ConflictError(message)
        if name.issues:
            message = "Fix the problems with this name before approving it."
            raise ConflictError(message)
        if name.duplicate:
            message = f"{name.chinese} is already one of your names."
            raise ConflictError(message)
        _add_name(connection, name, now)
        connection.execute("DELETE FROM pending_names WHERE id = ?", (name_id,))


def waiting(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute(
            "SELECT (SELECT count(*) FROM pending_sentences) + "
            "(SELECT count(*) FROM pending_words WHERE requested = 1) + "
            "(SELECT count(*) FROM pending_names)"
        ).fetchone()[0]
    )
