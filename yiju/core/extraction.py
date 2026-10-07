import re
import unicodedata
from functools import lru_cache

from pypinyin.constants import PINYIN_DICT

from .models import ExampleSentence
from .text import HAN, clean_definition

MIN_CHARACTERS = 2
MAX_CHARACTERS = 60
MAX_PINYIN_WORD_LENGTH = 360

CHINESE = re.compile(f"[{HAN}][{HAN}，。！？、；：‘’“”（）《》〈〉…—· ,.!?;:()\\s]*")
WORD = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", re.UNICODE)
SEPARATORS = " \t\n,，.。!?！？;；:：—-()（）"
SECTION = re.compile(
    r"\s+(?:\d+\s+|(?:noun|verb|adverb|adjective|pronoun|measure word|"
    r"preposition|conjunction|particle|interjection|number)\b)",
)


def _plain(text: str) -> str:
    text = text.lower().replace("ü", "v")
    return "".join(
        char
        for char in unicodedata.normalize("NFD", text)
        if not unicodedata.combining(char)
    ).replace("u:", "v")


SYLLABLES = frozenset(
    _plain(syllable)
    for variants in PINYIN_DICT.values()
    for syllable in variants.split(",")
)


def _tone_count(text: str) -> int:
    return sum(
        char in "\u0304\u0301\u030c\u0300"
        for char in unicodedata.normalize("NFD", text)
    )


@lru_cache(maxsize=8192)
def _syllable_counts(word: str) -> frozenset[int]:
    if len(word) > MAX_PINYIN_WORD_LENGTH:
        return frozenset[int]()
    word = word.lower().replace("'", "").replace("’", "").replace("-", "")
    counts: list[set[int]] = [set() for _ in range(len(word) + 1)]
    counts[0].add(0)
    for end in range(1, len(word) + 1):
        for start in range(max(0, end - 6), end):
            if (
                _plain(word[start:end]) in SYLLABLES
                and _tone_count(word[start:end]) <= 1
            ):
                counts[end].update(count + 1 for count in counts[start])
    return frozenset(counts[-1])


def _split_reading(text: str, han_count: int) -> tuple[str, str] | None:
    position = 0
    count = 0
    for match in WORD.finditer(text):
        gap = text[position : match.start()]
        if gap.strip(SEPARATORS) or re.search(r"[.!?。！？]", gap):
            return None
        options = _syllable_counts(match.group())
        if not options:
            return None
        count += min(options)
        if count > han_count:
            return None
        position = match.end()
        if han_count == count:
            boundary = position
            while boundary < len(text) and text[boundary] in SEPARATORS:
                boundary += 1
            pinyin = text[:boundary].strip()
            translation = str(SECTION.split(text[boundary:], maxsplit=1)[0]).strip()
            has_tone = any(
                unicodedata.combining(c) for c in unicodedata.normalize("NFD", pinyin)
            )
            first_word = WORD.search(translation)
            starts_with_pinyin = (
                first_word is not None and _tone_count(first_word.group()) > 0
            )
            if (
                has_tone
                and not starts_with_pinyin
                and re.search(r"[A-Za-z]{2}", translation)
            ):
                return pinyin, translation
            return None
    return None


def extract_examples(definition: str) -> tuple[ExampleSentence, ...]:
    text = clean_definition(definition)
    matches = list(CHINESE.finditer(text))
    result: list[ExampleSentence] = []
    seen: set[str] = set()
    for index, match in enumerate(matches):
        chinese = match.group().strip()
        count = len(re.findall(f"[{HAN}]", chinese))
        if count < MIN_CHARACTERS or count > MAX_CHARACTERS:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        reading = _split_reading(text[match.end() : end], count)
        if reading is None or chinese in seen:
            continue
        pinyin, translation = reading
        seen.add(chinese)
        result.append(ExampleSentence(chinese, translation, pinyin))
    return tuple(result)
