import json
import sqlite3
from datetime import datetime

from yiju.core.selection import Analysis, analyze
from yiju.core.text import contains_han
from yiju.core.tokenizer import tokenize

from .cards import create_sentence_card, identity
from .database import settings, transaction, vocabulary_words
from .learning import ConflictError, known_words

MAX_CHINESE_LENGTH = 300
MAX_TRANSLATION_LENGTH = 2000
MAX_PINYIN_LENGTH = 1000


def sentence_analysis(
    connection: sqlite3.Connection, sentence: sqlite3.Row, now: datetime
) -> Analysis:
    config = settings(connection)
    return analyze(
        tuple(json.loads(sentence["tokens"])),
        known_words(connection, now),
        sentence["word"],
        int(config["max_unknown"]),
        include_zero=bool(config["include_zero"]),
        vocabulary=vocabulary_words(connection),
    )


def update_sentence(
    connection: sqlite3.Connection,
    sentence_id: str,
    version: int,
    chinese: str,
    pinyin: str,
    translation: str,
    status: str,
    now: datetime,
) -> None:
    if status not in {"accepted", "rejected"}:
        message = "Invalid sentence status."
        raise ValueError(message)
    if (
        not contains_han(chinese)
        or not translation.strip()
        or len(chinese) > MAX_CHINESE_LENGTH
        or len(translation) > MAX_TRANSLATION_LENGTH
        or len(pinyin) > MAX_PINYIN_LENGTH
    ):
        message = "Provide a Chinese sentence and translation within the field limits."
        raise ValueError(message)
    words = tuple(
        row[0] for row in connection.execute("SELECT DISTINCT word FROM entries")
    )
    tokens = tokenize(chinese, words)
    with transaction(connection):
        row = connection.execute(
            "SELECT s.*, e.word FROM sentences s JOIN entries e ON e.id = "
            "s.entry_id WHERE s.id = ?",
            (sentence_id,),
        ).fetchone()
        if row is None or row["version"] != version:
            message = "This sentence has changed. Reload it before saving."
            raise ConflictError(message)
        if row["word"] not in tokens:
            message = "The sentence must contain its target word as a separate token."
            raise ValueError(message)
        connection.execute(
            "UPDATE sentences SET chinese = ?, pinyin = ?, translation = ?, "
            "tokens = ?, status = ?, version = version + 1 WHERE id = ?",
            (
                chinese.strip(),
                pinyin.strip(),
                translation.strip(),
                json.dumps(tokens),
                status,
                sentence_id,
            ),
        )
        card_id = identity("sentence-card", sentence_id)
        if status == "accepted":
            create_sentence_card(
                connection, sentence_id, row["entry_id"], row["word"], now
            )
        connection.execute(
            "UPDATE cards SET suspended = ?, version = version + 1 WHERE id = ?",
            (int(status != "accepted"), card_id),
        )
