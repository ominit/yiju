from pathlib import Path

import pytest

from yiju.storage.database import SCHEMA_VERSION, connect, initialize
from yiju.storage.importing import import_xml


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


def test_initialize_rejects_unsupported_schema_versions(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    with connect(path) as db:
        db.execute("CREATE TABLE legacy (value TEXT)")
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 7}")
    with pytest.raises(ValueError, match="unsupported schema"):
        initialize(path)
    with connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 7
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'cards'"
        ).fetchone()
