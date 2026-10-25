import logging
import re
from functools import lru_cache

import jieba
from pypinyin import Style, lazy_pinyin

from .dictionary import contains, join_syllables, syllables
from .normalization import normalize_text
from .text import HAN

jieba.setLogLevel(logging.WARNING)

HEADWORD_FREQ = 100_000
MAX_WORD = 6
RUN = re.compile(f"[{HAN}]+|[^{HAN}]+")
OPENING = "“‘（《「『(["
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


def segment(text: str, words: tuple[str, ...] = ()) -> tuple[str, ...]:
    text = normalize_text(text)
    if re.search(f"[{HAN}]\\s+[{HAN}]", text):
        return tuple(word for piece in text.split() for word in _runs(piece))
    library = frozenset(words)
    return tuple(
        word
        for piece in _tokenizer(tuple(sorted(library))).cut(text, HMM=False)
        for token in _runs(piece)
        for word in _split(token, library)
    )


def parse_words(text: str) -> tuple[str, ...]:
    return tuple(word for word in normalize_text(text).split() if word)


def segmentation_error(chinese: str, words: tuple[str, ...]) -> str | None:
    if not words:
        return "The sentence has no Chinese words."
    if any(han(word) != word for word in words):
        return "Words may contain only Chinese characters."
    if "".join(words) != han(chinese):
        return "The words must spell out the sentence's characters in order."
    return None


def word_pinyin(word: str) -> str:
    return join_syllables(lazy_pinyin(word, style=Style.TONE))


def sentence_pinyin(chinese: str, words: tuple[str, ...]) -> str:
    chinese = normalize_text(chinese)
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
        known = syllables(word) if len(word) > 1 else None
        parts = (
            list(known)
            if known is not None
            else characters[offset : offset + len(word)]
        )
        offset += len(word)
        closing, middle, following = _split_gap(gaps.get(offset, ""))
        pieces += [opening + join_syllables(parts) + closing, middle]
        opening = following
    pieces[-1] += opening
    return " ".join(piece for piece in pieces if piece)


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
