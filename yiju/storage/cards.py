import json
from typing import TYPE_CHECKING

from fsrs import Card

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

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


def card_ids(
    connection: sqlite3.Connection,
    *,
    word: str | None = None,
    sentence_id: int | None = None,
) -> list[int]:
    return [
        int(row["id"])
        for row in connection.execute(
            "SELECT id FROM cards WHERE word IS ? AND sentence_id IS ?",
            (word, sentence_id),
        )
    ]


def review_count(connection: sqlite3.Connection, cards: list[int]) -> int:
    return int(
        connection.execute(
            "SELECT count(*) FROM reviews WHERE undone = 0 "
            "AND card_id IN (SELECT value FROM json_each(?))",
            (json.dumps(cards),),
        ).fetchone()[0]
    )


def delete_cards(connection: sqlite3.Connection, cards: list[int]) -> None:
    ids = json.dumps(cards)
    reviews = "SELECT id FROM reviews WHERE card_id IN (SELECT value FROM json_each(?))"
    for table in ("review_changes", "review_words"):
        connection.execute(
            f"DELETE FROM {table} WHERE review_id IN ({reviews})",  # noqa: S608
            (ids,),
        )
    connection.execute(
        "DELETE FROM reviews WHERE card_id IN (SELECT value FROM json_each(?))", (ids,)
    )
    connection.execute(
        "DELETE FROM cards WHERE id IN (SELECT value FROM json_each(?))", (ids,)
    )
