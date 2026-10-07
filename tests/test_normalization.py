import pytest

from yiju.core.normalization import normalize_text


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
