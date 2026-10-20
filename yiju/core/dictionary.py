import asyncio
import gzip
import json
import os
import re
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from threading import Lock

from aiohttp import ClientError, ClientSession, ClientTimeout
from pypinyin.contrib.tone_convert import to_tone

from .normalization import normalize_text
from .text import HAN

URL = "https://www.mdbg.net/chinese/export/cedict/cedict_1_0_ts_utf-8_mdbg.txt.gz"
FILENAME = "cedict_ts.u8.gz"
MIN_ENTRIES = 100_000
LINE = re.compile(r"^(\S+) (\S+) \[([^\]]*)\] /(.*)/$")
REFERENCE = re.compile(rf"(?:([{HAN}·]+)\|)?([{HAN}·]+)?\[([^\]]+)\]")
CLASSIFIER = re.compile(r"^CL:(.+)$")
NOTE = re.compile(r"^\(([^()]*(?:\([^()]*\)[^()]*)*)\)\s*")
VARIANT = re.compile(
    r"^(?:old |archaic |unofficial |Japanese |erhua |ancient |Korean )?"
    r"variant of\b"
)
SEE = re.compile(r"^(?:see|see also|used in)\b")
VOWEL = re.compile(r"^[aeoāáǎàēéěèōóǒò]", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Sense:
    text: str
    notes: tuple[str, ...] = ()
    usage: bool = False


@dataclass(frozen=True, slots=True)
class Reading:
    pinyin: str
    senses: tuple[Sense, ...]
    classifiers: tuple[str, ...] = ()
    traditional: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Entry:
    traditional: str
    simplified: str
    pinyin: str
    definition: str


def syllable(value: str) -> str:
    value = value.replace("u:", "v").replace("U:", "V")
    if not re.fullmatch(r"[A-Za-z]+[1-5]", value):
        return value
    toned = to_tone(value.lower())
    return toned.capitalize() if value[0].isupper() else toned


def join_syllables(syllables: list[str]) -> str:
    text = ""
    for index, part in enumerate(syllables):
        if index and VOWEL.match(part) and text[-1:].isalpha():
            text += "'"
        text += part
    return text


def pinyin(numbered: str) -> str:
    return join_syllables([syllable(part) for part in numbered.split()])


def _references(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        word = match[2] or match[1]
        reading = pinyin(match[3])
        return f"{word} ({reading})" if word else reading

    return REFERENCE.sub(replace, text)


def _sense(text: str) -> Sense:
    text = _references(text).strip()
    notes = []
    while (note := NOTE.match(text)) is not None and note.end() < len(text):
        notes.append(note[1].strip())
        text = text[note.end() :].strip()
    if NOTE.fullmatch(text):
        return Sense(text[1:-1].strip(), tuple(notes), usage=True)
    return Sense(text, tuple(notes))


@cache
def _parse(definition: str) -> tuple[tuple[Sense, ...], tuple[str, ...]]:
    senses = []
    classifiers: list[str] = []
    for part in definition.split("/"):
        if not part.strip():
            continue
        if (match := CLASSIFIER.match(part.strip())) is not None:
            classifiers.extend(
                value.strip() for value in _references(match[1]).split(",")
            )
        else:
            senses.append(_sense(part))
    return tuple(senses), tuple(classifiers)


class DictionaryUnavailableError(OSError):
    pass


class Source:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = Lock()


SOURCE = Source(Path(os.environ.get("YIJU_DATA_DIR", "data")) / FILENAME)


def use(path: Path) -> None:
    SOURCE.path = path
    _reset()


def _reset() -> None:
    _index.cache_clear()
    published.cache_clear()


async def fetch(url: str = URL) -> bytes:
    async with (
        ClientSession(timeout=ClientTimeout(total=120, connect=15)) as session,
        session.get(url) as response,
    ):
        response.raise_for_status()
        return await response.read()


def _entries(data: bytes) -> int:
    return sum(
        1
        for line in gzip.decompress(data).decode("utf-8").splitlines()
        if LINE.match(line.rstrip("\r"))
    )


def _download() -> bytes:
    try:
        data = asyncio.run(fetch())
        entries = _entries(data)
    except (OSError, TimeoutError, ClientError, ValueError, EOFError) as error:
        message = f"Could not download CC-CEDICT: {error}"
        raise DictionaryUnavailableError(message) from error
    if entries < MIN_ENTRIES:
        message = "Could not download CC-CEDICT: the file does not look like it."
        raise DictionaryUnavailableError(message)
    return data


def _write(data: bytes) -> None:
    SOURCE.path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(suffix=".gz", dir=SOURCE.path.parent)
    staging = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        staging.replace(SOURCE.path)
    finally:
        staging.unlink(missing_ok=True)


def update(*, force: bool = True) -> str:
    with SOURCE.lock:
        if force or not SOURCE.path.is_file():
            _write(_download())
            _reset()
    return published()


def ensure() -> Path:
    if not SOURCE.path.is_file():
        update(force=False)
    return SOURCE.path


@cache
def _index() -> tuple[dict[str, list[Entry]], dict[str, list[Entry]]]:
    simplified: dict[str, list[Entry]] = defaultdict(list)
    traditional: dict[str, list[Entry]] = defaultdict(list)
    with gzip.open(ensure(), "rt", encoding="utf-8") as stream:
        for line in stream:
            if line.startswith("#"):
                continue
            match = LINE.match(line.rstrip("\r\n"))
            if match is None:
                continue
            entry = Entry(*match.groups())
            simplified[entry.simplified].append(entry)
            if entry.traditional != entry.simplified:
                traditional[entry.traditional].append(entry)
    return simplified, traditional


@cache
def published() -> str:
    if not SOURCE.path.is_file():
        return ""
    with gzip.open(SOURCE.path, "rt", encoding="utf-8") as stream:
        for line in stream:
            if not line.startswith("#"):
                break
            if line.startswith("#! date="):
                return line.removeprefix("#! date=")[:10]
    return ""


def _variant_only(entry: Entry) -> bool:
    senses = _parse(entry.definition)[0]
    return bool(senses) and all(VARIANT.match(sense.text) for sense in senses)


def _proper(entry: Entry) -> bool:
    return entry.pinyin[:1].isupper()


def _self_reference(sense: Sense, word: str) -> bool:
    return (
        VARIANT.match(sense.text) is not None
        and re.search(rf"\bof {re.escape(word)} \(", sense.text) is not None
    )


def lookup(word: str) -> tuple[Reading, ...]:
    word = normalize_text(word)
    simplified, traditional = _index()
    entries = simplified.get(word) or traditional.get(word) or []
    for unwanted in (_variant_only, _proper):
        kept = [entry for entry in entries if not unwanted(entry)]
        entries = kept or entries
    merged: dict[str, list[Entry]] = {}
    for entry in entries:
        merged.setdefault(entry.pinyin, []).append(entry)
    readings = []
    for numbered, group in merged.items():
        parsed = [_parse(entry.definition) for entry in group]
        senses = tuple(dict.fromkeys(sense for senses, _ in parsed for sense in senses))
        readings.append(
            Reading(
                pinyin(numbered),
                tuple(sense for sense in senses if not _self_reference(sense, word))
                or senses,
                tuple(dict.fromkeys(value for _, values in parsed for value in values)),
                tuple(
                    dict.fromkeys(
                        entry.traditional
                        for entry in group
                        if entry.traditional != word
                    )
                ),
            )
        )
    return tuple(
        sorted(
            readings,
            key=lambda reading: all(is_reference(sense) for sense in reading.senses),
        )
    )


def contains(word: str) -> bool:
    simplified, traditional = _index()
    return word in simplified or word in traditional


def syllables(word: str) -> tuple[str, ...] | None:
    simplified, traditional = _index()
    entries = simplified.get(word) or traditional.get(word) or []
    readings = {
        entry.pinyin
        for entry in entries
        if not _variant_only(entry) and len(entry.pinyin.split()) == len(word)
    }
    readings = {value for value in readings if not value[:1].isupper()} or readings
    if len(readings) != 1:
        return None
    return tuple(syllable(part) for part in readings.pop().split())


def encode(readings: tuple[Reading, ...]) -> str:
    return json.dumps(
        [
            {
                "pinyin": reading.pinyin,
                "senses": [
                    {
                        "text": sense.text,
                        "notes": list(sense.notes),
                        "usage": sense.usage,
                    }
                    for sense in reading.senses
                ],
                "classifiers": list(reading.classifiers),
                "traditional": list(reading.traditional),
            }
            for reading in readings
        ],
        ensure_ascii=False,
    )


@cache
def decode(value: str) -> tuple[Reading, ...]:
    return tuple(
        Reading(
            item["pinyin"],
            tuple(
                Sense(sense["text"], tuple(sense["notes"]), bool(sense["usage"]))
                for sense in item["senses"]
            ),
            tuple(item["classifiers"]),
            tuple(item["traditional"]),
        )
        for item in json.loads(value)
    )


def manual(reading: str, meanings: str) -> tuple[Reading, ...]:
    senses = tuple(
        Sense(line.strip()) for line in meanings.splitlines() if line.strip()
    )
    return (Reading(normalize_text(reading), senses),) if senses else ()


def readings_pinyin(readings: tuple[Reading, ...]) -> str:
    return ", ".join(dict.fromkeys(reading.pinyin for reading in readings))


def is_reference(sense: Sense) -> bool:
    return SEE.match(sense.text) is not None or VARIANT.match(sense.text) is not None
