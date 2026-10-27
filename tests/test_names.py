import asyncio
from collections import Counter
from datetime import UTC, datetime
from random import Random
from typing import TYPE_CHECKING

import pytest
from conftest import add, check_form, csrf, database, set_strength, study_cards

from yiju.core.names import (
    Name,
    english,
    english_spelling,
    letters,
    missing,
    name_pinyin,
    parse_rules,
    pick,
    prompt,
    reading,
    slot_error,
)
from yiju.core.tokenizer import segment, segmentation_error, sentence_pinyin
from yiju.storage.database import ConflictError
from yiju.storage.pending import (
    FormatError,
    approve,
    approve_name,
    edit_name,
    edit_sentence,
    parse_names,
    preview,
    stage,
    stage_names,
    waiting,
)
from yiju.storage.queue import study_queue
from yiju.web import audio

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from flask import Flask
    from flask.testing import FlaskClient

NOW = datetime(2026, 10, 1, 9, tzinfo=UTC)
FANG = Name(1, "王芳", "Wáng Fāng", "Wang Fang", "female")
WEI = Name(2, "李伟", "Lǐ Wěi", "Li Wei", "male")
LIKES = "{A}喜欢{B}。 | {A} likes {B}."
NAMES = "王芳 female\n李伟 male\n"


def approve_all(connection: sqlite3.Connection) -> dict[str, int]:
    return approve(connection, preview(connection).version, NOW)


def test_slots_are_tokens_that_keep_their_place_in_the_pinyin() -> None:
    chinese = "{A}喜欢{B}，{A.他}很好。"
    words = segment(chinese)
    assert words == ("{A}", "喜欢", "{B}", "{A.他}", "很", "好")
    assert sentence_pinyin(chinese, words) == "{A} xǐhuan {B}, tā hěn hǎo."
    assert segmentation_error(chinese, words) is None
    assert segment("{A} 喜欢 {B}。") == ("{A}", "喜欢", "{B}")
    assert (
        segmentation_error(chinese, ("喜欢", "{A}", "{B}", "{A.他}", "很", "好"))
        is not None
    )
    assert segmentation_error("{A}。", ("{A}",)) == "The sentence has no Chinese words."
    assert sentence_pinyin("“{A}”好。", segment("“{A}”好。")) == '"{A}" hǎo.'
    assert sentence_pinyin("{A}是谁？", segment("{A}是谁？")) == "{A} shì shéi?"


def test_slot_errors_explain_what_to_fix() -> None:
    assert slot_error("{A}喜欢{B}。", "{A} likes {B}.", "A!=B") is None
    assert slot_error("{A.她}好。", "{A.They} is well.", "A=female") is None
    assert slot_error("{A}好。", "{B} is well.", "") == (
        "The translation never mentions A."
    )
    assert slot_error("{A}好。", "{A} and {B}.", "") == (
        "The sentence never mentions B."
    )
    assert "not a slot" in (slot_error("{A.x}好。", "{A}.", "") or "")
    assert "not a slot" in (slot_error("{A}好。", "{A.he}.", "") or "")
    assert "brace" in (slot_error("{A}好}。", "{A}.", "") or "")
    assert "not a gender rule" in (slot_error("{A}好。", "{A}.", "A=man") or "")
    assert slot_error("{A}好。", "{A}.", "B=male") == (
        "The gender rules mention B, who is not in the sentence."
    )
    assert slot_error("{A}{B}好。", "{A} {B}.", "A=male B=female A=B") == (
        "The gender rules contradict each other."
    )
    assert slot_error("{A}好。", "{A}.", "", "hǎo.") == (
        "Keep the name slots in the pinyin, in the same order as the sentence."
    )


def test_picks_are_uniform_over_every_valid_set_of_names() -> None:
    pool = [Name(index, f"男{index}", "", "", "male") for index in range(30)]
    pool += [Name(100 + index, f"女{index}", "", "", "female") for index in range(5)]
    random = Random(7)
    genders: Counter[str] = Counter()
    for _ in range(7000):
        picked = pick(["A"], (), pool, random)
        assert picked is not None
        genders[picked["A"].gender] += 1
    assert 0.83 < genders["male"] / 7000 < 0.89
    for _ in range(200):
        different = pick(["A", "B"], parse_rules("A!=B"), pool, random)
        assert different is not None
        assert different["A"].gender != different["B"].gender
        same = pick(["A", "B"], parse_rules("A=female A=B"), pool, random)
        assert same is not None
        assert same["A"] != same["B"]
        assert {same["A"].gender, same["B"].gender} == {"female"}
    assert pick(["A", "B"], (), [FANG], random) is None


def test_missing_names_are_described_by_gender() -> None:
    assert missing(["A"], (), {}) == "1 name"
    assert missing(["A", "B"], (), {"male": 1}) == "2 names"
    assert missing(["A", "B"], parse_rules("A!=B"), {"male": 3}) == (
        "1 male name and 1 female name"
    )
    assert missing(["A", "B"], parse_rules("A=B"), {"male": 1, "female": 1}) == (
        "2 male names or 2 female names"
    )
    assert missing(["A"], parse_rules("A=female"), {"female": 1}) is None


def test_names_fill_the_sentence_pinyin_and_translation() -> None:
    chinese = "{A}喜欢{B}，{A.他}很好。"
    pinyin = sentence_pinyin(chinese, segment(chinese))
    picks = {"A": FANG, "B": WEI}
    parts = reading(chinese, pinyin, "", picks)
    assert "".join(part[0] for part in parts) == "王芳喜欢李伟，她很好。"
    assert [part[2] for part in parts[:2]] == ["name:A", "name:A"]
    assert parts[0][1] == "Wáng"
    assert ("她", "tā", "pronoun:A") in parts
    template = reading(chinese, pinyin)
    assert template[0] == ("A", "", "slot:A")
    assert ("他/她", "tā", "pronoun:A") in template
    assert ("她", "tā", "pronoun:A") in reading(chinese, pinyin, "A=female")
    translation = "{A} likes {B}. {A.They} gave {A.them} {A.their} own {A.themself}."
    assert english(translation, picks) == (
        "Wang Fang likes Li Wei. She gave her her own herself."
    )
    assert prompt("{A} likes {B}.", picks) == [
        ("Wang Fang", "王芳"),
        (" likes ", ""),
        ("Li Wei", "李伟"),
        (".", ""),
    ]


def test_name_pinyin_handles_surnames() -> None:
    assert name_pinyin("王芳") == "Wáng Fāng"
    assert name_pinyin("曾小贤") == "Zēng Xiǎoxián"
    assert name_pinyin("欧阳娜娜").startswith("Ōuyáng ")
    assert english_spelling("Lǚ Xīn") == "Lü Xin"


def test_name_lines_need_characters_and_a_gender() -> None:
    assert parse_names("王芳 female\n# note\n李伟 male\n王芳 female\n") == (
        ("王芳", "female"),
        ("李伟", "male"),
    )
    with pytest.raises(FormatError, match="Line 1") as error:
        parse_names("王芳\nWang female\n")
    assert len(error.value.errors) == 2


def test_slot_sentences_and_names_wait_on_the_same_check(
    connection: sqlite3.Connection,
) -> None:
    add(connection, "我 喜欢")
    stage(connection, f"{LIKES} | A!=B")
    stage_names(connection, NAMES)
    result = preview(connection)
    sentence = result.sentences[0]
    assert [token.text for token in sentence.words] == ["{A}", "喜欢", "{B}"]
    assert [token.slot for token in sentence.words] == [True, False, True]
    assert sentence.rules == "A!=B"
    assert sentence.people
    assert [(name.chinese, name.pinyin, name.english) for name in result.names] == [
        ("王芳", "Wáng Fāng", "Wang Fang"),
        ("李伟", "Lǐ Wěi", "Li Wei"),
    ]
    assert result.adds == {"sentences": 1, "words": 0, "names": 2}
    assert waiting(connection) == 3
    assert approve_all(connection) == {"words": 0, "sentences": 1, "names": 2}
    assert [
        row["word"] for row in connection.execute("SELECT word FROM sentence_words")
    ] == ["喜欢"]
    assert connection.execute("SELECT rules FROM sentences").fetchone()[0] == "A!=B"


def test_bad_rules_and_names_block_approval_until_fixed(
    connection: sqlite3.Connection,
) -> None:
    stage(connection, "{A}好。 | {A} is well. | A=man")
    stage_names(connection, "王芳 female\n")
    result = preview(connection)
    assert not result.ready
    assert "not a gender rule" in result.issues[0]
    with connection:
        edit_sentence(connection, result.sentences[0].id, rules="A=female")
        edit_name(connection, result.names[0].id, english="")
    assert preview(connection).issues == ("王芳 needs its English spelling.",)
    with connection:
        edit_name(connection, result.names[0].id, english="Fang Wang")
    with pytest.raises(ValueError, match="male or female"):
        edit_name(connection, result.names[0].id, gender="other")
    assert preview(connection).ready


def test_one_name_can_be_approved_and_repeats_are_skipped(
    connection: sqlite3.Connection,
) -> None:
    stage_names(connection, NAMES)
    first = preview(connection).names[0]
    approve_name(connection, preview(connection).version, first.id, NOW)
    assert [name.chinese for name in preview(connection).names] == ["李伟"]
    stage_names(connection, "王芳 female\n")
    repeat = next(name for name in preview(connection).names if name.chinese == "王芳")
    assert repeat.duplicate
    with pytest.raises(ConflictError, match="already one of your names"):
        approve_name(connection, preview(connection).version, repeat.id, NOW)
    assert approve_all(connection)["names"] == 1
    assert connection.execute("SELECT count(*) FROM names").fetchone()[0] == 2


def test_sentence_cards_wait_until_the_names_can_fill_them(
    connection: sqlite3.Connection,
) -> None:
    add(connection, f"{LIKES} | A=female B=female", NOW)
    set_strength(connection, "喜欢", now=NOW)
    assert connection.execute("SELECT state FROM sentences").fetchone()[0] == (
        "unlocked"
    )
    assert not [row for row in study_queue(connection, NOW) if row["sentence_id"]]
    stage_names(connection, "王芳 female\n张丽 female\n")
    approve_all(connection)
    assert len([row for row in study_queue(connection, NOW) if row["sentence_id"]]) == 2


def stage_people(app: Flask) -> None:
    with database(app) as db:
        add(db, f"{LIKES} | A!=B", NOW)
        set_strength(db, "喜欢", now=NOW)
        stage_names(db, NAMES)
        approve_all(db)


def test_study_cards_show_random_names_with_matching_audio(
    client: FlaskClient, app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    spoken: list[str] = []

    async def fake_speech(text: str, path: Path) -> None:
        spoken.append(text)
        await asyncio.to_thread(path.write_bytes, b"ID3")

    monkeypatch.setattr(audio, "synthesize", fake_speech)
    stage_people(app)
    card = next(card for card in study_cards(client) if card["kind"] == "sentence")
    assert card["text"] in {"王芳喜欢李伟。", "李伟喜欢王芳。"}
    first = "王芳" if card["text"].startswith("王芳") else "李伟"
    assert card["names"]["A"]["chinese"] == first
    assert card["translation"] == (
        f"{card['names']['A']['english']} likes {card['names']['B']['english']}."
    )
    assert card["translationParts"][0] == [card["names"]["A"]["english"], first]
    assert card["sentence"]["translation"] == "{A} likes {B}."
    assert card["reading"][0][2] == "name:A"
    assert "?names=" in card["audio"]
    assert client.get(card["audio"]).status_code == 200
    assert spoken == [card["text"]]
    bare = card["audio"].split("?")[0]
    assert client.get(bare, headers={"Accept": "application/json"}).status_code == 404
    assert client.get(f"{bare}?names=1,1").status_code == 404


def test_check_screen_edits_and_approves_names(client: FlaskClient, app: Flask) -> None:
    response = client.post(
        "/import/names", data={"csrf": csrf(client), "names": "王芳 female\n"}
    )
    assert response.status_code == 303
    with database(app) as db:
        name = preview(db).names[0]
    form = check_form(client) | {
        "names": f"[{name.id}]",
        f"n{name.id}-pinyin": "Wáng Fāng",
        f"n{name.id}-english": "Fang Wang",
        f"n{name.id}-gender": "female",
        "approve-name": str(name.id),
    }
    assert client.post("/pending", data=form).status_code == 303
    with database(app) as db:
        assert db.execute("SELECT english FROM names").fetchone()[0] == "Fang Wang"
    page = client.get("/library/names").data.decode()
    assert "王芳" in page
    bad = client.post("/import/names", data={"csrf": csrf(client), "names": "Wang"})
    assert bad.status_code == 400


def test_names_can_be_edited_and_removed_in_the_library(
    client: FlaskClient, app: Flask
) -> None:
    stage_people(app)
    token = csrf(client)
    sentence = client.get("/sentences/1", headers={"X-Fragment": "1"}).data.decode()
    assert 'class="slot"' in sentence
    response = client.post(
        "/names/1",
        data={
            "csrf": token,
            "version": "0",
            "pinyin": "Wáng Fāng",
            "english": "Fang",
            "gender": "male",
        },
    )
    assert response.status_code == 303
    assert (
        client.post("/names/1", data={"csrf": token, "version": "0"}).status_code == 409
    )
    assert (
        client.post(
            "/names/1", data={"csrf": token, "version": "1", "remove": "1"}
        ).status_code
        == 303
    )
    detail = client.get("/sentences/1", headers={"X-Fragment": "1"}).data.decode()
    assert "Needs 1 male name and 1 female name" in detail
    with database(app) as db:
        assert not [row for row in study_queue(db, NOW) if row["sentence_id"]]


def test_letters_come_from_names_and_pronouns() -> None:
    assert letters("{B}说{A.他}好") == ("A", "B")
