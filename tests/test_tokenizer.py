import pytest

from yiju.core.tokenizer import (
    segment,
    segmentation_error,
    sentence_pinyin,
    word_pinyin,
)


def test_jieba_tokens_outside_the_dictionary_split_into_dictionary_words() -> None:
    assert segment("他跑得很快！") == ("他", "跑", "得", "很", "快")
    assert segment("我很喜欢葡萄。") == ("我", "很", "喜欢", "葡萄")


def test_spaces_in_the_sentence_replace_automatic_segmentation() -> None:
    assert segment("我 打 电话。") == ("我", "打", "电话")
    assert segment("我打 电 话") == ("我打", "电", "话")


def test_library_words_guide_segmentation() -> None:
    assert segment("酸苹果") == ("酸", "苹果")
    assert segment("酸苹果", ("酸苹果",)) == ("酸苹果",)


def test_punctuation_and_latin_text_are_not_words() -> None:
    assert segment("我有3个iPhone手机") == ("我", "有", "个", "手机")


@pytest.mark.parametrize(
    ("words", "error"),
    [
        (("我", "很", "好"), None),
        (("我很", "好"), None),
        ((), "no Chinese words"),
        (("我", "很"), "spell out"),
        (("我", "很好a"), "only Chinese"),
    ],
)
def test_segmentation_must_cover_the_sentence(
    words: tuple[str, ...], error: str | None
) -> None:
    result = segmentation_error("我很好。", words)
    assert result is None if error is None else error in str(result)


def test_sentence_pinyin_groups_words_and_keeps_punctuation() -> None:
    assert sentence_pinyin("我很喜欢葡萄。", ("我", "很", "喜欢", "葡萄")) == (
        "wǒ hěn xǐhuan pútao."
    )
    assert sentence_pinyin("你还好吗？", ("你", "还好", "吗")) == "nǐ háihǎo ma?"
    assert sentence_pinyin("「你好」，他说。", ("你好", "他", "说")) == (
        '"nǐhǎo", tā shuō.'
    )
    assert sentence_pinyin("我有3个iPhone", ("我", "有", "个")) == "wǒ yǒu 3 gè iPhone"
    assert sentence_pinyin("中国很大。", ("中国", "很", "大")) == "Zhōngguó hěn dà."


def test_words_without_entries_still_get_pinyin() -> None:
    assert word_pinyin("嗯嗯") != ""
