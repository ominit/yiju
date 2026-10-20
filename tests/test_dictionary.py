import gzip
from pathlib import Path

import pytest
from conftest import DICTIONARY

from yiju.core import dictionary
from yiju.core.definitions import prompt_senses, summary
from yiju.core.dictionary import (
    Sense,
    contains,
    decode,
    encode,
    lookup,
    manual,
    pinyin,
    published,
    readings_pinyin,
    syllables,
)
from yiju.web import create_app


def test_senses_split_on_slashes_and_surface_leading_notes() -> None:
    (reading,) = lookup("爸爸")
    assert reading.pinyin == "bàba"
    assert reading.senses == (Sense("father; dad", ("coll.",)),)
    assert reading.classifiers == ("位 (wèi)",)


def test_readings_stay_separate_and_reference_only_readings_come_last() -> None:
    readings = lookup("得")
    assert [reading.pinyin for reading in readings] == ["dé", "de", "děi", "dè"]
    assert readings[-1].senses == (Sense("used in 得瑟 (dèse)"),)
    assert Sense("to have to") in readings[2].senses


def test_parenthesized_senses_are_usage_notes() -> None:
    le = lookup("了")[0]
    assert le.pinyin == "le"
    assert le.senses[0] == Sense("completed action marker", usage=True)


def test_simplified_and_traditional_columns_are_both_searched() -> None:
    assert lookup("苹果")[0].senses == lookup("蘋果")[0].senses
    assert lookup("苹果")[0].traditional == ("蘋果",)
    assert contains("學習")


def test_proper_nouns_and_variant_entries_yield_to_common_readings() -> None:
    assert [reading.senses[0].text for reading in lookup("苹果")] == ["apple"]
    assert lookup("是")[0].senses[0].text == "to be (followed by substantives only)"
    assert all(
        "variant of 了" not in sense.text
        for reading in lookup("了")
        for sense in reading.senses
    )
    assert lookup("中国")[0].pinyin == "Zhōngguó"


def test_unknown_words_have_no_readings() -> None:
    assert lookup("葡萄很甜") == ()


@pytest.mark.parametrize(
    ("numbered", "expected"),
    [
        ("ping2 guo3", "píngguǒ"),
        ("nu:3 er2", "nǚ'ér"),
        ("Xi1 an1", "Xī'ān"),
        ("xi3 huan5", "xǐhuan"),
        ("yi1 dian3 r5", "yīdiǎnr"),
    ],
)
def test_numbered_pinyin_becomes_tone_marks(numbered: str, expected: str) -> None:
    assert pinyin(numbered) == expected


def test_single_dictionary_reading_supplies_syllables() -> None:
    assert syllables("葡萄") == ("pú", "tao")
    assert syllables("行") is None


def test_readings_round_trip_through_json() -> None:
    readings = lookup("了")
    assert decode(encode(readings)) == readings
    assert readings_pinyin(readings) == "le, liǎo"


def test_manual_definitions_take_one_meaning_per_line() -> None:
    (reading,) = manual("ǹg", "interjection of surprise\n\n  hm  \n")
    assert reading.pinyin == "ǹg"
    assert [sense.text for sense in reading.senses] == [
        "interjection of surprise",
        "hm",
    ]
    assert manual("a", "  ") == ()


def test_prompts_hide_chinese_and_skip_cross_references() -> None:
    prompts = prompt_senses(encode(lookup("得")))
    assert "to obtain" in prompts
    assert not any("得" in prompt or "dè" in prompt for prompt in prompts)
    assert all(not prompt.startswith("used in") for prompt in prompts)
    assert summary(encode(lookup("得"))) == "to obtain; to get; to gain"


def test_bundled_dictionary_reports_its_date() -> None:
    assert len(published()) == len("2026-10-17")


@pytest.fixture
def download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[str]]:
    monkeypatch.setattr(dictionary, "MIN_ENTRIES", 100)
    calls: list[str] = []

    async def fetch(url: str = dictionary.URL) -> bytes:
        calls.append(url)
        return gzip.compress(DICTIONARY.read_bytes())

    monkeypatch.setattr(dictionary, "fetch", fetch)
    path = tmp_path / dictionary.FILENAME
    path.unlink()
    dictionary.use(path)
    return path, calls


def test_the_dictionary_downloads_once_on_first_use(
    download: tuple[Path, list[str]],
) -> None:
    path, calls = download
    assert dictionary.published() == ""
    assert lookup("苹果")[0].pinyin == "píngguǒ"
    assert path.is_file()
    assert lookup("我")
    assert calls == [dictionary.URL]
    assert dictionary.published() == "2026-10-17"
    assert dictionary.update() == "2026-10-17"
    assert len(calls) == 2
    assert [file.name for file in path.parent.iterdir()] == [path.name]


def test_a_failed_or_suspicious_download_is_not_saved(
    download: tuple[Path, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _calls = download
    monkeypatch.setattr(dictionary, "MIN_ENTRIES", 1_000_000)
    with pytest.raises(dictionary.DictionaryUnavailableError, match="look like"):
        lookup("我")

    async def offline(_url: str = dictionary.URL) -> bytes:
        message = "Network is unreachable"
        raise OSError(message)

    monkeypatch.setattr(dictionary, "fetch", offline)
    with pytest.raises(dictionary.DictionaryUnavailableError, match="unreachable"):
        lookup("我")
    assert list(path.parent.iterdir()) == []


@pytest.mark.usefixtures("download")
def test_pages_explain_a_missing_dictionary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def offline(_url: str = dictionary.URL) -> bytes:
        message = "Network is unreachable"
        raise OSError(message)

    monkeypatch.setattr(dictionary, "fetch", offline)
    app = create_app(tmp_path / "yiju.sqlite3")
    client = app.test_client()
    client.get("/")
    with client.session_transaction() as session:
        token = session["csrf"]
    response = client.post("/import", data={"csrf": token, "text": "你好 | Hello"})
    assert response.status_code == 503
    assert "Could not download CC-CEDICT" in response.get_data(as_text=True)
    assert client.get("/about").status_code == 200
