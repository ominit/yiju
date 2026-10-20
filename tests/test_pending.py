import sqlite3
from datetime import UTC, datetime

import pytest
from conftest import add

from yiju.storage.database import ConflictError
from yiju.storage.pending import (
    FORMAT,
    FormatError,
    approve,
    discard,
    edit_sentence,
    edit_word,
    editing,
    parse,
    preview,
    remove_sentence,
    spoken,
    stage,
    waiting,
)

NOW = datetime(2026, 10, 1, 9, tzinfo=UTC)


def test_lines_are_sentences_with_translations_or_bare_words() -> None:
    parsed = parse(
        "# generated\n\n我很好。\tI am well.\n你好 | Hello\n葡萄 苹果\n你好 | Hello\n"
    )
    assert parsed.sentences == (("我很好。", "I am well."), ("你好", "Hello"))
    assert parsed.words == ("葡萄", "苹果")


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


def test_the_documented_examples_parse() -> None:
    examples = "\n".join(
        line.strip().replace("<TAB>", "\t")
        for line in FORMAT.splitlines()
        if line.startswith("    ")
    )
    parsed = parse(examples)
    assert len(parsed.sentences) == 2
    assert parsed.words == ("葡萄", "苹果")


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
    assert result.adds == {"sentences": 1, "words": 3}
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


def test_excluding_a_word_requires_excluding_its_sentences(
    connection: sqlite3.Connection,
) -> None:
    stage(connection, "我很好。 | I am well.\n你好 | Hello\n")
    with editing(connection):
        edit_word(connection, "我", included=False)
    result = preview(connection)
    assert result.issues == ("Sentence 1: 我 is excluded.",)
    with editing(connection):
        edit_sentence(connection, 1, included=False)
    result = preview(connection)
    assert result.ready
    assert result.adds == {"sentences": 1, "words": 1}
    approve(connection, result.version, NOW)
    assert [row[0] for row in connection.execute("SELECT word FROM words")] == ["你好"]


def test_sentences_already_in_the_library_are_not_added_twice(
    connection: sqlite3.Connection,
) -> None:
    add(connection, "我很好。 | I am well.\n", NOW)
    stage(connection, "我 很 好。 | I am fine.\n")
    result = preview(connection)
    assert result.sentences[0].duplicate
    assert not result.sentences[0].included
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
    assert approve(connection, result.version, NOW) == {"words": 4, "sentences": 2}
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
    assert de.pinyin == "dé"
    assert next(token for token in tokens if token.text == "他").choices == ()
    assert spoken("Tā, pǎo “dé” hěn kuài.", 5) == ["Tā", "pǎo", "dé", "hěn", "kuài"]
    assert spoken("Tā pǎodé hěn kuài.", 5) == [None] * 5
