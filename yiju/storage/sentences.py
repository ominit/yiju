import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from typing import cast

from fsrs import Card

from yiju.core.strength import Parameters
from yiju.core.text import contains_han

from .cards import create_cards
from .database import ConflictError, parameters, transaction
from .graph import refresh

MAX_CHINESE_LENGTH = 300
MAX_TRANSLATION_LENGTH = 2000
MAX_PINYIN_LENGTH = 1000


def field_error(chinese: str, pinyin: str, translation: str) -> str | None:
    if not translation.strip():
        return "Add a translation."
    if (
        len(chinese) > MAX_CHINESE_LENGTH
        or len(translation) > MAX_TRANSLATION_LENGTH
        or len(pinyin) > MAX_PINYIN_LENGTH
    ):
        return "Keep the sentence, pinyin and translation within the field limits."
    return None


def askable(translation: str) -> bool:
    return bool(translation.strip()) and not contains_han(translation)


def status(row: sqlite3.Row, cards: Iterable[sqlite3.Row], values: Parameters) -> str:
    if row["state"] != "unlocked":
        return str(row["state"])
    shown = [
        card
        for card in cards
        if card["card_type"] != "production" or askable(row["translation"])
    ]
    if not any(card["reviews"] for card in shown):
        return "unlocked"
    learned = all(
        card["reviews"]
        and (Card.from_json(card["scheduling"]).stability or 0.0)
        >= values.learned_stability
        for card in shown
    )
    return "learned" if learned else "learning"


def statuses(connection: sqlite3.Connection) -> dict[int, str]:
    cards: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for card in connection.execute(
        "SELECT * FROM cards WHERE sentence_id IS NOT NULL ORDER BY id"
    ):
        cards[card["sentence_id"]].append(card)
    values = parameters(connection)
    return {
        row["id"]: status(row, cards[row["id"]], values)
        for row in connection.execute("SELECT * FROM sentences")
    }


def words_of(connection: sqlite3.Connection, sentence_id: int) -> tuple[str, ...]:
    return tuple(
        str(row["word"])
        for row in connection.execute(
            "SELECT word FROM sentence_words WHERE sentence_id = ? ORDER BY position",
            (sentence_id,),
        )
    )


def add_sentence(
    connection: sqlite3.Connection,
    chinese: str,
    words: tuple[str, ...],
    pinyin: str,
    translation: str,
    now: datetime,
) -> int | None:
    cursor = connection.execute(
        "INSERT OR IGNORE INTO sentences(chinese, pinyin, translation, created) "
        "VALUES (?, ?, ?, ?)",
        (chinese, pinyin, translation, now.isoformat()),
    )
    if not cursor.rowcount or cursor.lastrowid is None:
        return None
    sentence_id = int(cursor.lastrowid)
    connection.executemany(
        "INSERT INTO sentence_words(sentence_id, position, word) VALUES (?, ?, ?)",
        [(sentence_id, position, word) for position, word in enumerate(words)],
    )
    create_cards(connection, now, sentence_id=sentence_id)
    return sentence_id


def _current(
    connection: sqlite3.Connection, sentence_id: int, version: int
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM sentences WHERE id = ?", (sentence_id,)
    ).fetchone()
    if row is None or row["version"] != version:
        message = "This sentence has changed. Reload it before saving."
        raise ConflictError(message)
    return cast("sqlite3.Row", row)


def update_sentence(
    connection: sqlite3.Connection,
    sentence_id: int,
    version: int,
    pinyin: str,
    translation: str,
) -> None:
    with transaction(connection):
        row = _current(connection, sentence_id, version)
        error = field_error(row["chinese"], pinyin, translation)
        if error is not None:
            raise ValueError(error)
        connection.execute(
            "UPDATE sentences SET pinyin = ?, translation = ?, version = version + 1 "
            "WHERE id = ?",
            (pinyin.strip(), translation.strip(), sentence_id),
        )


def set_hidden(
    connection: sqlite3.Connection, sentence_id: int, version: int, *, hidden: bool
) -> None:
    with transaction(connection):
        _current(connection, sentence_id, version)
        connection.execute(
            "UPDATE sentences SET hidden = ?, version = version + 1 WHERE id = ?",
            (int(hidden), sentence_id),
        )
        connection.execute(
            "UPDATE cards SET suspended = ?, version = version + 1 "
            "WHERE sentence_id = ?",
            (int(hidden), sentence_id),
        )
        refresh(connection, words_of(connection, sentence_id))
