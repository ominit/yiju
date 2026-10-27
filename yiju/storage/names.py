from collections import Counter
from random import SystemRandom
from typing import TYPE_CHECKING, cast

from yiju.core.names import (
    GENDERS,
    Name,
    letters,
    missing,
    name_error,
    parse_rules,
    pick,
)
from yiju.core.normalization import normalize_text

from .database import ConflictError, transaction

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime


def _name(row: sqlite3.Row) -> Name:
    return Name(row["id"], row["chinese"], row["pinyin"], row["english"], row["gender"])


def pool(connection: sqlite3.Connection) -> list[Name]:
    return [_name(row) for row in connection.execute("SELECT * FROM names ORDER BY id")]


def counts(connection: sqlite3.Connection) -> Counter[str]:
    return Counter(
        {
            row["gender"]: row["total"]
            for row in connection.execute(
                "SELECT gender, count(*) AS total FROM names GROUP BY gender"
            )
        }
    )


def needs(chinese: str, rules: str, available: Counter[str]) -> str | None:
    people = letters(chinese)
    if not people:
        return None
    try:
        parsed = parse_rules(rules)
    except ValueError:
        return None
    return missing(people, parsed, available)


def choose(chinese: str, rules: str, names: list[Name]) -> dict[str, Name] | None:
    people = letters(chinese)
    if not people:
        return {}
    try:
        parsed = parse_rules(rules)
    except ValueError:
        return None
    return pick(people, parsed, names, SystemRandom())


def chosen(
    connection: sqlite3.Connection, chinese: str, ids: str
) -> dict[str, Name] | None:
    people = letters(chinese)
    try:
        numbers = [int(value) for value in ids.split(",")] if ids else []
    except ValueError:
        return None
    if len(numbers) != len(people) or len(set(numbers)) != len(numbers):
        return None
    found = {
        row["id"]: _name(row)
        for row in connection.execute(
            "SELECT * FROM names WHERE id IN (SELECT value FROM json_each(?))",
            (str(numbers),),
        )
    }
    if len(found) != len(numbers):
        return None
    return {
        letter: found[number] for letter, number in zip(people, numbers, strict=True)
    }


def field_error(chinese: str, pinyin: str, english: str, gender: str) -> str | None:
    error = name_error(chinese)
    if error is not None:
        return error
    if gender not in GENDERS:
        return f"{chinese} needs a gender, male or female."
    if not pinyin.strip():
        return f"{chinese} needs its pinyin."
    if not english.strip():
        return f"{chinese} needs its English spelling."
    return None


def add_name(
    connection: sqlite3.Connection,
    chinese: str,
    pinyin: str,
    english: str,
    gender: str,
    now: datetime,
) -> bool:
    cursor = connection.execute(
        "INSERT OR IGNORE INTO names(chinese, pinyin, english, gender, created) "
        "VALUES (?, ?, ?, ?, ?)",
        (chinese, pinyin, english, gender, now.isoformat()),
    )
    return bool(cursor.rowcount)


def _current(connection: sqlite3.Connection, name_id: int, version: int) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM names WHERE id = ?", (name_id,)).fetchone()
    if row is None or row["version"] != version:
        message = "This name has changed. Reload it before saving."
        raise ConflictError(message)
    return cast("sqlite3.Row", row)


def update_name(
    connection: sqlite3.Connection,
    name_id: int,
    version: int,
    pinyin: str,
    english: str,
    gender: str,
) -> None:
    pinyin, english = normalize_text(pinyin), normalize_text(english)
    with transaction(connection):
        row = _current(connection, name_id, version)
        error = field_error(row["chinese"], pinyin, english, gender)
        if error is not None:
            raise ValueError(error)
        connection.execute(
            "UPDATE names SET pinyin = ?, english = ?, gender = ?, "
            "version = version + 1 WHERE id = ?",
            (pinyin, english, gender, name_id),
        )


def remove_name(connection: sqlite3.Connection, name_id: int, version: int) -> str:
    with transaction(connection):
        row = _current(connection, name_id, version)
        connection.execute("DELETE FROM names WHERE id = ?", (name_id,))
    return str(row["chinese"])
