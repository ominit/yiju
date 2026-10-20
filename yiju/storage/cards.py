import sqlite3
from datetime import datetime

from fsrs import Card

CARD_TYPES = ("recognition", "production")


def create_cards(
    connection: sqlite3.Connection,
    now: datetime,
    *,
    word: str | None = None,
    sentence_id: int | None = None,
) -> None:
    connection.executemany(
        "INSERT OR IGNORE INTO cards(word, sentence_id, card_type, scheduling, due) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (
                word,
                sentence_id,
                card_type,
                Card(card_id=0, due=now).to_json(),
                now.isoformat(),
            )
            for card_type in CARD_TYPES
        ],
    )
