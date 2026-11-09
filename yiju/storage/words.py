from dataclasses import replace
from typing import TYPE_CHECKING, cast

from fsrs import Card

from yiju.core.dictionary import decode, encode, same_entries
from yiju.core.strength import Parameters, Strength, familiar, learned

from .cards import card_ids, create_cards, delete_cards
from .database import ConflictError, transaction
from .graph import refresh

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime


def status(row: sqlite3.Row) -> str:
    if row["state"] != "seen":
        return str(row["state"])
    if row["learned"]:
        return "learned"
    return "familiar" if row["familiar"] else "learning"


def strength(row: sqlite3.Row) -> Strength:
    return Strength(
        Card.from_json(row["memory"]) if row["memory"] else None,
        int(row["success_days"]),
        row["last_success"],
        bool(row["learned"]),
        bool(row["familiar"]),
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


def set_readings(connection: sqlite3.Connection, word: str, readings: str) -> None:
    current = connection.execute(
        "SELECT readings FROM words WHERE word = ?", (word,)
    ).fetchone()
    rewritten = current is None or not same_entries(
        decode(readings), decode(current["readings"])
    )
    connection.execute(
        "UPDATE words SET readings = ?, "
        "source = CASE WHEN ? THEN 'manual' ELSE source END, "
        "version = version + 1 WHERE word = ?",
        (readings, int(rewritten), word),
    )


def move_reading(
    connection: sqlite3.Connection,
    word: str,
    version: int,
    position: tuple[int, int | None],
    offset: int,
) -> None:
    reading, sense = position
    with transaction(connection):
        readings = list(decode(_current(connection, word, version)["readings"]))
        if not 0 <= reading < len(readings):
            message = "That pronunciation does not exist."
            raise ValueError(message)
        if sense is None:
            readings = _moved(readings, reading, offset)
        else:
            current = readings[reading]
            if not 0 <= sense < len(current.senses):
                message = "That meaning does not exist."
                raise ValueError(message)
            readings[reading] = replace(
                current, senses=tuple(_moved(list(current.senses), sense, offset))
            )
        set_readings(connection, word, encode(tuple(readings)))


def _moved[T](items: list[T], index: int, offset: int) -> list[T]:
    target = index + offset
    if offset not in {-1, 1} or not 0 <= target < len(items):
        message = "It cannot move that way."
        raise ValueError(message)
    items[index], items[target] = items[target], items[index]
    return items


def delete_word(connection: sqlite3.Connection, word: str) -> None:
    delete_cards(connection, card_ids(connection, word=word))
    connection.execute("DELETE FROM review_words WHERE word = ?", (word,))
    connection.execute(
        "DELETE FROM review_changes WHERE kind = 'word' AND key = ?", (word,)
    )
    connection.execute("DELETE FROM flags WHERE word = ?", (word,))
    connection.execute("DELETE FROM words WHERE word = ?", (word,))


def reclassify(connection: sqlite3.Connection, parameters: Parameters) -> None:
    changed = []
    for row in connection.execute("SELECT * FROM words WHERE memory IS NOT NULL"):
        current = strength(row)
        values = (
            learned(
                current.stability,
                current.success_days,
                parameters,
                was_learned=current.learned,
            ),
            familiar(
                current.stability,
                current.success_days,
                parameters,
                was_familiar=current.familiar,
            ),
        )
        if values != (current.learned, current.familiar):
            changed.append((*map(int, values), row["word"]))
    connection.executemany(
        "UPDATE words SET learned = ?, familiar = ?, version = version + 1 "
        "WHERE word = ?",
        changed,
    )
    refresh(connection)
