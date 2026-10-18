import pytest

from yiju.core.extraction import extract_examples
from yiju.core.text import clean_definition, display_pinyin
from yiju.core.tokenizer import tokenize


@pytest.mark.parametrize(
    ("text", "chinese", "pinyin", "translation"),
    [
        ("go 我去。 Wǒ qù. I am going.", "我去。", "Wǒ qù.", "I am going."),
        (
            "我不跟他一起走。 Wǒ bù gēn tā yīqǐ zǒu. I won't go with him.",
            "我不跟他一起走。",
            "Wǒ bù gēn tā yīqǐ zǒu.",
            "I won't go with him.",
        ),
        (
            "你们是一起的吗？ Nǐmen shì yīqǐ de ma? Are you together?",
            "你们是一起的吗？",
            "Nǐmen shì yīqǐ de ma?",
            "Are you together?",
        ),
        (
            "西安很好。 Xī'ān hěn hǎo. Xi'an is nice.",
            "西安很好。",
            "Xī'ān hěn hǎo.",
            "Xi'an is nice.",
        ),
    ],
)
def test_extracts_complete_reading(
    text: str, chinese: str, pinyin: str, translation: str
) -> None:
    (example,) = extract_examples(text)
    assert example.chinese == chinese
    assert example.pinyin == pinyin
    assert example.translation == translation


def test_multiple_examples_and_sense_boundaries() -> None:
    result = extract_examples(
        "我去。 Wǒ qù. I am going. 2 depart 你去。 Nǐ qù. You go."
    )
    assert [example.translation for example in result] == ["I am going.", "You go."]


@pytest.mark.parametrize(
    "definition", ["to go", "see 去", "我去。 I am going.", "我去。 Wǒ qù."]
)
def test_ambiguous_or_missing_reading_is_not_invented(definition: str) -> None:
    assert extract_examples(definition) == ()


def test_cleans_cross_references_without_losing_the_word() -> None:
    assert (
        clean_definition("See \uead1\ueada123\uead8你好\uead9ni3hao3\uead0你好\uead2")
        == "See 你好"
    )


def test_numbered_pinyin() -> None:
    assert display_pinyin("xue2xi2") == "xué xí"


def test_tokenizer_retains_numbers_latin_and_repeated_words() -> None:
    tokens = tokenize("我，我学习 Python 3！", ("学习",))
    assert tokens == ("我", "我", "学习", "Python", "3")


@pytest.mark.parametrize(
    ("sentence", "word", "tokens"),
    [
        ("我们一起去看电影吧", "电影", ("我们", "一起", "去", "看", "电影", "吧")),
        ("这个怎么弄", "弄", ("这个", "怎么", "弄")),
    ],
)
def test_headword_is_not_absorbed_into_a_neighbouring_token(
    sentence: str, word: str, tokens: tuple[str, ...]
) -> None:
    assert tokenize(sentence, (word,)) == tokens


def test_headword_does_not_over_split_a_longer_word() -> None:
    assert tokenize("水果很甜", ("水",)) == ("水果", "很甜")
    assert tokenize("他去了水果店", ("水",)) == ("他", "去", "了", "水果店")


def test_does_not_consume_english_he_to_fill_an_optional_chinese_word() -> None:
    assert extract_examples("他第一(名)。 Tā dìyī. He came first.") == ()
