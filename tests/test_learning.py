import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from conftest import seed_known_word
from fsrs import Card, Rating, State

from yiju.core.scheduling import is_known, new_card, scheduler
from yiju.storage.cards import identity
from yiju.storage.database import vocabulary_words
from yiju.storage.importing import import_xml
from yiju.storage.learning import (
    ConflictError,
    known_sentences,
    known_words,
    review_card,
    undo_review,
)
from yiju.storage.queue import overview, study_queue
from yiju.storage.sentences import update_sentence


def test_import_is_repeatable_and_keeps_reviews(
    connection: sqlite3.Connection, xml_export: bytes
) -> None:
    batch = import_xml(connection, xml_export, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    first = study_queue(connection, now)[0]
    review_card(connection, first["id"], first["version"], 3, now)
    assert import_xml(connection, xml_export, "renamed.xml") == batch
    assert connection.execute("SELECT count(*) FROM cards").fetchone()[0] == 5
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


def test_new_sentences_wait_for_a_known_word_but_reviews_continue(
    connection: sqlite3.Connection, xml_export: bytes
) -> None:
    import_xml(connection, xml_export, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    queue = study_queue(connection, now)
    assert queue
    assert all(row["sentence_id"] is None for row in queue)
    card = connection.execute(
        "SELECT * FROM cards WHERE sentence_id IS NOT NULL AND word = '我'"
    ).fetchone()
    with pytest.raises(ConflictError, match="at least one known word"):
        review_card(connection, card["id"], card["version"], 3, now)
    assert connection.execute("SELECT count(*) FROM reviews").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM knowledge").fetchone()[0] == 0
    seed_known_word(connection, "学习", now)
    assert all(row["sentence_id"] is None for row in study_queue(connection, now))
    seed_known_word(connection, "我", now)
    assert any(row["id"] == card["id"] for row in study_queue(connection, now))
    review_card(connection, card["id"], card["version"], 1, now)
    assert "我" not in known_words(connection, now)
    assert any(
        row["id"] == card["id"]
        for row in study_queue(connection, now + timedelta(minutes=15))
    )


@pytest.mark.parametrize("rating", [1, 2])
def test_sentence_learning_uses_its_own_history_and_contributes_to_words(
    connection: sqlite3.Connection, xml_export: bytes, rating: int
) -> None:
    import_xml(connection, xml_export, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    seed_known_word(connection, "我", now)
    seed_known_word(connection, "去", now)
    card = connection.execute(
        "SELECT * FROM cards WHERE sentence_id IS NOT NULL AND word = '我'"
    ).fetchone()
    assert not known_sentences(connection, now)
    connection.execute("DELETE FROM knowledge WHERE word = '去'")
    connection.commit()
    for index in range(4):
        review_card(connection, card["id"], card["version"], 3, now)
        card = connection.execute(
            "SELECT * FROM cards WHERE id = ?", (card["id"],)
        ).fetchone()
        if index < 3:
            assert not known_sentences(connection, now)
            now = datetime.fromisoformat(card["due"])
    assert known_sentences(connection, now) == frozenset({card["sentence_id"]})
    assert known_words(connection, now) == frozenset({"我", "去"})
    assert overview(connection, now)["learned_sentences"] == 1
    assert not known_sentences(connection, now + timedelta(days=3650))
    assert import_xml(connection, xml_export, "again.xml") == 1
    assert known_sentences(connection, now) == frozenset({card["sentence_id"]})
    now = datetime.fromisoformat(card["due"])
    review_card(connection, card["id"], card["version"], rating, now)
    assert bool(known_sentences(connection, now)) == (rating == 2)
    latest = connection.execute(
        "SELECT id FROM reviews ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    undo_review(connection, latest)
    assert known_sentences(connection, now) == frozenset({card["sentence_id"]})
    sentence = connection.execute(
        "SELECT * FROM sentences WHERE id = ?", (card["sentence_id"],)
    ).fetchone()
    for version, status in [(0, "rejected"), (1, "accepted")]:
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
        assert overview(connection, now)["learned_sentences"] == int(
            status == "accepted"
        )
    connection.execute("UPDATE settings SET value = '4' WHERE key = 'known_days'")
    connection.commit()
    assert not known_sentences(connection, now)


def test_sentence_is_unlocked_by_knowledge_and_updates_every_word(
    connection: sqlite3.Connection, xml_export: bytes
) -> None:
    import_xml(connection, xml_export, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    sentence = connection.execute(
        "SELECT * FROM sentences WHERE chinese = '我学习中文。'"
    ).fetchone()
    assert sentence["status"] == "accepted"
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
    assert not any(
        row["sentence_id"] == sentence["id"] for row in study_queue(connection, now)
    )
    import_xml(
        connection,
        '<plecoflash formatversion="2"><cards><card language="chinese"><entry>'
        '<headword charset="sc">中文</headword><pron type="hypy">zhong1wen2</pron>'
        "<defn>Chinese language</defn></entry></card></cards></plecoflash>".encode(),
        "chinese.xml",
    )
    sentence_card = next(
        row
        for row in study_queue(connection, now)
        if row["sentence_id"] == sentence["id"]
    )
    review_card(connection, sentence_card["id"], sentence_card["version"], 3, now)
    words = {row[0] for row in connection.execute("SELECT word FROM knowledge")}
    assert words == {"我", "学习", "中文"}
    update_sentence(
        connection,
        sentence["id"],
        0,
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
    assert all(
        row[0] == 1 for row in connection.execute("SELECT success_days FROM knowledge")
    )


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
    cards = [
        row
        for row in study_queue(connection, now)
        if row["word"] == "我" and row["sentence_id"] is None
    ]
    assert len(cards) == 1
    assert "pronoun I; me" in cards[0]["definition"]
    assert "another meaning" in cards[0]["definition"]


def test_skip_and_restore_sentence_preserve_reviews_and_survive_reimport(
    connection: sqlite3.Connection, xml_export: bytes
) -> None:
    import_xml(connection, xml_export, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    seed_known_word(connection, "我", now)
    card = next(row for row in study_queue(connection, now) if row["sentence_id"])
    review_card(connection, card["id"], card["version"], 3, now)
    reviewed = dict(
        connection.execute("SELECT * FROM cards WHERE id = ?", (card["id"],)).fetchone()
    )
    sentence = connection.execute(
        "SELECT * FROM sentences WHERE id = ?", (card["sentence_id"],)
    ).fetchone()
    for version, status in [(0, "rejected"), (1, "accepted")]:
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
        for field in ["scheduling", "due", "reviews"]:
            assert updated[field] == reviewed[field]
        if status == "rejected":
            import_xml(
                connection,
                xml_export.replace(b"<cards>", b"<categories/><cards>"),
                "again.xml",
            )
            assert (
                connection.execute(
                    "SELECT status FROM sentences WHERE id = ?", (sentence["id"],)
                ).fetchone()[0]
                == "rejected"
            )
            assert not any(
                row["id"] == card["id"]
                for row in study_queue(connection, now + timedelta(days=1))
            )
    with pytest.raises(ConflictError):
        update_sentence(
            connection,
            sentence["id"],
            0,
            sentence["chinese"],
            sentence["pinyin"],
            sentence["translation"],
            "rejected",
            now,
        )


def test_missing_vocabulary_blocks_reviewed_sentences_without_losing_progress(
    connection: sqlite3.Connection, xml_export: bytes
) -> None:
    import_xml(connection, xml_export, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    seed_known_word(connection, "我", now)
    card = next(row for row in study_queue(connection, now) if row["sentence_id"])
    review_card(connection, card["id"], card["version"], 3, now)
    connection.execute(
        "UPDATE sentences SET tokens = ? WHERE id = ?",
        (json.dumps([card["word"], "中文"]), card["sentence_id"]),
    )
    connection.commit()
    assert "中文" not in vocabulary_words(connection)
    reviewed = dict(
        connection.execute("SELECT * FROM cards WHERE id = ?", (card["id"],)).fetchone()
    )
    later = now + timedelta(days=1)
    assert not any(row["id"] == card["id"] for row in study_queue(connection, later))
    import_xml(
        connection,
        '<plecoflash formatversion="2"><cards><card language="chinese"><entry>'
        '<headword charset="sc">中文</headword><pron type="hypy">zhong1wen2</pron>'
        "<defn>Chinese language</defn></entry></card></cards></plecoflash>".encode(),
        "chinese.xml",
    )
    assert any(row["id"] == card["id"] for row in study_queue(connection, later))
    assert (
        dict(
            connection.execute(
                "SELECT * FROM cards WHERE id = ?", (card["id"],)
            ).fetchone()
        )
        == reviewed
    )


@pytest.mark.parametrize("rating", [1, 2, 3, 4])
def test_sentence_review_updates_each_word_once_and_undo_restores_all(
    connection: sqlite3.Connection, xml_export: bytes, rating: int
) -> None:
    import_xml(connection, xml_export, "words.xml")
    now = datetime.now(UTC) + timedelta(seconds=1)
    existing = Card(
        card_id=int(identity("word", "我")[:12], 16),
        state=State.Review,
        stability=20,
        difficulty=3,
        due=now,
        last_review=now - timedelta(days=1),
    )
    connection.execute(
        "INSERT INTO knowledge VALUES (?, ?, ?, ?)",
        ("我", existing.to_json(), 3, (now - timedelta(days=1)).date().isoformat()),
    )
    connection.commit()
    before = dict(
        connection.execute("SELECT * FROM knowledge WHERE word = '我'").fetchone()
    )
    sentence = connection.execute(
        "SELECT * FROM sentences WHERE chinese = '我去。'"
    ).fetchone()
    update_sentence(
        connection,
        sentence["id"],
        sentence["version"],
        "我去我。",
        "Wǒ qù wǒ.",
        "I go.",
        "accepted",
        now,
    )
    card = next(
        row
        for row in study_queue(connection, now)
        if row["sentence_id"] == sentence["id"]
    )
    review_card(connection, card["id"], card["version"], rating, now)
    expected_existing, _ = scheduler().review_card(existing, Rating(rating), now)
    expected_new, _ = scheduler().review_card(
        new_card(identity("word", "去"), now), Rating(rating), now
    )
    states = {
        row["word"]: dict(row) for row in connection.execute("SELECT * FROM knowledge")
    }
    assert set(states) == {"我", "去"}
    assert states["我"]["scheduling"] == expected_existing.to_json()
    assert states["去"]["scheduling"] == expected_new.to_json()
    assert states["我"]["success_days"] == (
        0 if rating == 1 else 4 if rating >= 3 else 3
    )
    assert states["去"]["success_days"] == int(rating >= 3)
    review_id = connection.execute("SELECT id FROM reviews").fetchone()[0]
    assert {
        row[0]
        for row in connection.execute(
            "SELECT word FROM review_words WHERE review_id = ?", (review_id,)
        )
    } == {"我", "去"}
    undo_review(connection, review_id)
    assert [dict(row) for row in connection.execute("SELECT * FROM knowledge")] == [
        before
    ]
    restored = connection.execute(
        "SELECT * FROM cards WHERE id = ?", (card["id"],)
    ).fetchone()
    assert restored["scheduling"] == card["scheduling"]
    assert restored["reviews"] == card["reviews"]
