import sqlite3
from pathlib import Path

import pytest

from yiju.storage import database
from yiju.storage.database import SCHEMA_VERSION, connect, initialize
from yiju.storage.importing import import_xml


def version(path: Path) -> int:
    with connect(path) as db:
        return int(db.execute("PRAGMA user_version").fetchone()[0])


def columns(path: Path, table: str) -> set[str]:
    with connect(path) as db:
        return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


def add_flag_color(connection: sqlite3.Connection) -> None:
    connection.execute(
        "ALTER TABLE word_flags ADD COLUMN color TEXT NOT NULL DEFAULT 'red'"
    )
    connection.execute("UPDATE word_flags SET color = 'blue' WHERE note != ''")


def drop_flag_note(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE word_flags DROP COLUMN note")


def test_initialize_is_repeatable_and_keeps_existing_data(
    tmp_path: Path, xml_export: bytes
) -> None:
    path = tmp_path / "yiju.sqlite3"
    initialize(path)
    with connect(path) as db:
        import_xml(db, xml_export, "words.xml")
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
    assert "color" in columns(path, "word_flags")
    assert "note" not in columns(path, "word_flags")
    assert not list(tmp_path.glob("yiju.v*.sqlite3"))


def test_existing_database_is_backed_up_then_migrated(
    tmp_path: Path, xml_export: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "yiju.sqlite3"
    initialize(path)
    with connect(path) as db:
        import_xml(db, xml_export, "words.xml")
        db.execute(
            "INSERT INTO word_flags VALUES ('我', 'check', '2026-01-01', '2026-01-01')"
        )
        db.execute(
            "INSERT INTO word_flags VALUES ('去', '', '2026-01-01', '2026-01-01')"
        )
        cards = db.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    monkeypatch.setattr(database, "MIGRATIONS", (add_flag_color,))
    initialize(path)
    assert version(path) == database.BASELINE_VERSION + 1
    with connect(path) as db:
        assert dict(db.execute("SELECT word, color FROM word_flags").fetchall()) == {
            "我": "blue",
            "去": "red",
        }
        assert db.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == cards
    backup = tmp_path / f"yiju.v{database.BASELINE_VERSION}.sqlite3"
    assert version(backup) == database.BASELINE_VERSION
    assert "color" not in columns(backup, "word_flags")
    with connect(backup) as db:
        assert db.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == cards


def test_failed_migration_keeps_the_last_completed_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "yiju.sqlite3"
    initialize(path)

    def broken(connection: sqlite3.Connection) -> None:
        connection.execute("ALTER TABLE word_flags ADD COLUMN broken TEXT")
        message = "Migration failed"
        raise RuntimeError(message)

    monkeypatch.setattr(database, "MIGRATIONS", (add_flag_color, broken))
    with pytest.raises(RuntimeError, match="Migration failed"):
        initialize(path)
    assert version(path) == database.BASELINE_VERSION + 1
    assert "color" in columns(path, "word_flags")
    assert "broken" not in columns(path, "word_flags")
    monkeypatch.setattr(database, "MIGRATIONS", (add_flag_color, drop_flag_note))
    initialize(path)
    assert version(path) == database.BASELINE_VERSION + 2
    assert "note" not in columns(path, "word_flags")


def test_migration_that_breaks_foreign_keys_is_rolled_back(
    tmp_path: Path, xml_export: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "yiju.sqlite3"
    initialize(path)
    with connect(path) as db:
        import_xml(db, xml_export, "words.xml")

    def orphan_cards(connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM entries")

    monkeypatch.setattr(database, "MIGRATIONS", (orphan_cards,))
    with pytest.raises(ValueError, match="foreign key"):
        initialize(path)
    assert version(path) == database.BASELINE_VERSION
    with connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 3
