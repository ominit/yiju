import json
import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path

from yiju.core.definitions import parse_definition
from yiju.core.scheduling import now_utc

from .cards import create_sentence_card

SCHEMA_VERSION = 4
SENTENCE_DEFAULT_VERSION = 2
DEFINITION_SCHEMA_VERSION = 3

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
    with closing(connect(path)) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            message = "This database was created by a newer version of Yiju."
            raise ValueError(message)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(Path(__file__).with_name("schema.sql").read_text())
        with transaction(connection):
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version < SENTENCE_DEFAULT_VERSION:
                connection.execute(
                    "UPDATE cards SET suspended = 0, version = version + 1 "
                    "WHERE sentence_id IN "
                    "(SELECT id FROM sentences WHERE status = 'pending')"
                )
                connection.execute(
                    "UPDATE sentences SET status = 'accepted', version = version + 1 "
                    "WHERE status = 'pending'"
                )
                now = now_utc()
                for row in connection.execute(
                    "SELECT s.id, s.entry_id, e.word FROM sentences s JOIN entries e "
                    "ON e.id = s.entry_id WHERE s.status = 'accepted'"
                ).fetchall():
                    create_sentence_card(
                        connection, row["id"], row["entry_id"], row["word"], now
                    )
            if version < DEFINITION_SCHEMA_VERSION:
                columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(entries)")
                }
                if "definition_json" not in columns:
                    connection.execute(
                        "ALTER TABLE entries ADD COLUMN definition_json "
                        "TEXT NOT NULL DEFAULT '{}'"
                    )
                for row in connection.execute(
                    "SELECT id, definition FROM entries"
                ).fetchall():
                    connection.execute(
                        "UPDATE entries SET definition_json = ? WHERE id = ?",
                        (parse_definition(row["definition"]).to_json(), row["id"]),
                    )
            if version < SCHEMA_VERSION:
                connection.execute(
                    "INSERT OR IGNORE INTO review_words"
                    "(review_id, word, knowledge_before) "
                    "SELECT id, word, knowledge_before FROM reviews"
                )
                connection.execute("PRAGMA user_version = 4")
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


def vocabulary_words(connection: sqlite3.Connection) -> frozenset[str]:
    return frozenset(
        str(row[0]) for row in connection.execute("SELECT DISTINCT word FROM entries")
    )
