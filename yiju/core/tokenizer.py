import logging
import re
from functools import lru_cache

import jieba
from pypinyin import Style, lazy_pinyin

from .dictionary import contains, join_syllables, syllables
from .names import SLOT, is_slot, strip_slots
from .normalization import normalize_text
from .text import HAN

jieba.setLogLevel(logging.WARNING)

HEADWORD_FREQ = 100_000
MAX_WORD = 6
RUN = re.compile(f"[{HAN}]+|[^{HAN}]+")
SLOTS = re.compile(r"(\{[^{}]*\})")
OPENING = "“‘（《「『(["
CLOSING = re.compile(f"[^\\w\\s{OPENING}]")
PUNCTUATION = str.maketrans(
    {
        "，": ",",
        "。": ".",
        "！": "!",
        "？": "?",
        "；": ";",
        "：": ":",
        "、": ",",
        "（": "(",
        "）": ")",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "《": '"',
        "》": '"',
        "…": "...",
        "—": "-",
        "～": "~",
        "「": '"',
        "」": '"',
        "『": '"',
        "』": '"',
    }
)


@lru_cache(maxsize=4)
def _tokenizer(words: tuple[str, ...]) -> jieba.Tokenizer:
    tokenizer = jieba.Tokenizer()
    tokenizer.initialize()
    for word in words:
        tokenizer.add_word(word, freq=HEADWORD_FREQ)
    return tokenizer


def han(text: str) -> str:
    return re.sub(f"[^{HAN}]", "", text)


def _runs(text: str) -> list[str]:
    return [match.group() for match in re.finditer(f"[{HAN}]+", text)]


def _split(token: str, words: frozenset[str]) -> list[str]:
    if token in words or contains(token):
        return [token]
    pieces = []
    start = 0
    while start < len(token):
        end = next(
            (
                end
                for end in range(min(len(token), start + MAX_WORD), start + 1, -1)
                if token[start:end] in words or contains(token[start:end])
            ),
            start + 1,
        )
        pieces.append(token[start:end])
        start = end
    return pieces


def _slotted(text: str) -> list[str]:
    return [piece for piece in SLOTS.split(text) if piece]


def segment(text: str, words: tuple[str, ...] = ()) -> tuple[str, ...]:
    text = normalize_text(text)
    if re.search(f"[{HAN}]\\s+[{HAN}]", strip_slots(text)):
        return tuple(
            word
            for piece in text.split()
            for part in _slotted(piece)
            for word in ([part] if is_slot(part) else _runs(part))
        )
    library = frozenset(words)
    cut = _tokenizer(tuple(sorted(library))).cut
    return tuple(
        word
        for part in _slotted(text)
        for word in (
            [part]
            if is_slot(part)
            else (
                word
                for piece in cut(part, HMM=False)
                for token in _runs(piece)
                for word in _split(token, library)
            )
        )
    )


def parse_words(text: str) -> tuple[str, ...]:
    return tuple(
        part for word in normalize_text(text).split() for part in _slotted(word)
    )


def _spelled(text: str) -> list[str]:
    return [
        part
        for piece in _slotted(text)
        for part in ([piece] if is_slot(piece) else list(han(piece)))
    ]


def segmentation_error(chinese: str, words: tuple[str, ...]) -> str | None:
    if not any(not is_slot(word) for word in words):
        return "The sentence has no Chinese words."
    if any(han(word) != word for word in words if not is_slot(word)):
        return "Words may contain only Chinese characters."
    if [part for word in words for part in _spelled(word)] != _spelled(chinese):
        return "The words must spell out the sentence's characters in order."
    return None


def word_pinyin(word: str) -> str:
    return join_syllables(lazy_pinyin(word, style=Style.TONE))


def sentence_pinyin(chinese: str, words: tuple[str, ...]) -> str:
    chinese = normalize_text(chinese)
    if not SLOT.search(chinese):
        return _pinyin(chinese, words)
    groups: list[list[str]] = [[]]
    for word in words:
        if is_slot(word):
            groups.append([])
        else:
            groups[-1].append(word)
    pieces = SLOTS.split(chinese)
    text = ""
    for index, piece in enumerate(pieces):
        if index % 2:
            part = "tā" if "." in piece else str(piece)
        else:
            group = groups[index // 2] if index // 2 < len(groups) else []
            part = _pinyin(piece, tuple(group)) if piece else ""
        glued = pieces[index - 1][-1:] in set(OPENING) or (
            not index % 2 and CLOSING.match(piece) is not None
        )
        if text and part and not glued:
            text += " "
        text += part
    return text


def _pinyin(chinese: str, words: tuple[str, ...]) -> str:
    readings = lazy_pinyin(chinese, style=Style.TONE)
    characters: list[str] = []
    gaps: dict[int, str] = {}
    position = 0
    for run in RUN.findall(chinese):
        if re.match(f"[{HAN}]", run):
            characters.extend(readings[position : position + len(run)])
            position += len(run)
        else:
            gaps[len(characters)] = gaps.get(len(characters), "") + run
            position += 1
    closing, middle, opening = _split_gap(gaps.get(0, ""))
    pieces = [f"{closing} {middle}".strip()]
    offset = 0
    for word in words:
        parts = _dictionary_reading(word, characters[offset : offset + len(word)])
        offset += len(word)
        closing, middle, following = _split_gap(gaps.get(offset, ""))
        pieces += [opening + join_syllables(parts) + closing, middle]
        opening = following
    pieces[-1] += opening
    return " ".join(piece for piece in pieces if piece)


def _dictionary_reading(word: str, generated: list[str]) -> list[str]:
    readings = syllables(word)
    wanted = [part.lower() for part in generated]
    for reading in readings:
        if [part.lower() for part in reading] == wanted:
            return list(reading)
    return list(readings[0]) if readings else generated


def _split_gap(gap: str) -> tuple[str, str, str]:
    text = gap.strip()
    end = len(text)
    while end and text[end - 1] in OPENING:
        end -= 1
    body = text[:end]
    closing = re.match(r"^[^\w]*", body)
    lead = closing.group() if closing is not None else ""
    return _clean(lead), _clean(body[len(lead) :]), _clean(text[end:])


def _clean(text: str) -> str:
    return text.translate(PUNCTUATION).strip()
