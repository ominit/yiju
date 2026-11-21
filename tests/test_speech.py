import re

import pytest

from yiju.core.speech import phonemes


def tones(text: str, pinyin: str) -> str:
    return re.sub(r"\D", "", phonemes(text, pinyin))


@pytest.mark.parametrize(
    ("text", "pinyin", "expected"),
    [
        ("你好", "nǐ hǎo", "23"),
        ("我很好。", "wǒ hěn hǎo.", "223"),
        ("我很喜欢葡萄。", "wǒ hěn xǐhuan pútao.", "223525"),
        ("你好，你好。", "nǐ hǎo, nǐ hǎo.", "2323"),
        ("不是", "bùshì", "24"),
        ("不知道", "bù zhīdào", "414"),
        ("对不起", "duìbuqǐ", "453"),
        ("一起", "yīqǐ", "43"),
        ("一下", "yīxià", "24"),
        ("一本书", "yī běn shū", "431"),
        ("统一", "tǒngyī", "31"),
        ("第一天", "dìyī tiān", "411"),
    ],
)
def test_sandhi_follows_the_stored_tones(text: str, pinyin: str, expected: str) -> None:
    assert tones(text, pinyin) == expected


def test_neutral_and_dictionary_tones_are_kept() -> None:
    assert tones("早上", "zǎoshang") == "35"
    assert tones("兄弟", "xiōngdì") == "14"
    assert tones("兄弟", "xiōngdi") == "15"


def test_words_and_punctuation_are_marked() -> None:
    assert phonemes("你好，我是王伟。", "nǐ hǎo, wǒ shì Wáng Wěi.") == (
        "ㄋㄧ2/ㄏㄠ3, 我3/ㄕ十4/王2/为3."
    )
    assert phonemes("哪儿", "nǎr") == "ㄋㄚR3"
    assert phonemes("这本书", "zhè běn shū") == "ㄓㄜ4/ㄅㄣ3/ㄕㄨ1"


@pytest.mark.parametrize(
    ("text", "pinyin"),
    [("T恤", "T xù"), ("你好", "nǐ"), ("你好", ""), ("。", "")],
)
def test_text_without_a_full_reading_is_refused(text: str, pinyin: str) -> None:
    with pytest.raises(ValueError, match=r"Cannot|pinyin|nothing"):
        phonemes(text, pinyin)
