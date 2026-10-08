import json
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from itertools import pairwise

from .extraction import extract_examples
from .models import ExampleSentence
from .text import HAN, clean_definition

PART_OF_SPEECH = re.compile(
    r"^(noun|verb|adverb|adjective|pronoun|measure word|preposition|conjunction|"
    r"particle|interjection|number|auxiliary|music)\b\s*",
    re.IGNORECASE,
)
SENSE_NUMBER = re.compile(r"(?:^|\s)(\d{1,2})\s+(?=[A-Za-z\[])")
SUBSENSE = re.compile(r"(?:^|\s)[a-z]\s+(?=\[)")
REFERENCE = re.compile(r"^(?:see|variant of|same as)\b", re.IGNORECASE)
FORMAT_VERSION = 1


@dataclass(frozen=True, slots=True)
class DefinitionSense:
    part_of_speech: str
    meaning: str
    examples: tuple[ExampleSentence, ...]


@dataclass(frozen=True, slots=True)
class Definition:
    senses: tuple[DefinitionSense, ...]

    @property
    def example_count(self) -> int:
        return sum(len(sense.examples) for sense in self.senses)

    def to_json(self) -> str:
        return json.dumps(
            {"version": FORMAT_VERSION, **asdict(self)}, ensure_ascii=False
        )


@lru_cache(maxsize=2048)
def decode_definition(value: str) -> Definition:
    data = json.loads(value)
    if data["version"] != FORMAT_VERSION:
        message = "Unsupported dictionary format."
        raise ValueError(message)
    return Definition(
        tuple(
            DefinitionSense(
                sense["part_of_speech"],
                sense["meaning"],
                tuple(ExampleSentence(**example) for example in sense["examples"]),
            )
            for sense in data["senses"]
        )
    )


@lru_cache(maxsize=2048)
def combine_definitions(values: str) -> Definition:
    return Definition(
        tuple(
            dict.fromkeys(
                sense
                for value in values.splitlines()
                for sense in decode_definition(value).senses
            )
        )
    )


@lru_cache(maxsize=2048)
def recall_prompt(values: str) -> str:
    meanings = (
        re.sub(
            r"\(\s*\)|\[\s*\]|\uff08\s*\uff09",
            "",
            re.sub(f"[{HAN}]+", "", sense.meaning),
        ).strip()
        for sense in combine_definitions(values).senses
        if sense.meaning and not REFERENCE.match(sense.meaning)
    )
    return "\n".join(dict.fromkeys(meaning for meaning in meanings if meaning))


def _sections(text: str) -> tuple[str, ...]:
    boundaries = []
    next_number = 1
    for match in SENSE_NUMBER.finditer(text):
        if next_number == 1 and text[: match.start()].strip():
            continue
        if int(match[1]) == next_number:
            boundaries.append((match.start(), match.end()))
            next_number += 1
    if not boundaries:
        return (text,)
    sections = []
    prefix = text[: boundaries[0][0]].strip()
    if prefix:
        sections.append(prefix)
    for index, (_, start) in enumerate(boundaries):
        end = boundaries[index + 1][0] if index + 1 < len(boundaries) else len(text)
        sections.append(text[start:end].strip())
    return tuple(sections)


def _subsections(text: str) -> tuple[str, ...]:
    boundaries = [match.start() for match in SUBSENSE.finditer(text)]
    if not boundaries:
        return (text,)
    starts = [0, *boundaries, len(text)]
    return tuple(
        text[start:end].strip()
        for start, end in pairwise(starts)
        if text[start:end].strip()
    )


def _meaning(text: str) -> str:
    if REFERENCE.match(text):
        return text.strip()
    prefix = text
    for chinese in re.finditer(f"[{HAN}]", text):
        lead = text[: chinese.start()]
        if any(
            lead.count(left) > lead.count(right)
            for left, right in [("(", ")"), ("[", "]"), ("\uff08", "\uff09")]
        ):
            continue
        prefix = lead
        break
    return prefix.strip().rstrip("([,; ")


@lru_cache(maxsize=2048)
def parse_definition(text: str) -> Definition:
    senses = []
    for raw_line in clean_definition(text).replace("\\n", "\n").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        part = PART_OF_SPEECH.match(line)
        label = part[1].lower() if part else ""
        content = line[part.end() :] if part else line
        for numbered in _sections(content):
            for section in _subsections(numbered):
                meaning = _meaning(section)
                examples = extract_examples(section)
                if meaning or examples:
                    senses.append(DefinitionSense(label, meaning, examples))
    return Definition(tuple(senses))
