import re
from functools import lru_cache

from pypinyin.constants import PINYIN_DICT
from pypinyin.contrib.tone_convert import to_normal

from .normalization import normalize_text
from .text import contains_han, display_pinyin


def _align(text: str, reading: str) -> tuple[tuple[str, str], ...] | None:
    words = re.findall(r"[^\W\d_]+", reading, re.UNICODE)
    letters = "".join(words)
    boundaries = set()
    end = 0
    for word in words:
        end += len(word)
        boundaries.add(end)
    plain = to_normal(letters.lower())
    paths: dict[int, tuple[tuple[str, str], ...]] = {0: ()}
    for character in text:
        if not contains_han(character):
            paths = {
                position + int(plain[position : position + 1] == character.lower()): (
                    *path,
                    (character, ""),
                )
                for position, path in paths.items()
            }
            continue
        readings = {
            to_normal(value).lower()
            for value in PINYIN_DICT.get(ord(character), "").split(",")
            if value
        }
        if character == "儿":
            readings.add("r")
        following: dict[int, tuple[tuple[str, str], ...]] = {}
        for position, path in paths.items():
            for syllable in readings:
                end = position + len(syllable)
                if plain[position:end] != syllable or any(
                    position < boundary < end for boundary in boundaries
                ):
                    continue
                following[end] = (*path, (character, letters[position:end]))
        paths = following
    return paths.get(len(letters))


@lru_cache(maxsize=8192)
def pronunciation(text: str, pinyin: str | None) -> tuple[tuple[str, str], ...]:
    reading = normalize_text(display_pinyin(pinyin))
    if not reading:
        return tuple((character, "") for character in text)
    aligned_reading = _align(text, reading)
    if aligned_reading is not None:
        return aligned_reading
    alternatives = [_align(text, value) for value in re.split(r"[,;/]", reading)]
    if any(value is None for value in alternatives):
        return ((text, reading),)
    aligned = [value for value in alternatives if value is not None]
    return tuple(
        (
            character,
            " / ".join(dict.fromkeys(value[index][1] for value in aligned)),
        )
        for index, character in enumerate(text)
    )
