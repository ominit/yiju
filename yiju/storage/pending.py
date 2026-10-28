import hashlib
import json
import re
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any, cast

from yiju.core.definitions import brief_readings, readings_payload
from yiju.core.dictionary import decode, encode, lookup, manual
from yiju.core.names import (
    GENDERS,
    SLOT,
    clean_rules,
    english_spelling,
    is_slot,
    label,
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
    with_slots,
    word_pinyin,
)

from .cards import card_ids, review_count
from .database import ConflictError, library_words, transaction
from .graph import refresh, sentence_state
from .names import add_name, counts, needs
from .names import field_error as name_field_error
from .sentences import (
    add_sentence,
    delete_sentence,
    field_error,
    replace_sentence,
    sentence_status,
    words_of,
)
from .words import add_word, delete_word, set_readings
from .words import status as word_status

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Generator, Iterable, Mapping
    from datetime import datetime

type Entry = Callable[[str], PreviewWord]
type Row = sqlite3.Row | Mapping[str, Any]

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
    changes: tuple[int, ...] = ()


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


LIBRARY_TARGETS = (
    "SELECT 'sentence', s.id, s.pinyin, s.translation, s.rules, "
    "(SELECT group_concat(word, ' ') FROM (SELECT word FROM sentence_words "
    "WHERE sentence_id = s.id ORDER BY position)) FROM sentences s "
    "WHERE s.id IN (SELECT CAST(target AS INTEGER) FROM pending_changes "
    "WHERE kind = 'sentence') "
    "UNION ALL SELECT 'word', w.word, w.readings, NULL, NULL, NULL FROM words w "
    "WHERE w.word IN (SELECT target FROM pending_changes WHERE kind = 'word') "
    "ORDER BY 1, 2"
)


def version(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for table, key in (
        ("pending_sentences", "id"),
        ("pending_words", "word"),
        ("pending_names", "id"),
        ("pending_changes", "id"),
    ):
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY {key}"):  # noqa: S608
            digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
        digest.update(b"\0")
    for row in connection.execute(LIBRARY_TARGETS):
        digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
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
    changes: list[int] = field(default_factory=list)
    status: str | None = None


@dataclass(frozen=True, slots=True)
class Choice:
    pinyin: str
    meaning: str
    meanings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreviewToken:
    text: str
    pinyin: str | None
    new: bool
    slot: bool
    meanings: tuple[str, ...] | None
    choices: tuple[Choice, ...]
    status: str | None = None


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
        Choice(
            reading.pinyin,
            reading.senses[0].text if reading.senses else "",
            tuple(cast("list[str]", brief["meanings"])),
        )
        for reading, brief in zip(readings, brief_readings(word.encoded), strict=True)
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
    starts: str = "locked"


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
class Use:
    chinese: str
    translation: str
    waiting: bool = False
    change: int | None = None
    action: str | None = None
    keeps: bool = True

    @property
    def frees(self) -> bool:
        return self.change is not None and not self.keeps


@dataclass(frozen=True, slots=True)
class PreviewChange:
    id: int
    kind: str
    target: str
    action: str
    issues: tuple[str, ...]
    reviews: int = 0
    before: PreviewSentence | None = None
    after: PreviewSentence | None = None
    current: str | None = None
    proposed: str | None = None
    pinyin: str = ""
    meanings: str = ""
    status: str | None = None
    hidden: bool = False
    uses: tuple[Use, ...] = ()
    waits_on: tuple[int, ...] = ()

    @property
    def readings(self) -> list[dict[str, object]]:
        return [] if self.current is None else readings_payload(self.current)

    @property
    def proposed_readings(self) -> list[dict[str, object]]:
        return [] if self.proposed is None else readings_payload(self.proposed)

    @property
    def removed(self) -> set[str]:
        if self.before is None or self.after is None:
            return set()
        return {token.text for token in self.before.words} - {
            token.text for token in self.after.words
        }

    def to_json(self) -> dict[str, object]:
        data: dict[str, object] = {
            "id": self.id,
            "kind": self.kind,
            "target": int(self.target) if self.kind == "sentence" else self.target,
            "action": self.action,
            "issues": list(self.issues),
        }
        if self.kind == "word":
            if self.action == "remove":
                return (
                    data
                    | {
                        "readings": brief_readings(self.current or "[]"),
                        "reviews": self.reviews,
                    }
                    | ({"waits_on": list(self.waits_on)} if self.waits_on else {})
                )
            return data | {
                "before": brief_readings(self.current or "[]"),
                "after": brief_readings(self.proposed or "[]"),
            }
        if self.before is None:
            return data
        data["chinese"] = self.before.chinese
        if self.after is None:
            return data | {
                "translation": self.before.translation,
                "reviews": self.reviews,
            }
        return data | {
            "before": {
                "words": [token.text for token in self.before.words],
                "pinyin": self.before.pinyin,
                "translation": self.before.translation,
                "rules": self.before.rules,
            },
            "after": {
                "words": _tokens_json(asdict(self.after)["words"]),
                "pinyin": self.after.pinyin,
                "translation": self.after.translation,
                "rules": self.after.rules,
            },
        }


def _tokens_json(tokens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"text": token["text"], "slot": True}
        if token["slot"]
        else {
            key: [
                {"pinyin": choice["pinyin"], "meaning": choice["meaning"]}
                for choice in value
            ]
            if key == "choices"
            else value
            for key, value in token.items()
            if key != "slot" and (key != "choices" or value)
        }
        for token in tokens
    ]


@dataclass(frozen=True, slots=True)
class Preview:
    ready: bool
    issues: tuple[str, ...]
    adds: dict[str, int]
    sentences: tuple[PreviewSentence, ...]
    words: tuple[PreviewWord, ...]
    names: tuple[PreviewName, ...]
    version: str
    changes: tuple[PreviewChange, ...] = ()
    problems: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def section(self, name: str) -> list[Any]:
        if name == "sentences":
            return list(self.sentences)
        if name == "words":
            return [word for word in self.words if word.requested]
        if name == "names":
            return list(self.names)
        return [change for change in self.changes if _section(change) == name]

    def wanted(self, word: PreviewWord, section: str | None) -> bool:
        if section is None:
            return True
        return {
            "sentences": bool(word.sentences),
            "words": word.requested,
            "edits": bool(word.changes),
        }.get(section, False)

    @property
    def empty(self) -> bool:
        return (
            not self.sentences
            and not self.words
            and not self.names
            and not self.changes
        )

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        for sentence in data["sentences"]:
            sentence["words"] = _tokens_json(sentence["words"])
        data["words"] = [
            {
                "word": word.word,
                "found": word.found,
                "requested": word.requested,
                "manual": word.manual,
                "sentences": word.sentences,
                "changes": word.changes,
            }
            | (
                {}
                if word.sentences or word.changes
                else {"readings": brief_readings(word.encoded)}
            )
            for word in self.words
        ]
        data["changes"] = [change.to_json() for change in self.changes]
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
    row: Row, tokens: tuple[str, ...], words: dict[str, PreviewWord]
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


def _preview_tokens(
    row: Row, tokens: tuple[str, ...], entry: Entry
) -> tuple[PreviewToken, ...]:
    return tuple(
        PreviewToken(token, None, new=False, slot=True, meanings=None, choices=())
        if is_slot(token)
        else PreviewToken(
            token,
            pinyin,
            new=entry(token).new,
            slot=False,
            meanings=_meanings(entry(token), pinyin),
            choices=_choices(entry(token)),
            status=entry(token).status,
        )
        for token, pinyin in zip(
            tokens, spoken(row["pinyin"], row["chinese"], len(tokens)), strict=True
        )
    )


def _preview_sentence(
    row: Row,
    tokens: tuple[str, ...],
    entry: Entry,
    available: Counter[str],
    *,
    duplicate: bool = False,
    checked: bool = True,
) -> PreviewSentence:
    words_only = tuple(dict.fromkeys(token for token in tokens if not is_slot(token)))
    return PreviewSentence(
        id=row["id"],
        chinese=row["chinese"],
        pinyin=row["pinyin"],
        translation=row["translation"],
        rules=row["rules"],
        people=any(is_slot(token) for token in tokens) or bool(row["rules"]),
        duplicate=duplicate,
        needs_names=needs(row["chinese"], row["rules"], available),
        issues=_sentence_issues(
            row, tokens, {token: entry(token) for token in words_only}
        )
        if checked and not duplicate
        else (),
        words=_preview_tokens(row, tokens, entry),
        starts=_starts(words_only, entry),
    )


def _starts(words: tuple[str, ...], entry: Entry) -> str:
    statuses = [None if entry(word).new else entry(word).status for word in words]
    return sentence_state(
        "locked",
        sum(status != "learned" for status in statuses),
        sum(status not in {"learning", "learned"} for status in statuses),
        hidden=False,
    )


def preview(connection: sqlite3.Connection) -> Preview:
    library = library_words(connection)
    overrides = {
        row["word"]: row for row in connection.execute("SELECT * FROM pending_words")
    }
    proposed = {
        str(row["target"]): str(row["readings"])
        for row in connection.execute(
            "SELECT target, readings FROM pending_changes WHERE kind = 'word' "
            "AND remove = 0 AND readings IS NOT NULL"
        )
    }
    words: dict[str, PreviewWord] = {}

    def entry(word: str) -> PreviewWord:
        if word not in words:
            override = overrides.get(word)
            encoded, edited = _readings(override, word)
            known = _library_word(connection, word) if word in library else None
            if not edited and known is not None:
                encoded = proposed.get(word) or str(known["readings"])
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
                status=None if known is None else word_status(known),
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
        duplicate = (
            connection.execute(
                "SELECT 1 FROM sentences WHERE chinese = ?", (row["chinese"],)
            ).fetchone()
            is not None
        )
        if not duplicate:
            for token in dict.fromkeys(t for t in tokens if not is_slot(t)):
                entry(token).sentences.append(row["id"])
        sentences.append(
            _preview_sentence(row, tokens, entry, available, duplicate=duplicate)
        )
    changes = _preview_changes(connection, entry, available, sentences)
    for word, override in overrides.items():
        if override["requested"]:
            entry(word)
    listed = tuple(
        word
        for word in words.values()
        if word.new and (word.sentences or word.changes or word.requested)
    )
    problems = {
        "sentences": tuple(
            f"Sentence {sentence.id}: {issue}"
            for sentence in sentences
            for issue in sentence.issues
        ),
        "words": tuple(
            f"{word.word} has no definition."
            for word in listed
            if not word.readings and word.requested
        ),
        "names": tuple(issue for name in names for issue in name.issues),
        **{
            section: tuple(
                f"Change {change.id}: {issue}"
                for change in changes
                if _section(change) == section
                for issue in change.issues
            )
            for section in ("edits", "removals")
        },
    }
    issues = tuple(issue for section in problems.values() for issue in section)
    adds = {
        "sentences": sum(not sentence.duplicate for sentence in sentences),
        "words": len(listed),
        "names": sum(not name.duplicate for name in names),
        "changes": len(changes),
    }
    return Preview(
        ready=not issues and any(adds.values()),
        issues=issues,
        adds=adds,
        sentences=tuple(sentences),
        words=listed,
        names=names,
        version=version(connection),
        changes=changes,
        problems=problems,
    )


def _section(change: PreviewChange) -> str:
    return "removals" if change.action == "remove" else "edits"


def _library_sentence(
    connection: sqlite3.Connection, sentence_id: int
) -> sqlite3.Row | None:
    return cast(
        "sqlite3.Row | None",
        connection.execute(
            "SELECT * FROM sentences WHERE id = ?", (sentence_id,)
        ).fetchone(),
    )


def _library_word(connection: sqlite3.Connection, word: str) -> sqlite3.Row | None:
    return cast(
        "sqlite3.Row | None",
        connection.execute("SELECT * FROM words WHERE word = ?", (word,)).fetchone(),
    )


def _library_tokens(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> tuple[str, ...]:
    return with_slots(row["chinese"], words_of(connection, row["id"]))


def _sentence_change(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    entry: Entry,
    available: Counter[str],
) -> PreviewChange:
    action = "remove" if row["remove"] else "edit"
    library = _library_sentence(connection, int(row["target"]))
    if library is None:
        return PreviewChange(
            row["id"],
            "sentence",
            row["target"],
            action,
            (f"Sentence {row['target']} is no longer in your library.",),
        )
    current = _library_tokens(connection, library)
    before = _preview_sentence(library, current, entry, available, checked=False)
    reviews = review_count(connection, card_ids(connection, sentence_id=library["id"]))
    status = sentence_status(connection, library)
    hidden = bool(library["hidden"])
    if row["remove"]:
        return PreviewChange(
            row["id"],
            "sentence",
            row["target"],
            action,
            (),
            reviews,
            before,
            status=status,
            hidden=hidden,
        )
    tokens = current if row["words"] is None else tuple(json.loads(row["words"]))
    fields = {
        key: library[key] if row[key] is None else row[key]
        for key in ("pinyin", "translation", "rules")
    }
    for token in dict.fromkeys(token for token in tokens if not is_slot(token)):
        entry(token).changes.append(row["id"])
    after = _preview_sentence(
        {"id": library["id"], "chinese": library["chinese"], **fields},
        tokens,
        entry,
        available,
    )
    unchanged = tokens == current and all(fields[key] == library[key] for key in fields)
    return PreviewChange(
        row["id"],
        "sentence",
        row["target"],
        action,
        (f"This leaves sentence {library['id']} as it is.",)
        if unchanged
        else after.issues,
        reviews,
        before,
        after,
        status=status,
        hidden=hidden,
    )


def _word_change(connection: sqlite3.Connection, row: sqlite3.Row) -> PreviewChange:
    word = str(row["target"])
    action = "remove" if row["remove"] else "edit"
    library = _library_word(connection, word)
    if library is None:
        return PreviewChange(
            row["id"], "word", word, action, (f"{word} is no longer in your library.",)
        )
    reviews = review_count(connection, card_ids(connection, word=word))
    current = str(library["readings"])
    if row["remove"]:
        return PreviewChange(
            row["id"],
            "word",
            word,
            action,
            (),
            reviews,
            current=current,
            status=word_status(library),
        )
    proposed = row["readings"]
    readings = decode(proposed) if proposed is not None else ()
    issues: tuple[str, ...] = ()
    if not readings:
        issues = (f"Give {word} at least one meaning.",)
    elif proposed == current:
        issues = (f"This leaves {word} as it is.",)
    shown = readings or decode(current)
    return PreviewChange(
        row["id"],
        "word",
        word,
        action,
        issues,
        reviews,
        current=current,
        proposed=proposed,
        pinyin=shown[0].pinyin if shown else word_pinyin(word),
        meanings="\n".join(
            sense.text for reading in readings for sense in reading.senses
        ),
        status=word_status(library),
    )


def _plain(chinese: str) -> str:
    return SLOT.sub(lambda match: label(match.group())[0], chinese)


def _uses(
    connection: sqlite3.Connection,
    word: str,
    changes: list[PreviewChange],
    waiting: list[PreviewSentence],
) -> tuple[Use, ...]:
    touched = {
        int(change.target): change
        for change in changes
        if change.kind == "sentence" and change.before is not None
    }
    uses = []
    library = set()
    for row in connection.execute(
        "SELECT DISTINCT s.id, s.chinese, s.translation FROM sentences s "
        "JOIN sentence_words sw ON sw.sentence_id = s.id WHERE sw.word = ? "
        "ORDER BY s.id",
        (word,),
    ):
        library.add(row["id"])
        change = touched.get(row["id"])
        uses.append(
            Use(
                row["chinese"],
                row["translation"],
                change=None if change is None else change.id,
                action=None if change is None else change.action,
                keeps=change is None
                or (
                    change.after is not None
                    and any(token.text == word for token in change.after.words)
                ),
            )
        )
    uses += [
        Use(change.after.chinese, change.after.translation, change=change.id)
        for sentence_id, change in touched.items()
        if sentence_id not in library
        and change.after is not None
        and any(token.text == word for token in change.after.words)
    ]
    uses += [
        Use(sentence.chinese, sentence.translation, waiting=True)
        for sentence in waiting
        if not sentence.duplicate
        and any(token.text == word for token in sentence.words)
    ]
    return tuple(uses)


def _still_used(word: str, uses: tuple[Use, ...]) -> str:
    places = ", ".join(
        dict.fromkeys(
            f"the waiting sentence {_plain(use.chinese)}"
            if use.waiting
            else _plain(use.chinese)
            for use in uses
        )
    )
    if not places.endswith(tuple("。！？!?.…")):  # noqa: RUF001
        places += "."
    which = "that sentence" if len(uses) == 1 else "those sentences"
    return f"{word} is still in {places} Change or remove {which} first."


def _preview_changes(
    connection: sqlite3.Connection,
    entry: Entry,
    available: Counter[str],
    waiting: list[PreviewSentence],
) -> tuple[PreviewChange, ...]:
    changes = [
        _sentence_change(connection, row, entry, available)
        if row["kind"] == "sentence"
        else _word_change(connection, row)
        for row in connection.execute(
            "SELECT * FROM pending_changes ORDER BY id"
        ).fetchall()
    ]
    result = []
    for change in changes:
        if change.kind != "word" or change.action != "remove" or change.issues:
            result.append(change)
            continue
        uses = _uses(connection, change.target, changes, waiting)
        blocked = tuple(use for use in uses if not use.frees)
        result.append(
            replace(
                change,
                uses=uses,
                issues=(_still_used(change.target, blocked),) if blocked else (),
                waits_on=()
                if blocked
                else tuple(
                    dict.fromkeys(use.change for use in uses if use.change is not None)
                ),
            )
        )
    return tuple(result)


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
    if entry.sentences or entry.changes:
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


def _change(connection: sqlite3.Connection, kind: str, target: str) -> int:
    return int(
        connection.execute(
            "SELECT id FROM pending_changes WHERE kind = ? AND target = ?",
            (kind, target),
        ).fetchone()["id"]
    )


def _stage_change(
    connection: sqlite3.Connection, kind: str, target: str, **values: object
) -> int:
    columns = ("remove", "words", "pinyin", "translation", "rules", "readings")
    row = {column: values.get(column) for column in columns} | {
        "remove": int(bool(values.get("remove")))
    }
    connection.execute(
        f"INSERT INTO pending_changes(kind, target, {', '.join(columns)}) "  # noqa: S608
        f"VALUES (?, ?, {', '.join('?' for _ in columns)}) "
        "ON CONFLICT(kind, target) DO UPDATE SET "
        + ", ".join(f"{column} = excluded.{column}" for column in columns),
        (kind, target, *(row[column] for column in columns)),
    )
    return _change(connection, kind, target)


def _staged_change(
    connection: sqlite3.Connection, kind: str, target: str
) -> sqlite3.Row | None:
    return cast(
        "sqlite3.Row | None",
        connection.execute(
            "SELECT * FROM pending_changes WHERE kind = ? AND target = ? "
            "AND remove = 0",
            (kind, target),
        ).fetchone(),
    )


def library_words_error(
    connection: sqlite3.Connection, sentence_id: int, words: str
) -> str | None:
    library = _library_sentence(connection, sentence_id)
    if library is None:
        message = f"There is no sentence {sentence_id} in your library."
        raise LookupError(message)
    return segmentation_error(library["chinese"], parse_words(words))


def propose_sentence(
    connection: sqlite3.Connection,
    sentence_id: int,
    *,
    words: str | None = None,
    pinyin: str | None = None,
    translation: str | None = None,
    rules: str | None = None,
) -> int:
    library = _library_sentence(connection, sentence_id)
    if library is None:
        message = f"There is no sentence {sentence_id} in your library."
        raise LookupError(message)
    target = str(sentence_id)
    staged = _staged_change(connection, "sentence", target)
    current = _library_tokens(connection, library)

    def value(key: str) -> str:
        return str(
            library[key] if staged is None or staged[key] is None else staged[key]
        )

    tokens = list(
        current
        if staged is None or staged["words"] is None
        else json.loads(staged["words"])
    )
    new_pinyin = value("pinyin") if pinyin is None else normalize_text(pinyin)
    if words is not None:
        parsed = list(parse_words(words))
        if parsed != tokens and new_pinyin == value("pinyin"):
            new_pinyin = sentence_pinyin(library["chinese"], tuple(parsed))
        tokens = parsed
    fields = {
        "pinyin": new_pinyin,
        "translation": value("translation")
        if translation is None
        else normalize_text(translation),
        "rules": value("rules") if rules is None else clean_rules(rules),
    }
    return _stage_change(
        connection,
        "sentence",
        target,
        words=None
        if tuple(tokens) == current
        else json.dumps(tokens, ensure_ascii=False),
        **{
            key: None if field_value == library[key] else field_value
            for key, field_value in fields.items()
        },
    )


def propose_word(
    connection: sqlite3.Connection,
    word: str,
    *,
    meanings: str,
    pinyin: str | None = None,
) -> int:
    library = _library_word(connection, word)
    if library is None:
        message = f"{word} is not in your library."
        raise LookupError(message)
    current = decode(library["readings"])
    reading = pinyin if pinyin is not None else (current[0].pinyin if current else "")
    return _stage_change(
        connection,
        "word",
        word,
        readings=encode(manual(reading or word_pinyin(word), meanings))
        if meanings.strip()
        else None,
    )


def propose_removal(connection: sqlite3.Connection, kind: str, target: str) -> int:
    exists = (
        _library_sentence(connection, int(target))
        if kind == "sentence"
        else _library_word(connection, target)
    )
    if exists is None:
        message = (
            f"There is no sentence {target} in your library."
            if kind == "sentence"
            else f"{target} is not in your library."
        )
        raise LookupError(message)
    return _stage_change(connection, kind, target, remove=True)


def _pending_change(connection: sqlite3.Connection, change_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM pending_changes WHERE id = ?", (change_id,)
    ).fetchone()
    if row is None:
        message = f"There is no pending change {change_id}."
        raise LookupError(message)
    return cast("sqlite3.Row", row)


def edit_change(
    connection: sqlite3.Connection,
    change_id: int,
    *,
    words: str | None = None,
    pinyin: str | None = None,
    translation: str | None = None,
    rules: str | None = None,
    meanings: str | None = None,
) -> None:
    row = _pending_change(connection, change_id)
    if row["remove"]:
        return
    if row["kind"] == "sentence":
        propose_sentence(
            connection,
            int(row["target"]),
            words=words,
            pinyin=pinyin,
            translation=translation,
            rules=rules,
        )
    elif meanings is not None:
        propose_word(connection, row["target"], meanings=meanings, pinyin=pinyin)


def remove_change(connection: sqlite3.Connection, change_id: int) -> None:
    _pending_change(connection, change_id)
    connection.execute("DELETE FROM pending_changes WHERE id = ?", (change_id,))


def _clear(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM pending_sentences")
    connection.execute("DELETE FROM pending_words")
    connection.execute("DELETE FROM pending_names")
    connection.execute("DELETE FROM pending_changes")


def discard(
    connection: sqlite3.Connection, checked: str, section: str | None = None
) -> None:
    with editing(connection, checked):
        if section is None:
            _clear(connection)
            return
        words = preview(connection).words
        if section == "sentences":
            connection.execute("DELETE FROM pending_sentences")
            orphans = [
                word.word
                for word in words
                if word.sentences and not word.changes and not word.requested
            ]
        elif section == "words":
            connection.execute(
                "DELETE FROM pending_words WHERE requested = 1 AND readings IS NULL"
            )
            connection.execute("UPDATE pending_words SET requested = 0")
            orphans = [
                word.word
                for word in words
                if word.requested and not word.sentences and not word.changes
            ]
        elif section == "names":
            connection.execute("DELETE FROM pending_names")
            orphans = []
        else:
            connection.execute(
                "DELETE FROM pending_changes WHERE remove = ?",
                (int(section == "removals"),),
            )
            orphans = [
                word.word
                for word in words
                if section == "edits"
                and word.changes
                and not word.sentences
                and not word.requested
            ]
        connection.executemany(
            "DELETE FROM pending_words WHERE word = ?", [(word,) for word in orphans]
        )


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
    connection: sqlite3.Connection,
    checked: str,
    now: datetime,
    section: str | None = None,
) -> dict[str, int]:
    with editing(connection, checked):
        result = preview(connection)
        blocked = result.issues if section is None else result.problems[section]
        if blocked or not (
            result.ready if section is None else result.section(section)
        ):
            message = "Fix the problems listed above before approving."
            raise ConflictError(message)
        words = [word for word in result.words if result.wanted(word, section)]
        added = _add_words(connection, words, now)
        touched = set(added)
        added_sentences = 0
        for sentence in result.sentences if section in {None, "sentences"} else ():
            if sentence.duplicate:
                continue
            if tokens := _add_sentence(connection, sentence, now):
                added_sentences += 1
                touched.update(tokens)
        added_names = sum(
            _add_name(connection, name, now)
            for name in (result.names if section in {None, "names"} else ())
            if not name.duplicate
        )
        batch = [
            change
            for change in result.changes
            if section is None or _section(change) == section
        ]
        ids = {change.id for change in batch}
        changes = [change for change in batch if set(change.waits_on) <= ids]
        for change in sorted(
            changes,
            key=lambda change: (change.kind == "word", change.action == "remove"),
        ):
            _apply_change(connection, change)
        if section is None:
            _clear(connection)
        else:
            if section == "sentences":
                connection.execute("DELETE FROM pending_sentences")
            if section == "names":
                connection.execute("DELETE FROM pending_names")
            connection.executemany(
                "DELETE FROM pending_words WHERE word = ?", [(word,) for word in added]
            )
        refresh(connection, touched)
    return {
        "words": len(added),
        "sentences": added_sentences,
        "names": added_names,
        "changes": len(changes),
        "waiting": len(batch) - len(changes),
    }


def _apply_change(connection: sqlite3.Connection, change: PreviewChange) -> None:
    if change.kind == "sentence":
        if change.after is None:
            delete_sentence(connection, int(change.target))
        else:
            replace_sentence(
                connection,
                int(change.target),
                tuple(token.text for token in change.after.words if not token.slot),
                change.after.pinyin,
                change.after.translation,
                change.after.rules,
            )
    elif change.action == "remove":
        used = connection.execute(
            "SELECT sentence_id FROM sentence_words WHERE word = ? LIMIT 1",
            (change.target,),
        ).fetchone()
        if used is not None:
            message = (
                f"{change.target} is still in sentence {used['sentence_id']}. "
                "Approve the change to that sentence first."
            )
            raise ConflictError(message)
        delete_word(connection, change.target)
    elif change.proposed is not None:
        set_readings(connection, change.target, change.proposed)
    connection.execute("DELETE FROM pending_changes WHERE id = ?", (change.id,))


def approve_change(
    connection: sqlite3.Connection, checked: str, change_id: int, now: datetime
) -> dict[str, int]:
    with editing(connection, checked):
        result = preview(connection)
        change = next((item for item in result.changes if item.id == change_id), None)
        if change is None:
            message = f"There is no pending change {change_id}."
            raise ConflictError(message)
        if change.issues:
            message = "Fix the problems in this change before approving it."
            raise ConflictError(message)
        if change.waits_on:
            message = (
                f"Approve the changes that take {change.target} out of its "
                "sentences first."
            )
            raise ConflictError(message)
        tokens = (
            set()
            if change.after is None
            else {token.text for token in change.after.words if not token.slot}
        )
        added = _add_words(
            connection, (word for word in result.words if word.word in tokens), now
        )
        _apply_change(connection, change)
        connection.executemany(
            "DELETE FROM pending_words WHERE word = ?", [(word,) for word in added]
        )
    return {"words": len(added), "changes": 1}


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


def approve_word(
    connection: sqlite3.Connection, checked: str, word: str, now: datetime
) -> None:
    with editing(connection, checked):
        result = preview(connection)
        entry = next(
            (item for item in result.words if item.word == word and item.requested),
            None,
        )
        if entry is None:
            message = f"{word} is not a word waiting on its own."
            raise ConflictError(message)
        if not entry.readings:
            message = f"Give {word} a definition before approving it."
            raise ConflictError(message)
        _add_words(connection, [entry], now)
        connection.execute("DELETE FROM pending_words WHERE word = ?", (word,))
        refresh(connection, [word])


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
            "(SELECT count(*) FROM pending_names) + "
            "(SELECT count(*) FROM pending_changes)"
        ).fetchone()[0]
    )
