import pytest

from yiju.core.normalization import normalize_text
from yiju.core.text import display_pinyin


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("  你好\n", "你好"),
        ("nu\u0308\u030c", "nǚ"),
        ("學習", "學習"),
        ("学习", "学习"),
        ("Ａ１", "Ａ１"),
        ("你 好！", "你 好！"),
        ("\u3000\t\n", ""),
    ],
)
def test_normalize_text(text: str, expected: str) -> None:
    result = normalize_text(text)

    assert result == expected
    assert normalize_text(result) == result


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("hui2//jia1", "huí jiā"),
        ("huí //jiā", "huí jiā"),
        ("chi1//fan4", "chī fàn"),
        ("xue2xi2", "xué xí"),
        ("he2/he4", "hé /hè"),
        ("  huí  jiā  ", "huí jiā"),
        (None, ""),
    ],
)
def test_pinyin_hides_separable_verb_markers(text: str | None, expected: str) -> None:
    assert display_pinyin(text) == expected
    assert display_pinyin(expected) == expected
