import hashlib
import sqlite3
from datetime import datetime

from yiju.core.scheduling import new_card
from yiju.core.text import comparison_text


def identity(kind: str, text: str) -> str:
    return hashlib.sha256(f"{kind}:{comparison_text(text)}".encode()).hexdigest()


def create_sentence_card(
    connection: sqlite3.Connection,
    sentence_id: str,
    entry_id: str,
    word: str,
    now: datetime,
) -> None:
    card_id = identity("sentence-card", sentence_id)
    connection.execute(
        "INSERT OR IGNORE INTO cards(id, entry_id, sentence_id, word, "
        "scheduling, due) VALUES (?, ?, ?, ?, ?, ?)",
        (
            card_id,
            entry_id,
            sentence_id,
            word,
            new_card(card_id, now).to_json(),
            now.isoformat(),
        ),
    )
