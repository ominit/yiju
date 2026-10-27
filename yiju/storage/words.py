from typing import TYPE_CHECKING, cast

from fsrs import Card

from yiju.core.strength import Parameters, Strength, learned

from .cards import create_cards
from .database import ConflictError, transaction
from .graph import refresh

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime


def status(row: sqlite3.Row) -> str:
    if row["state"] != "seen":
        return str(row["state"])
    return "learned" if row["learned"] else "learning"


def strength(row: sqlite3.Row) -> Strength:
    return Strength(
        Card.from_json(row["memory"]) if row["memory"] else None,
        int(row["success_days"]),
        row["last_success"],
        bool(row["learned"]),
    )


def add_word(
    connection: sqlite3.Connection,
    word: str,
    readings: str,
    source: str,
    now: datetime,
) -> bool:
    inserted = connection.execute(
        "INSERT OR IGNORE INTO words(word, readings, source, created) "
        "VALUES (?, ?, ?, ?)",
        (word, readings, source, now.isoformat()),
    ).rowcount
    if inserted:
        create_cards(connection, now, word=word)
    return bool(inserted)


def _current(connection: sqlite3.Connection, word: str, version: int) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM words WHERE word = ?", (word,)).fetchone()
    if row is None or row["version"] != version:
        message = "This word has changed. Reload the page."
        raise ConflictError(message)
    return cast("sqlite3.Row", row)


def set_manual(
    connection: sqlite3.Connection, word: str, version: int, *, manual: bool
) -> None:
    with transaction(connection):
        _current(connection, word, version)
        connection.execute(
            "UPDATE words SET manual = ?, version = version + 1 WHERE word = ?",
            (int(manual), word),
        )
        refresh(connection, [word])


def set_paused(
    connection: sqlite3.Connection, word: str, version: int, *, paused: bool
) -> None:
    with transaction(connection):
        _current(connection, word, version)
        connection.execute(
            "UPDATE words SET version = version + 1 WHERE word = ?", (word,)
        )
        connection.execute(
            "UPDATE cards SET suspended = ?, version = version + 1 WHERE word = ?",
            (int(paused), word),
        )


def reclassify(connection: sqlite3.Connection, parameters: Parameters) -> None:
    changed = []
    for row in connection.execute("SELECT * FROM words WHERE memory IS NOT NULL"):
        current = strength(row)
        value = learned(
            current.stability,
            current.success_days,
            parameters,
            was_learned=current.learned,
        )
        if value != current.learned:
            changed.append((int(value), row["word"]))
    connection.executemany(
        "UPDATE words SET learned = ?, version = version + 1 WHERE word = ?", changed
    )
    refresh(connection)
