import hashlib
import sqlite3
from datetime import datetime

from yiju.core.scheduling import new_card
from yiju.core.text import comparison_text


def identity(kind: str, text: str) -> str:
    return hashlib.sha256(f"{kind}:{comparison_text(text)}".encode()).hexdigest()


def _create_cards(
    connection: sqlite3.Connection,
    entry_id: str,
    word: str,
    now: datetime,
    sentence_id: str | None = None,
) -> None:
    kind = "sentence-card" if sentence_id is not None else "word"
    text = sentence_id if sentence_id is not None else word
    original = connection.execute(
        "SELECT suspended FROM cards WHERE id = ?", (identity(kind, text),)
    ).fetchone()
    suspended = original["suspended"] if original is not None else 0
    for direction, card_kind in [
        ("recognition", kind),
        ("production", kind + "-reverse"),
    ]:
        card_id = identity(card_kind, text)
        connection.execute(
            "INSERT OR IGNORE INTO cards(id, entry_id, sentence_id, direction, word, "
            "scheduling, due, suspended) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                card_id,
                entry_id,
                sentence_id,
                direction,
                word,
                new_card(card_id, now).to_json(),
                now.isoformat(),
                suspended,
            ),
        )


def create_word_cards(
    connection: sqlite3.Connection, entry_id: str, word: str, now: datetime
) -> None:
    _create_cards(connection, entry_id, word, now)


def create_sentence_cards(
    connection: sqlite3.Connection,
    sentence_id: str,
    entry_id: str,
    word: str,
    now: datetime,
) -> None:
    _create_cards(connection, entry_id, word, now, sentence_id)
