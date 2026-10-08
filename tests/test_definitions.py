from yiju.core.definitions import parse_definition, recall_prompt


def test_separates_numbered_senses_parts_of_speech_and_examples() -> None:
    definition = parse_definition(
        "number 1 one 一匹马 yī pǐ mǎ a horse 2 first 一级 yījí of first rate"
        "\\nadverb once; as soon as 他回来。 Tā huílai. He comes back."
    )
    assert [sense.meaning for sense in definition.senses] == [
        "one",
        "first",
        "once; as soon as",
    ]
    assert [sense.part_of_speech for sense in definition.senses] == [
        "number",
        "number",
        "adverb",
    ]
    assert definition.example_count == 3
    assert definition.senses[0].examples[0].chinese == "一匹马"
    assert definition.senses[0].examples[0].pinyin == "yī pǐ mǎ"
    assert definition.senses[0].examples[0].translation == "a horse"


def test_preserves_usage_notes_subsenses_and_cross_references() -> None:
    definition = parse_definition(
        "number 1 [brief action] a [between repeated verbs] "
        "等一等 děng yī děng wait a bit "
        "b [before a measure] 瞧一眼 qiáo yī yǎn take a look\n"
        "VARIANT OF 得 dé 3 verb fit; suit"
    )
    assert [sense.meaning for sense in definition.senses] == [
        "[brief action]",
        "a [between repeated verbs]",
        "b [before a measure]",
        "VARIANT OF 得 dé 3 verb fit; suit",
    ]
    assert definition.example_count == 2


def test_plain_or_empty_definition_does_not_invent_examples() -> None:
    assert parse_definition("").senses == ()
    definition = parse_definition("verb go 我去。 I am going.")
    assert definition.senses[0].meaning == "go"
    assert definition.example_count == 0


def test_numbers_in_examples_do_not_create_senses() -> None:
    definition = parse_definition("noun bus 一路车 yī lù chē No. 1 bus")
    assert len(definition.senses) == 1
    assert definition.senses[0].meaning == "bus"


def test_multiple_dictionary_entries_are_kept_separate() -> None:
    definition = parse_definition("verb work; operate\n\nnoun work; job")
    assert [sense.meaning for sense in definition.senses] == [
        "work; operate",
        "work; job",
    ]


def test_chinese_in_usage_notes_is_preserved() -> None:
    definition = parse_definition("noun note in gongchepu (工尺谱), corresponding to 7")
    assert (
        definition.senses[0].meaning == "note in gongchepu (工尺谱), corresponding to 7"
    )


def test_recall_prompt_includes_all_meanings_without_character_hints() -> None:
    definition = parse_definition(
        "verb 1 work 2 operate\nnoun work; job\n"
        "noun see 工作\nnoun note (工尺谱)\nverb work"
    )
    assert recall_prompt(definition.to_json()) == "work\noperate\nwork; job\nnote"
