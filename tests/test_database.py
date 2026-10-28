from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from conftest import SAMPLE, add

from yiju.storage import database
from yiju.storage.database import MIGRATIONS, SCHEMA_VERSION, connect, initialize

if TYPE_CHECKING:
    import sqlite3


REAL = MIGRATIONS


def version(path: Path) -> int:
    with connect(path) as db:
        return int(db.execute("PRAGMA user_version").fetchone()[0])


def columns(path: Path, table: str) -> set[str]:
    with connect(path) as db:
        return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


def add_flag_color(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE flags ADD COLUMN color TEXT NOT NULL DEFAULT 'red'")
    connection.execute("UPDATE flags SET color = 'blue' WHERE note != ''")


def drop_flag_note(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE flags DROP COLUMN note")


def test_initialize_is_repeatable_and_keeps_existing_data(
    tmp_path: Path,
) -> None:
    path = tmp_path / "yiju.sqlite3"
    initialize(path)
    with connect(path) as db:
        add(db, SAMPLE)
        db.execute("UPDATE settings SET value = '5' WHERE key = 'new_per_day'")
        cards = [tuple(row) for row in db.execute("SELECT * FROM cards ORDER BY id")]
    initialize(path)
    with connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert [
            tuple(row) for row in db.execute("SELECT * FROM cards ORDER BY id")
        ] == cards
        assert (
            db.execute(
                "SELECT value FROM settings WHERE key = 'new_per_day'"
            ).fetchone()[0]
            == "5"
        )
    assert not list(tmp_path.glob("yiju.v*.sqlite3"))


def test_initialize_rejects_newer_schema_versions(tmp_path: Path) -> None:
    path = tmp_path / "newer.sqlite3"
    with connect(path) as db:
        db.execute("CREATE TABLE legacy (value TEXT)")
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 7}")
    with pytest.raises(ValueError, match="newer version"):
        initialize(path)
    assert version(path) == SCHEMA_VERSION + 7
    with connect(path) as db:
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'cards'"
        ).fetchone()


def test_new_database_applies_every_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(database, "MIGRATIONS", (add_flag_color, drop_flag_note))
    path = tmp_path / "yiju.sqlite3"
    initialize(path)
    assert version(path) == database.BASELINE_VERSION + 2
    assert "color" in columns(path, "flags")
    assert "note" not in columns(path, "flags")
    assert not list(tmp_path.glob("yiju.v*.sqlite3"))


def test_existing_database_is_backed_up_then_migrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "yiju.sqlite3"
    initialize(path)
    with connect(path) as db:
        add(db, SAMPLE)
        db.execute(
            "INSERT INTO flags VALUES ('我', 'check', '2026-01-01', '2026-01-01')"
        )
        db.execute("INSERT INTO flags VALUES ('去', '', '2026-01-01', '2026-01-01')")
        cards = db.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    monkeypatch.setattr(database, "MIGRATIONS", (*REAL, add_flag_color))
    initialize(path)
    assert version(path) == SCHEMA_VERSION + 1
    with connect(path) as db:
        assert dict(db.execute("SELECT word, color FROM flags").fetchall()) == {
            "我": "blue",
            "去": "red",
        }
        assert db.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == cards
    backup = tmp_path / f"yiju.v{SCHEMA_VERSION}.sqlite3"
    assert version(backup) == SCHEMA_VERSION
    assert "color" not in columns(backup, "flags")
    with connect(backup) as db:
        assert db.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == cards


def test_failed_migration_keeps_the_last_completed_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "yiju.sqlite3"
    initialize(path)

    def broken(connection: sqlite3.Connection) -> None:
        connection.execute("ALTER TABLE flags ADD COLUMN broken TEXT")
        message = "Migration failed"
        raise RuntimeError(message)

    monkeypatch.setattr(database, "MIGRATIONS", (*REAL, add_flag_color, broken))
    with pytest.raises(RuntimeError, match="Migration failed"):
        initialize(path)
    assert version(path) == SCHEMA_VERSION + 1
    assert "color" in columns(path, "flags")
    assert "broken" not in columns(path, "flags")
    monkeypatch.setattr(database, "MIGRATIONS", (*REAL, add_flag_color, drop_flag_note))
    initialize(path)
    assert version(path) == SCHEMA_VERSION + 2
    assert "note" not in columns(path, "flags")


def test_migration_that_breaks_foreign_keys_is_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "yiju.sqlite3"
    initialize(path)
    with connect(path) as db:
        add(db, SAMPLE)

    def orphan_cards(connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM words")

    monkeypatch.setattr(database, "MIGRATIONS", (*REAL, orphan_cards))
    with pytest.raises(ValueError, match="foreign key"):
        initialize(path)
    assert version(path) == SCHEMA_VERSION
    with connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM words").fetchone()[0] == 5


def test_a_baseline_database_gains_the_pending_changes_table(tmp_path: Path) -> None:
    path = tmp_path / "yiju.sqlite3"
    with connect(path) as db:
        db.executescript((Path(database.__file__).with_name("schema.sql")).read_text())
        db.execute(f"PRAGMA user_version = {database.BASELINE_VERSION}")
    assert not columns(path, "pending_changes")
    initialize(path)
    assert version(path) == SCHEMA_VERSION
    assert {"kind", "target", "remove", "words", "readings"} <= columns(
        path, "pending_changes"
    )
    assert (tmp_path / f"yiju.v{database.BASELINE_VERSION}.sqlite3").exists()
