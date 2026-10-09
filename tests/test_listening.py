import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Never

import pytest
from conftest import seed_known_word
from fsrs import Card

from yiju.storage import database
from yiju.storage.cards import identity
from yiju.storage.database import connect, initialize
from yiju.storage.importing import import_xml
from yiju.storage.learning import known_sentences, review_card, undo_review
from yiju.storage.sentences import update_sentence


def test_import_creates_stable_listening_cards_for_words_and_sentences(
    connection: sqlite3.Connection, xml_export: bytes
) -> None:
    import_xml(connection, xml_export, "words.xml")
    before = {row["id"]: dict(row) for row in connection.execute("SELECT * FROM cards")}
    assert len(before) == 15
    listening = [row for row in before.values() if row["direction"] == "listening"]
    assert len(listening) == 5
    for card in listening:
        sentence_id = card["sentence_id"]
        kind = "sentence-card-listening" if sentence_id else "word-listening"
        assert card["id"] == identity(kind, sentence_id or card["word"])
        siblings = [
            row
            for row in before.values()
            if row["word"] == card["word"] and row["sentence_id"] == sentence_id
        ]
        assert {row["direction"] for row in siblings} == {
            "recognition",
            "production",
            "listening",
        }
        assert len({Card.from_json(row["scheduling"]).card_id for row in siblings}) == 3
    import_xml(connection, xml_export, "same.xml")
    import_xml(
        connection,
        xml_export.replace(b"<cards>", b"<categories/><cards>"),
        "again.xml",
    )
    assert {
        row["id"]: dict(row) for row in connection.execute("SELECT * FROM cards")
    } == before


@pytest.mark.parametrize("sentence", [False, True])
def test_listening_reviews_have_independent_schedules_and_shared_knowledge(
    connection: sqlite3.Connection, xml_export: bytes, *, sentence: bool
) -> None:
    import_xml(connection, xml_export, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    if sentence:
        seed_known_word(connection, "我", now)
    cards = {
        row["direction"]: dict(row)
        for row in connection.execute(
            "SELECT * FROM cards WHERE word = '我' AND (sentence_id IS NOT NULL) = ?",
            (int(sentence),),
        )
    }
    original_knowledge = {
        row["word"]: dict(row) for row in connection.execute("SELECT * FROM knowledge")
    }
    listening = cards["listening"]
    first_review = review_card(
        connection, listening["id"], listening["version"], 3, now
    )
    for direction in ("recognition", "production"):
        sibling = cards[direction]
        assert (
            dict(
                connection.execute(
                    "SELECT * FROM cards WHERE id = ?", (sibling["id"],)
                ).fetchone()
            )
            == sibling
        )
    after_listening = {
        row["word"]: dict(row) for row in connection.execute("SELECT * FROM knowledge")
    }
    learned_word = "去" if sentence else "我"
    assert after_listening[learned_word]["success_days"] == 1
    recognition = cards["recognition"]
    second_review = review_card(
        connection, recognition["id"], recognition["version"], 3, now
    )
    assert (
        connection.execute(
            "SELECT success_days FROM knowledge WHERE word = ?", (learned_word,)
        ).fetchone()[0]
        == 1
    )
    undo_review(connection, second_review)
    assert {
        row["word"]: dict(row) for row in connection.execute("SELECT * FROM knowledge")
    } == after_listening
    undo_review(connection, first_review)
    assert {
        row["word"]: dict(row) for row in connection.execute("SELECT * FROM knowledge")
    } == original_knowledge


def test_listening_sentence_reviews_contribute_to_learned_count(
    connection: sqlite3.Connection, xml_export: bytes
) -> None:
    import_xml(connection, xml_export, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    seed_known_word(connection, "我", now)
    card = connection.execute(
        "SELECT * FROM cards WHERE word = '我' AND sentence_id IS NOT NULL "
        "AND direction = 'listening'"
    ).fetchone()
    sentence_id = card["sentence_id"]
    for _ in range(3):
        now = max(now + timedelta(days=1), datetime.fromisoformat(card["due"]))
        review_card(connection, card["id"], card["version"], 3, now)
        card = connection.execute(
            "SELECT * FROM cards WHERE id = ?", (card["id"],)
        ).fetchone()
    assert known_sentences(connection, now) == frozenset({sentence_id})
    now = datetime.fromisoformat(card["due"])
    review = review_card(connection, card["id"], card["version"], 1, now)
    assert known_sentences(connection, now) == frozenset()
    undo_review(connection, review)
    assert known_sentences(connection, now) == frozenset({sentence_id})


@pytest.fixture
def legacy_database(tmp_path: Path, xml_export: bytes) -> Path:
    source = tmp_path / "source.sqlite3"
    initialize(source)
    with connect(source) as db:
        import_xml(db, xml_export, "words.xml")
        now = datetime.now(UTC) + timedelta(seconds=1)
        for direction in ("recognition", "production"):
            card = db.execute(
                "SELECT * FROM cards WHERE word = '我' AND sentence_id IS NULL "
                "AND direction = ?",
                (direction,),
            ).fetchone()
            review = review_card(db, card["id"], card["version"], 3, now)
        undo_review(db, review)
        db.execute("DELETE FROM cards WHERE direction = 'listening'")
        db.execute("UPDATE cards SET suspended = 1, version = 7 WHERE word = '学习'")
        db.execute(
            "UPDATE sentences SET status = 'rejected' WHERE chinese = '我学习中文。'"
        )
        db.commit()
        legacy = "\n".join(db.iterdump()).replace(
            "IN ('recognition', 'production', 'listening')",
            "IN ('recognition', 'production')",
        )
    path = tmp_path / "legacy.sqlite3"
    with connect(path) as db:
        db.execute("PRAGMA foreign_keys = OFF")
        db.executescript(legacy)
        db.execute("PRAGMA user_version = 7")
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    return path


def test_upgrade_preserves_history_and_pauses_and_is_safe_to_repeat(
    legacy_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with connect(legacy_database) as db:
        before = {row["id"]: dict(row) for row in db.execute("SELECT * FROM cards")}
        queries = (
            "SELECT * FROM reviews",
            "SELECT * FROM review_words",
            "SELECT * FROM knowledge",
            "SELECT * FROM sentences",
            "SELECT * FROM settings",
        )
        history = {
            query: [tuple(row) for row in db.execute(query)] for query in queries
        }

    def unexpected_parsing(_text: str) -> Never:
        message = "Listening migration must not reparse dictionary text."
        raise AssertionError(message)

    monkeypatch.setattr(database, "parse_definition", unexpected_parsing)
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(initialize, [legacy_database] * 3))
    for _ in range(2):
        initialize(legacy_database)
        with connect(legacy_database) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 8
            assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            assert {
                row["id"]: dict(row)
                for row in db.execute(
                    "SELECT * FROM cards WHERE direction != 'listening'"
                )
            } == before
            assert db.execute("SELECT count(*) FROM cards").fetchone()[0] == 15
            for row in db.execute("SELECT * FROM cards WHERE direction = 'listening'"):
                assert row["reviews"] == 0
                assert row["version"] == 0
                assert row["suspended"] == int(row["word"] == "学习")
            for query, rows in history.items():
                assert [tuple(row) for row in db.execute(query)] == rows
            foreign_keys = db.execute("PRAGMA foreign_key_list(reviews)").fetchall()
            assert {row["table"] for row in foreign_keys} == {"cards"}
            indexes = db.execute("PRAGMA index_list(cards)").fetchall()
            assert {"cards_due", "cards_sentence"} <= {row["name"] for row in indexes}
    with connect(legacy_database) as db:
        review = db.execute("SELECT id FROM reviews WHERE undone = 0").fetchone()[0]
        undo_review(db, review)
        assert db.execute("SELECT count(*) FROM knowledge").fetchone()[0] == 0


def test_failed_upgrade_rolls_back_card_schema_and_history(
    legacy_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with connect(legacy_database) as db:
        before = "\n".join(db.iterdump())

    def failed_creation(_connection: sqlite3.Connection) -> Never:
        message = "Unable to add listening cards."
        raise RuntimeError(message)

    monkeypatch.setattr(database, "_create_direction_cards", failed_creation)
    with pytest.raises(RuntimeError, match="Unable to add listening cards"):
        initialize(legacy_database)
    with connect(legacy_database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 7
        assert "\n".join(db.iterdump()) == before
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_rejecting_and_restoring_sentence_preserves_listening_progress(
    connection: sqlite3.Connection, xml_export: bytes
) -> None:
    import_xml(connection, xml_export, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    seed_known_word(connection, "我", now)
    card = connection.execute(
        "SELECT * FROM cards WHERE word = '我' AND sentence_id IS NOT NULL "
        "AND direction = 'listening'"
    ).fetchone()
    review_card(connection, card["id"], card["version"], 3, now)
    reviewed = dict(
        connection.execute("SELECT * FROM cards WHERE id = ?", (card["id"],)).fetchone()
    )
    sentence = connection.execute(
        "SELECT * FROM sentences WHERE id = ?", (card["sentence_id"],)
    ).fetchone()
    for version, status in enumerate(("rejected", "accepted")):
        update_sentence(
            connection,
            sentence["id"],
            version,
            sentence["chinese"],
            sentence["pinyin"],
            sentence["translation"],
            status,
            now,
        )
        updated = connection.execute(
            "SELECT * FROM cards WHERE id = ?", (card["id"],)
        ).fetchone()
        assert updated["suspended"] == int(status == "rejected")
        assert updated["scheduling"] == reviewed["scheduling"]
        assert updated["reviews"] == reviewed["reviews"]
