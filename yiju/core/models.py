from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExampleSentence:
    chinese: str
    translation: str | None
    pinyin: str | None


@dataclass(frozen=True, slots=True)
class DictionaryReference:
    dictionary_id: str
    entry_id: str


@dataclass(frozen=True, slots=True)
class PlecoEntry:
    source_id: str
    headword_simplified: str | None
    headword_traditional: str | None
    pinyin: str | None
    definition: str | None
    sentences: tuple[ExampleSentence, ...]
    dictionary_references: tuple[DictionaryReference, ...] = ()
