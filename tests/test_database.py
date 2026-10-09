from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Never

import pytest
from conftest import seed_known_word

from yiju.core.definitions import decode_definition
from yiju.storage import database
from yiju.storage.database import connect, initialize
from yiju.storage.importing import import_xml
from yiju.storage.learning import review_card, undo_review
from yiju.storage.queue import study_queue
from yiju.storage.sentences import update_sentence


def test_pinyin_migration_cleans_existing_entries_and_preserves_progress(
    tmp_path: Path, xml_export: bytes
) -> None:
    path = tmp_path / "pinyin.sqlite3"
    initialize(path)
    source = xml_export.replace("我".encode(), "回家".encode()).replace(
        b"wo3", b"hui2//jia1"
    )
    with connect(path) as db:
        import_xml(db, source, "words.xml")
        entry = db.execute("SELECT * FROM entries WHERE word = '回家'").fetchone()
        assert entry["pinyin"] == "huí jiā"
        db.execute(
            "UPDATE entries SET pinyin = 'huí //jiā' WHERE id = ?", (entry["id"],)
        )
        db.commit()
        now = datetime.now(UTC) + timedelta(seconds=1)
        card = db.execute(
            "SELECT * FROM cards WHERE word = '回家' AND direction = 'recognition' "
            "AND sentence_id IS NULL"
        ).fetchone()
        review_card(db, card["id"], card["version"], 3, now)
        db.execute(
            "INSERT INTO word_flags VALUES ('回家', '', ?, ?)",
            (now.isoformat(), now.isoformat()),
        )
        db.execute("PRAGMA user_version = 6")
        queries = (
            "SELECT * FROM cards ORDER BY rowid",
            "SELECT * FROM knowledge ORDER BY rowid",
            "SELECT * FROM reviews ORDER BY rowid",
            "SELECT * FROM word_flags ORDER BY rowid",
            "SELECT * FROM settings ORDER BY rowid",
        )
        before = {query: [tuple(row) for row in db.execute(query)] for query in queries}
        original_entry = dict(entry)
    for _ in range(2):
        initialize(path)
        with connect(path) as db:
            assert (
                dict(
                    db.execute(
                        "SELECT * FROM entries WHERE id = ?", (entry["id"],)
                    ).fetchone()
                )
                == original_entry
            )
            assert (
                db.execute("PRAGMA user_version").fetchone()[0]
                == database.SCHEMA_VERSION
            )
            for query in queries:
                assert [tuple(row) for row in db.execute(query)] == before[query]


@pytest.mark.parametrize("existing_pending_card", [False, True])
def test_migration_activates_pending_sentences_and_preserves_progress(
    tmp_path: Path, xml_export: bytes, *, existing_pending_card: bool
) -> None:
    path = tmp_path / "legacy.sqlite3"
    legacy_schema = (
        Path(database.__file__)
        .with_name("schema.sql")
        .read_text()
        .replace(
            "DEFAULT 'accepted' CHECK(status IN ('accepted', 'rejected'))",
            "DEFAULT 'pending' CHECK(status IN ('pending', 'accepted', 'rejected'))",
        )
    )
    initialize(path)
    with connect(path) as db:
        db.execute("DROP TABLE sentences")
        db.executescript(legacy_schema)
        import_xml(db, xml_export, "words.xml")
        now = datetime.now(UTC) + timedelta(seconds=1)
        seed_known_word(db, "我", now)
        card = next(row for row in study_queue(db, now) if row["sentence_id"])
        review_card(db, card["id"], card["version"], 3, now)
        sentence = db.execute(
            "SELECT * FROM sentences WHERE id = ?", (card["sentence_id"],)
        ).fetchone()
        update_sentence(
            db,
            sentence["id"],
            0,
            sentence["chinese"],
            sentence["pinyin"],
            sentence["translation"],
            "rejected",
            now,
        )
        db.execute(
            "UPDATE sentences SET status = 'pending' WHERE id != ?", (sentence["id"],)
        )
        if existing_pending_card:
            db.execute(
                "UPDATE cards SET suspended = 1 WHERE sentence_id != ?",
                (sentence["id"],),
            )
        else:
            db.execute("DELETE FROM cards WHERE sentence_id != ?", (sentence["id"],))
        db.execute("PRAGMA user_version = 1")
        queries = [
            "SELECT * FROM knowledge",
            "SELECT * FROM reviews",
            "SELECT * FROM settings",
        ]
        before = {query: [tuple(row) for row in db.execute(query)] for query in queries}
        skipped_card = dict(
            db.execute("SELECT * FROM cards WHERE id = ?", (card["id"],)).fetchone()
        )
    for _ in range(2):
        initialize(path)
        with connect(path) as db:
            assert (
                db.execute("PRAGMA user_version").fetchone()[0]
                == database.SCHEMA_VERSION
            )
            assert (
                db.execute(
                    "SELECT count(*) FROM sentences WHERE status = 'pending'"
                ).fetchone()[0]
                == 0
            )
            assert (
                db.execute(
                    "SELECT count(*) FROM cards WHERE sentence_id IS NOT NULL"
                ).fetchone()[0]
                == 6
            )
            assert (
                dict(
                    db.execute(
                        "SELECT * FROM cards WHERE id = ?", (card["id"],)
                    ).fetchone()
                )
                == skipped_card
            )
            assert (
                db.execute(
                    "SELECT status FROM sentences WHERE id = ?", (sentence["id"],)
                ).fetchone()[0]
                == "rejected"
            )
            assert (
                db.execute(
                    "SELECT suspended FROM cards WHERE sentence_id != ?",
                    (sentence["id"],),
                ).fetchone()[0]
                == 0
            )
            for query, rows in before.items():
                assert [tuple(row) for row in db.execute(query)] == rows
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_legacy_definitions_are_converted_once_without_changing_source(
    tmp_path: Path, xml_export: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "legacy.sqlite3"
    initialize(path)
    with connect(path) as db:
        import_xml(db, xml_export, "words.xml")
        source = db.execute("SELECT raw_definition FROM entries ORDER BY id").fetchall()
        source = [tuple(row) for row in source]
        db.execute("ALTER TABLE entries DROP COLUMN definition_json")
        db.execute("PRAGMA user_version = 2")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(initialize, [path] * 4))
    with connect(path) as db:
        for row in db.execute("SELECT definition_json FROM entries"):
            assert decode_definition(row[0]).senses
        assert [
            tuple(row)
            for row in db.execute("SELECT raw_definition FROM entries ORDER BY id")
        ] == source

    def unexpected_parsing(_text: str) -> Never:
        message = "Already converted entries must not be parsed again."
        raise AssertionError(message)

    monkeypatch.setattr(database, "parse_definition", unexpected_parsing)
    initialize(path)


def test_legacy_reviews_remain_undoable_after_multiword_upgrade(
    tmp_path: Path, xml_export: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "legacy-reviews.sqlite3"
    initialize(path)
    with connect(path) as db:
        import_xml(db, xml_export, "words.xml")
        now = datetime.now(UTC) + timedelta(seconds=1)
        card = next(row for row in study_queue(db, now) if row["sentence_id"] is None)
        review_card(db, card["id"], card["version"], 3, now)
        before = dict(
            db.execute(
                "SELECT * FROM knowledge WHERE word = ?", (card["word"],)
            ).fetchone()
        )
        later = now + timedelta(days=1)
        card = db.execute("SELECT * FROM cards WHERE id = ?", (card["id"],)).fetchone()
        review_card(db, card["id"], card["version"], 1, later)
        review = db.execute(
            "SELECT id FROM reviews ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        db.execute(
            "UPDATE reviews SET knowledge_before = (SELECT knowledge_before "
            "FROM review_words WHERE review_id = reviews.id)"
        )
        db.execute("DROP TABLE review_words")
        db.execute("PRAGMA user_version = 3")

    def unexpected_parsing(_text: str) -> Never:
        message = "Review migration must not reparse dictionary text."
        raise AssertionError(message)

    monkeypatch.setattr(database, "parse_definition", unexpected_parsing)
    initialize(path)
    with connect(path) as db:
        undo_review(db, review)
        restored = dict(
            db.execute(
                "SELECT * FROM knowledge WHERE word = ?", (card["word"],)
            ).fetchone()
        )
        assert restored == before
