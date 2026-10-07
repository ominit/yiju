from xml.sax.saxutils import escape

import pytest

from yiju.core.models import DictionaryReference
from yiju.core.pleco_parser import PlecoParseError, parse_pleco_xml


def export(*cards: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<plecoflash formatversion="2"><categories/><cards>'
        + "".join(cards)
        + "</cards></plecoflash>"
    ).encode("utf-8")


def card(definition: str = "to learn", reference: str = "") -> str:
    return (
        '<card language="chinese"><entry>'
        '<headword charset="sc">学习</headword>'
        '<headword charset="tc">學習</headword>'
        '<pron type="hypy" tones="numbers">xue2xi2</pron>'
        f"<defn>{escape(definition)}</defn></entry>{reference}</card>"
    )


def test_preserves_source_text_without_inventing_sentences() -> None:
    definition = "  learn & study\n学习中文。 Xuéxí Zhōngwén. Study Chinese. link  "
    result = parse_pleco_xml(
        export(card(definition, '<dictref dictid="TEST" entryid="123"/>')),
    )

    assert result.total_cards == 1
    assert result.issues == ()
    (entry,) = result.entries
    assert entry.headword_simplified == "学习"
    assert entry.headword_traditional == "學習"
    assert entry.pinyin == "xue2xi2"
    assert entry.definition == definition
    assert entry.dictionary_references == (DictionaryReference("TEST", "123"),)
    assert entry.sentences == ()


def test_missing_optional_fields() -> None:
    result = parse_pleco_xml(
        export(
            '<card language="chinese"><entry>'
            '<headword charset="sc">我</headword></entry></card>'
        ),
    )
    (entry,) = result.entries

    assert entry.headword_traditional is None
    assert entry.pinyin is None
    assert entry.definition is None
    assert entry.dictionary_references == ()


@pytest.mark.parametrize(
    "invalid_card",
    [
        '<card language="english"><entry/></card>',
        '<card language="chinese"/>',
        '<card language="chinese"><entry/><entry/></card>',
        (
            '<card language="chinese"><entry>'
            '<headword charset="sc"> </headword></entry></card>'
        ),
        card().replace("</entry>", '<headword charset="sc">重复</headword></entry>'),
        card().replace("to learn", "<span>to learn</span>"),
        card(reference='<dictref dictid="TEST"/>'),
    ],
)
def test_reports_invalid_cards_and_continues(invalid_card: str) -> None:
    result = parse_pleco_xml(export(card(), invalid_card, card("study")))

    assert result.total_cards == 3
    assert len(result.entries) == 2
    (issue,) = result.issues
    assert issue.card_number == 2
    assert issue.message


@pytest.mark.parametrize(
    "data",
    [
        b"not XML",
        b"<plecoflash",
        b'<other formatversion="2"><cards/></other>',
        b'<plecoflash formatversion="3"><cards/></plecoflash>',
        b'<plecoflash formatversion="2"/>',
        b'<plecoflash formatversion="2"><cards/><cards/></plecoflash>',
        b'<plecoflash formatversion="2"><cards><unexpected/></cards></plecoflash>',
        (
            b'<!DOCTYPE plecoflash [<!ENTITY x "test">]>'
            b'<plecoflash formatversion="2"><cards/></plecoflash>'
        ),
        (
            b'<!DOCTYPE plecoflash SYSTEM "file:///etc/passwd">'
            b'<plecoflash formatversion="2"><cards/></plecoflash>'
        ),
    ],
)
def test_rejects_invalid_documents(data: bytes) -> None:
    with pytest.raises(PlecoParseError):
        parse_pleco_xml(data)


def test_empty_export() -> None:
    result = parse_pleco_xml(export())

    assert result.total_cards == 0
    assert result.entries == ()
    assert result.issues == ()


def test_identity_is_independent_of_export_order_and_metadata() -> None:
    first = card("first sense")
    second = card("second sense")
    original = parse_pleco_xml(export(first, second))
    reordered = parse_pleco_xml(
        export(second, first).replace(b"<plecoflash ", b'<plecoflash created="123" '),
    )

    assert original.entries == tuple(reversed(reordered.entries))
    assert len({entry.source_id for entry in original.entries}) == 2


def test_dictionary_identity_survives_definition_edits() -> None:
    reference = '<dictref dictid="TEST" entryid="123"/>'
    (before,) = parse_pleco_xml(export(card("before", reference))).entries
    (after,) = parse_pleco_xml(export(card("after", reference))).entries
    (different,) = parse_pleco_xml(
        export(card("before", '<dictref dictid="TEST" entryid="456"/>')),
    ).entries

    assert before.source_id == after.source_id
    assert before.source_id != different.source_id


def test_duplicate_entries_remain_available_for_import_summary() -> None:
    result = parse_pleco_xml(export(card(), card()))

    assert result.total_cards == 2
    first, second = result.entries
    assert first.source_id == second.source_id
