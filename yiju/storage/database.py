import json
import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path

from yiju.core.definitions import parse_definition
from yiju.core.scheduling import now_utc
from yiju.core.text import display_pinyin

from .cards import create_sentence_cards, create_word_cards

SCHEMA_VERSION = 8
SENTENCE_DEFAULT_VERSION = 2
DEFINITION_SCHEMA_VERSION = 3
REVIEW_WORDS_SCHEMA_VERSION = 4
CARD_DIRECTION_SCHEMA_VERSION = 5
PINYIN_FORMAT_VERSION = 7
LISTENING_SCHEMA_VERSION = 8

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


def _add_card_direction(connection: sqlite3.Connection) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(cards)")}
    if "direction" not in columns:
        connection.execute(
            "ALTER TABLE cards ADD COLUMN direction TEXT NOT NULL "
            "DEFAULT 'recognition' CHECK(direction IN ('recognition', 'production'))"
        )


def _add_listening_direction(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE cards_with_listening ("
        "id TEXT PRIMARY KEY, "
        "entry_id TEXT NOT NULL REFERENCES entries(id), "
        "sentence_id TEXT REFERENCES sentences(id), "
        "direction TEXT NOT NULL DEFAULT 'recognition' "
        "CHECK(direction IN ('recognition', 'production', 'listening')), "
        "word TEXT NOT NULL, scheduling TEXT NOT NULL, due TEXT NOT NULL, "
        "reviews INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0, "
        "suspended INTEGER NOT NULL DEFAULT 0)"
    )
    connection.execute(
        "INSERT INTO cards_with_listening "
        "(id, entry_id, sentence_id, direction, word, scheduling, due, reviews, "
        "version, suspended) SELECT id, entry_id, sentence_id, direction, word, "
        "scheduling, due, reviews, version, suspended FROM cards ORDER BY rowid"
    )
    connection.execute("DROP TABLE cards")
    connection.execute("ALTER TABLE cards_with_listening RENAME TO cards")
    connection.execute("CREATE INDEX cards_due ON cards(due)")
    connection.execute("CREATE INDEX cards_sentence ON cards(sentence_id)")


def _create_direction_cards(connection: sqlite3.Connection) -> None:
    now = now_utc()
    for row in connection.execute(
        "SELECT * FROM cards WHERE direction = 'recognition'"
    ).fetchall():
        if row["sentence_id"] is None:
            create_word_cards(connection, row["entry_id"], row["word"], now)
        else:
            create_sentence_cards(
                connection, row["sentence_id"], row["entry_id"], row["word"], now
            )


def _migrate_pinyin(connection: sqlite3.Connection, version: int) -> None:
    if version >= PINYIN_FORMAT_VERSION:
        return
    for row in connection.execute(
        "SELECT id, pinyin FROM entries WHERE instr(pinyin, '//') > 0"
    ).fetchall():
        connection.execute(
            "UPDATE entries SET pinyin = ? WHERE id = ?",
            (display_pinyin(row["pinyin"]), row["id"]),
        )


def _migrate_definitions(connection: sqlite3.Connection, version: int) -> None:
    if version >= DEFINITION_SCHEMA_VERSION:
        return
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(entries)")}
    if "definition_json" not in columns:
        connection.execute(
            "ALTER TABLE entries ADD COLUMN definition_json TEXT NOT NULL DEFAULT '{}'"
        )
    for row in connection.execute("SELECT id, definition FROM entries").fetchall():
        connection.execute(
            "UPDATE entries SET definition_json = ? WHERE id = ?",
            (parse_definition(row["definition"]).to_json(), row["id"]),
        )


def initialize(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(path)) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            message = "This database was created by a newer version of Yiju."
            raise ValueError(message)
        connection.execute("PRAGMA journal_mode = WAL")
        if version < LISTENING_SCHEMA_VERSION:
            connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(Path(__file__).with_name("schema.sql").read_text())
        with transaction(connection):
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version < CARD_DIRECTION_SCHEMA_VERSION:
                _add_card_direction(connection)
            if version < LISTENING_SCHEMA_VERSION:
                _add_listening_direction(connection)
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
                    create_sentence_cards(
                        connection, row["id"], row["entry_id"], row["word"], now
                    )
            _migrate_definitions(connection, version)
            if version < REVIEW_WORDS_SCHEMA_VERSION:
                connection.execute(
                    "INSERT OR IGNORE INTO review_words"
                    "(review_id, word, knowledge_before) "
                    "SELECT id, word, knowledge_before FROM reviews"
                )
            if version < LISTENING_SCHEMA_VERSION:
                _create_direction_cards(connection)
                if connection.execute("PRAGMA foreign_key_check").fetchone():
                    message = "Database migration found invalid card references."
                    raise sqlite3.IntegrityError(message)
            _migrate_pinyin(connection, version)
            connection.execute("PRAGMA user_version = 8")
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
