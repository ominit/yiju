import logging
from functools import lru_cache

import jieba

from .normalization import normalize_text

TOKENIZER_VERSION = "jieba-0.42.1-hmm-off-pinned"
jieba.setLogLevel(logging.WARNING)

HEADWORD_FREQ = 100_000


@lru_cache(maxsize=4)
def _tokenizer(words: tuple[str, ...]) -> jieba.Tokenizer:
    tokenizer = jieba.Tokenizer()
    tokenizer.initialize()
    for word in words:
        tokenizer.add_word(word, freq=HEADWORD_FREQ)
    return tokenizer


def tokenize(text: str, words: tuple[str, ...] = ()) -> tuple[str, ...]:
    tokenizer = _tokenizer(tuple(sorted(set(words))))
    return tuple(
        normalized
        for token in tokenizer.cut(text, HMM=False)
        if (normalized := normalize_text(token))
        and any(c.isalnum() for c in normalized)
    )
