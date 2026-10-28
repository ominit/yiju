from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from conftest import add

from yiju.storage.database import ConflictError
from yiju.storage.pending import (
    FORMAT,
    NAME_FORMAT,
    FormatError,
    approve,
    discard,
    edit_sentence,
    edit_word,
    editing,
    parse,
    preview,
    remove_sentence,
    remove_word,
    spoken,
    stage,
    stage_names,
    waiting,
)

if TYPE_CHECKING:
    import sqlite3

NOW = datetime(2026, 10, 1, 9, tzinfo=UTC)


def test_lines_are_sentences_with_translations_or_bare_words() -> None:
    parsed = parse(
        "# generated\n\n我很好。\tI am well.\n你好 | Hello\n葡萄 苹果\n你好 | Hello\n"
    )
    assert parsed.sentences == (
        ("我很好。", "I am well.", ""),
        ("你好", "Hello", ""),
    )
    assert parsed.words == ("葡萄", "苹果")


def test_spaces_next_to_punctuation_and_slots_are_dropped(
    connection: sqlite3.Connection,
) -> None:
    stage(connection, "我 很 好 。 | I am well.\n{A} 有 iPhone 15 。 | {A} has one.\n")
    assert [sentence.chinese for sentence in preview(connection).sentences] == [
        "我很好。",
        "{A}有iPhone 15。",
    ]


def test_format_errors_name_every_bad_line() -> None:
    with pytest.raises(FormatError) as error:
        parse("hello | there\n我很好。\n我 | \n")
    assert error.value.errors == [
        "Line 1: the part before the separator has no Chinese.",
        (
            'Line 2: expected a sentence with a translation after a tab or " | ", '
            "or Chinese words separated by spaces."
        ),
        "Line 3: the translation is empty.",
    ]
    with pytest.raises(FormatError, match="Nothing"):
        parse("# only a comment\n")


def test_the_agent_guide_quotes_both_formats() -> None:
    guide = (Path(__file__).parents[1] / "docs" / "agent-guide.md").read_text()
    assert f"```text\n{FORMAT}```" in guide
    assert f"```text\n{NAME_FORMAT}```" in guide


def test_preview_shows_new_words_with_definitions_and_generated_pinyin(
    connection: sqlite3.Connection,
) -> None:
    add(connection, "我很好。 | I am well.\n", NOW)
    stage(connection, "我很喜欢葡萄。 | I like grapes.\n苹果\n")
    result = preview(connection)
    (sentence,) = result.sentences
    assert [token.text for token in sentence.words] == ["我", "很", "喜欢", "葡萄"]
    assert [token.new for token in sentence.words] == [False, False, True, True]
    assert sentence.pinyin == "wǒ hěn xǐhuan pútao."
    assert [word.word for word in result.words] == ["喜欢", "葡萄", "苹果"]
    apple = result.words[2]
    assert apple.requested
    assert apple.sentences == []
    assert apple.readings[0]["pinyin"] == "píngguǒ"
    assert result.adds["sentences"] == 1
    assert result.ready
    assert connection.execute("SELECT count(*) FROM words").fetchone()[0] == 3


def test_words_missing_from_the_dictionary_block_approval_until_defined(
    connection: sqlite3.Connection,
) -> None:
    stage(connection, "嗯嗯嗯 好 | Mm, good.\n")
    result = preview(connection)
    assert not result.ready
    assert any("has no definition" in issue for issue in result.issues)
    missing = [word for word in result.words if not word.found]
    with editing(connection):
        for word in missing:
            edit_word(connection, word.word, meanings="mm")
    result = preview(connection)
    assert result.ready
    assert all(word.manual for word in result.words if not word.found)
    approve(connection, result.version, NOW)
    sources = dict(connection.execute("SELECT word, source FROM words").fetchall())
    assert set(sources.values()) == {"cedict", "manual"}


def test_editing_the_split_regenerates_pinyin_and_changes_the_new_words(
    connection: sqlite3.Connection,
) -> None:
    stage(connection, "他跑得很快。 | He runs fast.\n")
    with editing(connection):
        edit_sentence(connection, 1, words="他 跑得 很快")
    result = preview(connection)
    assert [token.text for token in result.sentences[0].words] == ["他", "跑得", "很快"]
    assert not result.ready
    with editing(connection):
        edit_sentence(
            connection, 1, words="他 跑 得 很 快", pinyin="Tā pǎo de hěn kuài."
        )
    result = preview(connection)
    assert result.sentences[0].pinyin == "Tā pǎo de hěn kuài."
    assert result.ready


def test_a_bad_split_is_reported(connection: sqlite3.Connection) -> None:
    stage(connection, "我很好。 | I am well.\n")
    with editing(connection):
        edit_sentence(connection, 1, words="我 好")
    assert "spell out" in preview(connection).issues[0]


def test_pinyin_needs_one_group_per_word(connection: sqlite3.Connection) -> None:
    stage(connection, "他跑得很快。 | He runs fast.\n我有iPhone。 | I have one.\n")
    assert preview(connection).ready
    with editing(connection):
        edit_sentence(connection, 1, pinyin="tā pǎo de hěn kuài le.")
    assert preview(connection).issues == (
        (
            "Sentence 1: The pinyin needs one group per word or slot, in order. "
            "This sentence has 5 and the pinyin has 6."
        ),
    )


def test_waiting_sentences_say_which_names_they_need(
    connection: sqlite3.Connection,
) -> None:
    stage(
        connection,
        "{A}喜欢{B}。 | {A} likes {B}.\n{A}是哥哥。 | {A} is older. | A=male\n",
    )

    def needs_names() -> list[str | None]:
        return [sentence.needs_names for sentence in preview(connection).sentences]

    assert needs_names() == ["2 names", "1 male name"]
    stage_names(connection, "王芳 female\n")
    assert needs_names() == ["2 names", "1 male name"]
    stage_names(connection, "李伟 male\n")
    assert needs_names() == [None, None]


def test_only_words_waiting_on_their_own_can_be_removed(
    connection: sqlite3.Connection,
) -> None:
    stage(connection, "我很好。 | I am well.\n你好 我\n")
    with editing(connection):
        remove_word(connection, "你好")
    with pytest.raises(ValueError, match="goes with that sentence"):
        remove_word(connection, "我")
    with pytest.raises(LookupError):
        remove_word(connection, "很")
    result = preview(connection)
    assert "你好" not in {word.word for word in result.words}
    assert result.adds == {"sentences": 1, "words": 3, "names": 0, "changes": 0}


def test_sentences_already_in_the_library_are_not_added_twice(
    connection: sqlite3.Connection,
) -> None:
    add(connection, "我很好。 | I am well.\n", NOW)
    stage(connection, "我 很 好。 | I am fine.\n")
    result = preview(connection)
    assert result.sentences[0].duplicate
    assert result.sentences[0].issues == ()
    assert result.adds["sentences"] == 0
    assert not result.ready


def test_later_additions_join_the_same_check_and_need_another_look(
    connection: sqlite3.Connection,
) -> None:
    stage(connection, "你好 | Hello\n")
    checked = preview(connection).version
    stage(connection, "我很好。 | I am well.\n你好 | Hi\n")
    with pytest.raises(ConflictError, match="changed"):
        approve(connection, checked, NOW)
    with pytest.raises(ConflictError, match="changed"):
        discard(connection, checked)
    result = preview(connection)
    assert [sentence.translation for sentence in result.sentences] == [
        "Hello",
        "I am well.",
    ]
    assert approve(connection, result.version, NOW) == {
        "words": 4,
        "sentences": 2,
        "names": 0,
        "changes": 0,
        "waiting": 0,
    }
    assert preview(connection).empty
    assert waiting(connection) == 0


def test_waiting_items_can_be_counted_trimmed_and_discarded(
    connection: sqlite3.Connection,
) -> None:
    stage(connection, "你好 | Hello\n我很好。 | I am well.\n苹果\n")
    assert waiting(connection) == 3
    with editing(connection):
        remove_sentence(connection, 1)
    assert [sentence.id for sentence in preview(connection).sentences] == [2]
    with pytest.raises(LookupError):
        remove_sentence(connection, 1)
    discard(connection, preview(connection).version)
    assert preview(connection).empty
    assert connection.execute("SELECT count(*) FROM words").fetchone()[0] == 0


def test_words_with_several_readings_offer_them_with_the_one_in_use(
    connection: sqlite3.Connection,
) -> None:
    stage(connection, "他跑得很快。 | He runs fast.\n")
    tokens = preview(connection).sentences[0].words
    de = next(token for token in tokens if token.text == "得")
    assert [choice.pinyin for choice in de.choices] == ["dé", "de", "děi", "dè"]
    assert de.choices[0].meaning == "to obtain"
    assert de.choices[0].meanings[0] == "to obtain"
    assert len(de.choices[0].meanings) > 1
    assert preview(connection).to_json()["sentences"][0]["words"][2]["choices"][0] == {
        "pinyin": "dé",
        "meaning": "to obtain",
    }
    assert de.pinyin == "dé"
    assert de.meanings is not None
    assert de.meanings[0] == "to obtain"
    with editing(connection):
        edit_sentence(connection, 1, pinyin="tā pǎo dá hěn kuài.")
    tokens = preview(connection).sentences[0].words
    assert next(token for token in tokens if token.text == "得").meanings is None
    assert next(token for token in tokens if token.text == "他").choices == ()
    assert spoken("Tā, pǎo “dé” hěn kuài.", "他跑得很快。", 5) == [
        "Tā",
        "pǎo",
        "dé",
        "hěn",
        "kuài",
    ]
    assert spoken("Tā pǎodé hěn kuài.", "他跑得很快。", 5) == [None] * 5
