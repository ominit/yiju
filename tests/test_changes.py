from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from conftest import add, set_strength

from yiju.storage.database import ConflictError
from yiju.storage.learning import review_card
from yiju.storage.pending import (
    approve,
    approve_change,
    approve_word,
    discard,
    edit_change,
    editing,
    library_words_error,
    preview,
    propose_removal,
    propose_sentence,
    propose_word,
    remove_change,
    stage,
    stage_names,
    waiting,
)
from yiju.storage.sentences import statuses, update_sentence

if TYPE_CHECKING:
    import sqlite3

NOW = datetime(2026, 10, 1, 9, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
TEA = "我喜欢喝茶。 | I like drinking tea.\n"


def words(connection: sqlite3.Connection, sentence_id: int = 1) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT word FROM sentence_words WHERE sentence_id = ? ORDER BY position",
            (sentence_id,),
        )
    ]


def cards(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT id, word, sentence_id FROM cards ORDER BY id"
        )
    ]


def count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # noqa: S608


def review_sentence(connection: sqlite3.Connection) -> None:
    for word in words(connection):
        set_strength(connection, word, now=NOW)
    card = connection.execute(
        "SELECT * FROM cards WHERE sentence_id = 1 AND card_type = 'recognition'"
    ).fetchone()
    review_card(connection, card["id"], card["version"], 3, LATER)


def test_a_new_split_waits_for_approval_and_keeps_the_cards(
    connection: sqlite3.Connection,
) -> None:
    add(connection, TEA, NOW)
    assert words(connection) == ["我", "喜欢", "喝茶"]
    before = cards(connection)
    with editing(connection):
        change = propose_sentence(connection, 1, words="我 喜欢 喝 茶")
    connection.commit()
    assert words(connection) == ["我", "喜欢", "喝茶"]
    assert waiting(connection) == 1
    result = preview(connection)
    assert result.ready
    assert result.adds == {"sentences": 0, "words": 2, "names": 0, "changes": 1}
    [item] = result.changes
    assert (item.id, item.kind, item.action) == (change, "sentence", "edit")
    assert item.after is not None
    assert [token.text for token in item.after.words] == ["我", "喜欢", "喝", "茶"]
    assert item.after.pinyin == "wǒ xǐhuan hē chá."
    assert item.removed == {"喝茶"}
    assert {word.word: word.changes for word in result.words} == {
        "喝": [change],
        "茶": [change],
    }
    data = result.to_json()["changes"][0]
    assert data["target"] == 1
    assert data["before"]["words"] == ["我", "喜欢", "喝茶"]
    assert [token["text"] for token in data["after"]["words"]] == [
        "我",
        "喜欢",
        "喝",
        "茶",
    ]
    assert approve(connection, result.version, NOW) == {
        "words": 2,
        "sentences": 0,
        "names": 0,
        "changes": 1,
        "waiting": 0,
    }
    assert words(connection) == ["我", "喜欢", "喝", "茶"]
    assert connection.execute("SELECT pinyin FROM sentences").fetchone()[0] == (
        "wǒ xǐhuan hē chá."
    )
    assert cards(connection)[: len(before)] == before
    assert preview(connection).empty


def test_a_sentence_with_new_unseen_words_waits_until_they_are_seen(
    connection: sqlite3.Connection,
) -> None:
    add(connection, TEA, NOW)
    review_sentence(connection)
    assert connection.execute("SELECT state FROM sentences").fetchone()[0] == (
        "unlocked"
    )
    with editing(connection):
        propose_sentence(connection, 1, words="我 喜欢 喝 茶")
    approve(connection, preview(connection).version, LATER)
    assert connection.execute("SELECT state FROM sentences").fetchone()[0] == (
        "eligible"
    )
    assert {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT word, state FROM words WHERE word IN ('喝', '茶')"
        )
    } == {"喝": "unlocked", "茶": "unlocked"}
    assert count(connection, "reviews") == 1


def test_proposals_merge_and_keep_what_they_do_not_mention(
    connection: sqlite3.Connection,
) -> None:
    add(connection, TEA, NOW)
    with editing(connection):
        first = propose_sentence(connection, 1, translation="I like tea.")
    with editing(connection):
        second = propose_sentence(connection, 1, pinyin="wǒ xǐhuan hēchá.")
    assert first == second
    [item] = preview(connection).changes
    assert item.after is not None
    assert (item.after.translation, item.after.pinyin) == (
        "I like tea.",
        "wǒ xǐhuan hēchá.",
    )
    assert not item.issues
    with editing(connection):
        propose_sentence(connection, 1, translation="I like drinking tea.")
    with editing(connection):
        propose_sentence(connection, 1, pinyin="wǒ xǐhuan hēchá.")
    [item] = preview(connection).changes
    assert item.issues == ("This leaves sentence 1 as it is.",)
    assert not preview(connection).ready


def test_a_new_split_must_spell_the_sentence(connection: sqlite3.Connection) -> None:
    add(connection, TEA, NOW)
    assert library_words_error(connection, 1, "我 喜欢 茶") == (
        "The words must spell out the sentence's characters in order."
    )
    assert library_words_error(connection, 1, "我 喜欢 喝 茶") is None
    with pytest.raises(LookupError, match="no sentence 9"):
        library_words_error(connection, 9, "我")
    with pytest.raises(LookupError, match="no sentence 9"):
        propose_sentence(connection, 9, words="我")


def test_slots_stay_in_place_when_a_sentence_is_split_again(
    connection: sqlite3.Connection,
) -> None:
    add(connection, "{A}喜欢喝茶。 | {A} likes drinking tea.\n", NOW)
    with editing(connection):
        propose_sentence(connection, 1, words="{A} 喜欢 喝 茶")
    [item] = preview(connection).changes
    assert item.before is not None
    assert item.after is not None
    assert [token.text for token in item.before.words] == ["{A}", "喜欢", "喝茶"]
    assert [token.slot for token in item.after.words] == [True, False, False, False]
    assert item.after.pinyin == "{A} xǐhuan hē chá."
    assert not item.issues
    approve(connection, preview(connection).version, NOW)
    assert words(connection) == ["喜欢", "喝", "茶"]


def test_a_word_leaves_only_after_every_sentence_drops_it(
    connection: sqlite3.Connection,
) -> None:
    add(connection, TEA, NOW)
    connection.execute(
        "INSERT INTO flags VALUES ('喝茶', 'split', '2026-01-01', '2026-01-01')"
    )
    review_sentence(connection)
    with editing(connection):
        removal = propose_removal(connection, "word", "喝茶")
    [item] = preview(connection).changes
    assert item.issues == (
        "喝茶 is still in 我喜欢喝茶。 Change or remove that sentence first.",
    )
    assert [(use.chinese, use.frees) for use in item.uses] == [("我喜欢喝茶。", False)]
    with editing(connection):
        resplit = propose_sentence(connection, 1, words="我 喜欢 喝 茶")
    result = preview(connection)
    assert result.ready
    item = next(change for change in result.changes if change.id == removal)
    assert item.waits_on == (resplit,)
    assert item.uses[0].frees
    with pytest.raises(ConflictError, match="take 喝茶 out of its sentences first"):
        approve_change(connection, result.version, removal, LATER)
    assert approve(connection, result.version, LATER, "removals")["waiting"] == 1
    assert words(connection) == ["我", "喜欢", "喝茶"]
    approve_change(connection, preview(connection).version, resplit, LATER)
    approve_change(connection, preview(connection).version, removal, LATER)
    assert not connection.execute("SELECT 1 FROM words WHERE word = '喝茶'").fetchone()
    assert not connection.execute("SELECT 1 FROM cards WHERE word = '喝茶'").fetchone()
    assert count(connection, "flags") == 0
    assert not connection.execute(
        "SELECT 1 FROM review_words WHERE word = '喝茶'"
    ).fetchone()
    assert count(connection, "reviews") == 1
    assert connection.execute("PRAGMA foreign_key_check").fetchone() is None


def test_waiting_sentences_also_keep_a_word_in_the_library(
    connection: sqlite3.Connection,
) -> None:
    add(connection, "我去。 | I am going.\n", NOW)
    with editing(connection):
        propose_removal(connection, "word", "去")
    with editing(connection):
        propose_removal(connection, "sentence", "1")
    assert preview(connection).ready
    stage(connection, "我去学校。 | I am going to school.\n")
    assert preview(connection).issues == (
        (
            "Change 1: 去 is still in the waiting sentence 我去学校。 "
            "Change or remove that sentence first."
        ),
    )


def test_removing_a_sentence_deletes_its_cards_and_history(
    connection: sqlite3.Connection,
) -> None:
    add(connection, TEA + "我去。 | I am going.\n", NOW)
    review_sentence(connection)
    with editing(connection):
        change = propose_removal(connection, "sentence", "1")
    [item] = preview(connection).changes
    assert (item.action, item.reviews) == ("remove", 1)
    assert item.after is None
    assert preview(connection).to_json()["changes"][0] == {
        "id": change,
        "kind": "sentence",
        "target": 1,
        "action": "remove",
        "issues": [],
        "chinese": "我喜欢喝茶。",
        "translation": "I like drinking tea.",
        "reviews": 1,
    }
    approve(connection, preview(connection).version, LATER)
    assert [row[0] for row in connection.execute("SELECT id FROM sentences")] == [2]
    assert not connection.execute(
        "SELECT 1 FROM cards WHERE sentence_id = 1"
    ).fetchone()
    assert count(connection, "reviews") == 0
    assert count(connection, "review_words") == 0
    assert count(connection, "words") == 4
    assert connection.execute("PRAGMA foreign_key_check").fetchone() is None


def test_a_new_definition_replaces_the_dictionary_one_on_approval(
    connection: sqlite3.Connection,
) -> None:
    add(connection, TEA, NOW)
    original = connection.execute(
        "SELECT readings FROM words WHERE word = '喝茶'"
    ).fetchone()[0]
    with editing(connection):
        change = propose_word(connection, "喝茶", meanings="to drink tea\nto have tea")
    [item] = preview(connection).changes
    assert item.current == original
    assert item.pinyin == "hēchá"
    assert item.meanings == "to drink tea\nto have tea"
    assert preview(connection).to_json()["changes"][0]["after"] == [
        {"pinyin": "hēchá", "meanings": ["to drink tea", "to have tea"]}
    ]
    with editing(connection):
        edit_change(connection, change, meanings="", pinyin="hē chá")
    assert preview(connection).changes[0].issues == ("Give 喝茶 at least one meaning.",)
    with editing(connection):
        edit_change(connection, change, meanings="to drink tea", pinyin="hē chá")
    approve_change(connection, preview(connection).version, change, NOW)
    row = connection.execute(
        "SELECT source, readings FROM words WHERE word = '喝茶'"
    ).fetchone()
    assert row["source"] == "manual"
    assert "to drink tea" in row["readings"]
    assert "hē chá" in row["readings"]


def test_proposals_can_be_dropped_one_at_a_time_or_all_at_once(
    connection: sqlite3.Connection,
) -> None:
    add(connection, TEA, NOW)
    with editing(connection):
        change = propose_removal(connection, "sentence", "1")
    with editing(connection):
        assert propose_sentence(connection, 1, words="我 喜欢 喝 茶") == change
    assert preview(connection).changes[0].action == "edit"
    with editing(connection):
        assert propose_removal(connection, "sentence", "1") == change
    assert preview(connection).changes[0].action == "remove"
    with editing(connection):
        remove_change(connection, change)
    assert preview(connection).empty
    with pytest.raises(LookupError, match="no pending change"):
        remove_change(connection, change)
    with pytest.raises(LookupError, match="not in your library"):
        propose_removal(connection, "word", "茶")
    with editing(connection):
        propose_word(connection, "喝茶", meanings="tea")
    discard(connection, preview(connection).version)
    assert waiting(connection) == 0
    assert words(connection) == ["我", "喜欢", "喝茶"]


def test_one_section_is_approved_or_discarded_without_the_others(
    connection: sqlite3.Connection,
) -> None:
    add(connection, TEA, NOW)
    stage(connection, "我去。 | I am going.\n苹果\n")
    stage_names(connection, "王芳 female\n")
    with editing(connection):
        propose_sentence(connection, 1, words="我 喜欢 喝 茶")
        propose_removal(connection, "word", "喝茶")
    result = preview(connection)
    sections = ("sentences", "words", "names", "edits", "removals")
    assert {name: len(result.section(name)) for name in sections} == dict.fromkeys(
        sections, 1
    )
    assert approve(connection, result.version, NOW, "edits") == {
        "words": 2,
        "sentences": 0,
        "names": 0,
        "changes": 1,
        "waiting": 0,
    }
    assert words(connection) == ["我", "喜欢", "喝", "茶"]
    result = preview(connection)
    assert [len(result.section(name)) for name in sections] == [1, 1, 1, 0, 1]
    discard(connection, result.version, "words")
    result = preview(connection)
    assert [word.word for word in result.words] == ["去"]
    approve(connection, result.version, NOW, "removals")
    assert not connection.execute("SELECT 1 FROM words WHERE word = '喝茶'").fetchone()
    approve(connection, preview(connection).version, NOW, "sentences")
    assert words(connection, 2) == ["我", "去"]
    result = preview(connection)
    assert [len(result.section(name)) for name in sections] == [0, 0, 1, 0, 0]
    with pytest.raises(ConflictError, match="Fix the problems"):
        approve(connection, result.version, NOW, "words")
    discard(connection, result.version, "names")
    assert preview(connection).empty


def test_problems_are_grouped_by_section(connection: sqlite3.Connection) -> None:
    add(connection, TEA, NOW)
    stage(connection, "我去。 | I am going.\n嗯嗯嗯\n")
    with editing(connection):
        propose_removal(connection, "word", "喝茶")
    result = preview(connection)
    assert result.problems == {
        "sentences": (),
        "words": ("嗯嗯嗯 has no definition.",),
        "names": (),
        "edits": (),
        "removals": (
            (
                "Change 1: 喝茶 is still in 我喜欢喝茶。 "
                "Change or remove that sentence first."
            ),
        ),
    }
    approve(connection, result.version, NOW, "sentences")
    with pytest.raises(ConflictError, match="definition"):
        approve_word(connection, preview(connection).version, "嗯嗯嗯", NOW)
    stage(connection, "苹果\n")
    approve_word(connection, preview(connection).version, "苹果", NOW)
    assert connection.execute("SELECT 1 FROM words WHERE word = '苹果'").fetchone()
    assert [word.word for word in preview(connection).words] == ["嗯嗯嗯"]


def test_cards_show_where_things_start_and_stand(
    connection: sqlite3.Connection,
) -> None:
    add(connection, TEA, NOW)
    stage(connection, "你好。 | Hello.\n我喜欢你。 | I like you.\n")
    with editing(connection):
        propose_removal(connection, "sentence", "1")
    with editing(connection):
        propose_removal(connection, "word", "喝茶")
    result = preview(connection)
    assert [sentence.starts for sentence in result.sentences] == [
        "eligible",
        "locked",
    ]
    sentence, word = result.changes
    assert sentence.status == statuses(connection)[1]
    assert word.status == "locked"
    assert [(use.action, use.frees) for use in word.uses] == [("remove", True)]
    assert word.waits_on == (sentence.id,)
    assert word.issues == ()
    assert approve(connection, result.version, LATER, "removals")["waiting"] == 0
    assert count(connection, "sentences") == 0
    assert not connection.execute("SELECT 1 FROM words WHERE word = '喝茶'").fetchone()


def test_a_word_without_a_definition_blocks_its_own_tab_too(
    connection: sqlite3.Connection,
) -> None:
    stage(connection, "我犇。 | I x.\n犇\n")
    result = preview(connection)
    assert result.problems["words"] == ("犇 has no definition.",)
    with pytest.raises(ConflictError, match="Fix the problems"):
        approve(connection, result.version, NOW, "words")
    assert not connection.execute("SELECT 1 FROM words WHERE word = '犇'").fetchone()


def test_a_library_edit_after_loading_the_check_screen_is_not_undone(
    connection: sqlite3.Connection,
) -> None:
    add(connection, TEA, NOW)
    with editing(connection):
        change = propose_sentence(connection, 1, words="我 喜欢 喝 茶")
    shown = preview(connection).version
    row = connection.execute("SELECT * FROM sentences WHERE id = 1").fetchone()
    update_sentence(connection, 1, row["version"], row["pinyin"], "I love tea.")
    assert preview(connection).version != shown
    with pytest.raises(ConflictError):
        approve_change(connection, shown, change, LATER)
    approve_change(connection, preview(connection).version, change, LATER)
    assert (
        connection.execute("SELECT translation FROM sentences WHERE id = 1").fetchone()[
            0
        ]
        == "I love tea."
    )
