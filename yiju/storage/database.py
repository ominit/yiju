import json
import sqlite3
from collections.abc import Callable, Generator
from contextlib import closing, contextmanager
from pathlib import Path

type Migration = Callable[[sqlite3.Connection], None]

BASELINE_VERSION = 1
MIGRATIONS: tuple[Migration, ...] = ()
SCHEMA_VERSION = BASELINE_VERSION + len(MIGRATIONS)

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
    latest = BASELINE_VERSION + len(MIGRATIONS)
    with closing(connect(path)) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > latest:
            message = "This database was created by a newer version of Yiju."
            raise ValueError(message)
        connection.execute("PRAGMA journal_mode = WAL")
        if version == 0:
            connection.executescript(Path(__file__).with_name("schema.sql").read_text())
            with transaction(connection):
                connection.execute(f"PRAGMA user_version = {BASELINE_VERSION}")
            version = BASELINE_VERSION
        elif version < latest:
            backup = path.with_name(f"{path.stem}.v{version}{path.suffix}")
            if not backup.exists():
                with closing(sqlite3.connect(backup)) as destination:
                    connection.backup(destination)
        _migrate(connection, version)
        with transaction(connection):
            connection.executemany(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                [(key, json.dumps(value)) for key, value in DEFAULT_SETTINGS.items()],
            )


def _migrate(connection: sqlite3.Connection, version: int) -> None:
    connection.execute("PRAGMA foreign_keys = OFF")
    for target, migration in enumerate(
        MIGRATIONS[version - BASELINE_VERSION :], version + 1
    ):
        with transaction(connection):
            migration(connection)
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                message = f"Migrating to schema {target} broke a foreign key."
                raise ValueError(message)
            connection.execute(f"PRAGMA user_version = {target}")
    connection.execute("PRAGMA foreign_keys = ON")


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
