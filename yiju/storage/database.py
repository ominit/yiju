import json
import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path

DEFAULT_SETTINGS = {
    "max_unknown": 2,
    "include_zero": False,
    "new_per_day": 20,
    "retention": 0.9,
    "known_stability": 7,
    "known_days": 3,
    "known_retrievability": 0.8,
}


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(path)) as connection, connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            message = "This database was created by a newer version of Yiju."
            raise ValueError(message)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(Path(__file__).with_name("schema.sql").read_text())
        connection.executemany(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            [(key, json.dumps(value)) for key, value in DEFAULT_SETTINGS.items()],
        )


@contextmanager
def transaction(connection: sqlite3.Connection) -> Generator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def settings(connection: sqlite3.Connection) -> dict[str, int | float | bool]:
    return {
        row["key"]: json.loads(row["value"])
        for row in connection.execute("SELECT key, value FROM settings")
    }
