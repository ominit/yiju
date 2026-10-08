import pytest

from yiju.core.pronunciation import pronunciation


@pytest.mark.parametrize(
    ("text", "reading", "expected"),
    [
        ("学习", "xuéxí", (("学", "xué"), ("习", "xí"))),
        ("学习", "xue2xi2", (("学", "xué"), ("习", "xí"))),
        ("回家", "hui2//jia1", (("回", "huí"), ("家", "jiā"))),
        ("银行", "yínháng", (("银", "yín"), ("行", "háng"))),
        ("你好", "ní hǎo", (("你", "ní"), ("好", "hǎo"))),
        ("西安", "xī'ān", (("西", "xī"), ("安", "ān"))),
        ("女儿", "nv3er2", (("女", "nǚ"), ("儿", "ér"))),
        ("花儿", "huār", (("花", "huā"), ("儿", "r"))),
        ("行", "xíng/háng", (("行", "xíng / háng"),)),
        ("重新", "chóngxīn, zhòngxīn", (("重", "chóng / zhòng"), ("新", "xīn"))),
        ("阿Q", "Ā Q", (("阿", "Ā"), ("Q", ""))),
        ("你，好！", "Nǐ, hǎo!", (("你", "Nǐ"), ("，", ""), ("好", "hǎo"), ("！", ""))),
        ("学习", None, (("学", ""), ("习", ""))),
        ("学习", "xué", (("学习", "xué"),)),
        ("西安", "xiān", (("西", "xi"), ("安", "ān"))),
        ("先", "xī ān", (("先", "xī ān"),)),
        ("学习", "xué xí rubbish", (("学习", "xué xí rubbish"),)),
        (
            "我学习中文。",
            "Wǒ xuéxí Zhōngwén.",
            (
                ("我", "Wǒ"),
                ("学", "xué"),
                ("习", "xí"),
                ("中", "Zhōng"),
                ("文", "wén"),
                ("。", ""),
            ),
        ),
    ],
)
def test_pinyin_aligns_with_characters_and_preserves_imported_readings(
    text: str, reading: str | None, expected: tuple[tuple[str, str], ...]
) -> None:
    assert pronunciation(text, reading) == expected
