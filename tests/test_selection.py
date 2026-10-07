from yiju.core.selection import analyze


def test_unknown_words_are_unique_but_coverage_counts_occurrences() -> None:
    result = analyze(("我", "我", "去"), frozenset({"去"}), "我")
    assert result.unknown == ("我",)
    assert result.coverage == 1 / 3 or abs(result.coverage - 1 / 3) < 1e-9
    assert result.eligible


def test_three_unknown_words_wait_for_learning() -> None:
    tokens = ("我", "学习", "中文")
    assert not analyze(tokens, frozenset(), "学习").eligible
    assert analyze(tokens, frozenset({"我"}), "学习").eligible


def test_zero_unknown_is_optional_and_target_is_required() -> None:
    assert not analyze(("我",), frozenset({"我"}), "我").eligible
    assert analyze(("我",), frozenset({"我"}), "我", include_zero=True).eligible
    assert not analyze(("我们",), frozenset(), "我").eligible
    assert not analyze((), frozenset(), "我").eligible


def test_ranking_prefers_one_then_two_then_zero_unknown() -> None:
    tokens = ("我", "去", "学习")
    one = analyze(tokens, frozenset({"我", "学习"}), "去")
    two = analyze(tokens, frozenset({"我"}), "去")
    zero = analyze(tokens, frozenset(tokens), "去", include_zero=True)
    assert one.score > two.score > zero.score


def test_sentence_requires_every_word_in_vocabulary_even_if_familiar() -> None:
    result = analyze(
        ("分配", "工作"),
        frozenset({"分配"}),
        "工作",
        vocabulary=frozenset({"工作"}),
    )
    assert not result.eligible
    assert result.missing == ("分配",)
    assert result.reason == "Words missing from vocabulary"
    assert analyze(
        ("分配", "工作"),
        frozenset({"分配"}),
        "工作",
        vocabulary=frozenset({"分配", "工作"}),
    ).eligible


def test_sentences_need_a_known_word_in_the_sentence() -> None:
    for tokens in [("我",), ("我", "我"), ("我", "去")]:
        result = analyze(tokens, frozenset({"学习"}), "我", include_zero=True)
        assert not result.eligible
        assert result.reason == "Needs at least one known word"
    assert analyze(("我", "去"), frozenset({"我"}), "去").eligible
