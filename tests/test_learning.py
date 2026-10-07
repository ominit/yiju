import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fsrs import Card, State

from yiju.core.scheduling import is_known
from yiju.storage.importing import import_xml
from yiju.storage.learning import ConflictError, known_words, review_card, undo_review
from yiju.storage.queue import study_queue
from yiju.storage.sentences import update_sentence


def test_import_is_repeatable_and_keeps_reviews(
    connection: sqlite3.Connection, xml_export: bytes
) -> None:
    batch = import_xml(connection, xml_export, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    first = study_queue(connection, now)[0]
    review_card(connection, first["id"], first["version"], 3, now)
    assert import_xml(connection, xml_export, "renamed.xml") == batch
    assert connection.execute("SELECT count(*) FROM cards").fetchone()[0] == 3
    assert connection.execute("SELECT count(*) FROM reviews").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM sentences").fetchone()[0] == 2
    second = import_xml(
        connection, xml_export.replace(b"<cards>", b"<categories/><cards>"), "new.xml"
    )
    receipt = json.loads(
        connection.execute(
            "SELECT summary FROM imports WHERE id = ?", (second,)
        ).fetchone()[0]
    )
    assert receipt["duplicates"] == 3
    assert receipt["added"] == 0


def test_reviews_are_versioned_and_undo_restores_progress(
    connection: sqlite3.Connection, xml_export: bytes
) -> None:
    import_xml(connection, xml_export, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    assert known_words(connection, now) == frozenset()
    first = study_queue(connection, now)[0]
    review_card(connection, first["id"], first["version"], 3, now)
    assert known_words(connection, now) == frozenset()
    with pytest.raises(ConflictError):
        review_card(connection, first["id"], first["version"], 3, now)
    review = connection.execute("SELECT id FROM reviews").fetchone()[0]
    undo_review(connection, review)
    restored = connection.execute(
        "SELECT * FROM cards WHERE id = ?", (first["id"],)
    ).fetchone()
    assert restored["scheduling"] == first["scheduling"]
    assert restored["reviews"] == 0
    assert restored["version"] > first["version"]
    assert connection.execute("SELECT count(*) FROM knowledge").fetchone()[0] == 0
    with pytest.raises(ConflictError):
        undo_review(connection, review)


def test_familiarity_requires_days_stability_and_recall() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    card = Card(
        card_id=1,
        state=State.Review,
        stability=10,
        difficulty=3,
        last_review=now,
        due=now,
    )
    assert not is_known(card, 2, now)
    assert is_known(card, 3, now)
    assert not is_known(card, 3, now + timedelta(days=100))


def test_sentence_is_unlocked_by_knowledge_and_only_target_is_updated(
    connection: sqlite3.Connection, xml_export: bytes
) -> None:
    import_xml(connection, xml_export, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    sentence = connection.execute(
        "SELECT * FROM sentences WHERE chinese = '我学习中文。'"
    ).fetchone()
    update_sentence(
        connection,
        sentence["id"],
        0,
        sentence["chinese"],
        sentence["pinyin"],
        sentence["translation"],
        "accepted",
        now,
    )
    assert not any(
        row["sentence_id"] == sentence["id"] for row in study_queue(connection, now)
    )
    mature = Card(
        card_id=1,
        state=State.Review,
        stability=20,
        difficulty=3,
        due=now,
        last_review=now,
    )
    connection.execute(
        "INSERT INTO knowledge VALUES (?, ?, ?, ?)",
        ("我", mature.to_json(), 3, now.date().isoformat()),
    )
    connection.commit()
    sentence_card = next(
        row
        for row in study_queue(connection, now)
        if row["sentence_id"] == sentence["id"]
    )
    review_card(connection, sentence_card["id"], sentence_card["version"], 3, now)
    words = {row[0] for row in connection.execute("SELECT word FROM knowledge")}
    assert words == {"我", "学习"}
    update_sentence(
        connection,
        sentence["id"],
        1,
        sentence["chinese"],
        sentence["pinyin"],
        sentence["translation"],
        "rejected",
        now,
    )
    assert not any(
        row["sentence_id"] == sentence["id"]
        for row in study_queue(connection, now + timedelta(days=1))
    )


def test_daily_new_limit_and_same_day_successes(
    connection: sqlite3.Connection, xml_export: bytes
) -> None:
    import_xml(connection, xml_export, "words.xml")
    connection.execute("UPDATE settings SET value = '1' WHERE key = 'new_per_day'")
    connection.commit()
    now = datetime.now(UTC) + timedelta(seconds=1)
    assert len(study_queue(connection, now)) == 1
    first = study_queue(connection, now)[0]
    review_card(connection, first["id"], first["version"], 3, now)
    assert study_queue(connection, now) == []
    later = now + timedelta(minutes=15)
    review = study_queue(connection, later)[0]
    review_card(connection, review["id"], review["version"], 3, later)
    assert connection.execute("SELECT success_days FROM knowledge").fetchone()[0] == 1


def test_multiple_dictionary_senses_share_card_without_losing_definitions(
    connection: sqlite3.Connection,
    xml_export: bytes,
) -> None:
    import_xml(connection, xml_export, "first.xml")
    modified = xml_export.replace(b'entryid="1"', b'entryid="4"').replace(
        b"pronoun I; me",
        b"another meaning",
    )
    import_xml(connection, modified, "second.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    cards = [row for row in study_queue(connection, now) if row["word"] == "我"]
    assert len(cards) == 1
    assert "pronoun I; me" in cards[0]["definition"]
    assert "another meaning" in cards[0]["definition"]
