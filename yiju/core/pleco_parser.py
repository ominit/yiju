import hashlib
import json
from dataclasses import dataclass
from xml.etree.ElementTree import Element, ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring

from yiju.core.models import DictionaryReference, PlecoEntry
from yiju.core.normalization import normalize_text


class PlecoParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ParseIssue:
    card_number: int
    message: str


@dataclass(frozen=True, slots=True)
class ParseResult:
    entries: tuple[PlecoEntry, ...]
    issues: tuple[ParseIssue, ...]

    @property
    def total_cards(self) -> int:
        return len(self.entries) + len(self.issues)


def parse_pleco_xml(data: bytes | str) -> ParseResult:
    try:
        root = fromstring(data, forbid_dtd=True)
    except (ParseError, DefusedXmlException, LookupError, ValueError) as error:
        message = f"Invalid Pleco XML: {error}"
        raise PlecoParseError(message) from error

    if root.tag != "plecoflash" or root.get("formatversion") != "2":
        message = "Expected a Pleco flashcard XML export with formatversion 2."
        raise PlecoParseError(message)
    containers = root.findall("cards")
    if len(containers) != 1:
        message = "Expected exactly one cards element."
        raise PlecoParseError(message)
    (container,) = containers
    if any(child.tag != "card" for child in container):
        message = "Unexpected element in cards."
        raise PlecoParseError(message)

    entries: list[PlecoEntry] = []
    issues: list[ParseIssue] = []
    for number, card in enumerate(container, start=1):
        try:
            entries.append(_parse_card(card))
        except PlecoParseError as error:
            issues.append(ParseIssue(number, str(error)))
    return ParseResult(tuple(entries), tuple(issues))


def _optional_text(entry: Element, path: str) -> str | None:
    elements = entry.findall(path)
    if len(elements) > 1:
        message = f"Multiple values for {path}."
        raise PlecoParseError(message)
    if not elements:
        return None
    (element,) = elements
    if len(element):
        message = f"Nested markup is unsupported in {path}."
        raise PlecoParseError(message)
    text = element.text
    return text if text is not None and text.strip() else None


def _references(card: Element) -> tuple[DictionaryReference, ...]:
    references: list[DictionaryReference] = []
    for reference in card.findall("dictref"):
        dictionary_id = reference.get("dictid", "").strip()
        entry_id = reference.get("entryid", "").strip()
        if not dictionary_id or not entry_id:
            message = "Dictionary reference is missing dictid or entryid."
            raise PlecoParseError(message)
        references.append(DictionaryReference(dictionary_id, entry_id))
    return tuple(references)


def _source_id(
    simplified: str | None,
    traditional: str | None,
    pinyin: str | None,
    definition: str | None,
    references: tuple[DictionaryReference, ...],
) -> str:
    identity = {
        "headwords": [normalize_text(text or "") for text in (simplified, traditional)],
        "pinyin": normalize_text(pinyin or ""),
        "references": sorted(
            {(reference.dictionary_id, reference.entry_id) for reference in references},
        ),
        "definition": normalize_text(definition or "") if not references else None,
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return "pleco-xml-v2:" + hashlib.sha256(encoded).hexdigest()


def _parse_card(card: Element) -> PlecoEntry:
    if card.get("language") != "chinese":
        message = "Only Chinese cards are supported."
        raise PlecoParseError(message)
    entries = card.findall("entry")
    if len(entries) != 1:
        message = "Expected exactly one entry in the card."
        raise PlecoParseError(message)
    (entry,) = entries
    simplified = _optional_text(entry, 'headword[@charset="sc"]')
    traditional = _optional_text(entry, 'headword[@charset="tc"]')
    if simplified is None and traditional is None:
        message = "Entry has no simplified or traditional headword."
        raise PlecoParseError(message)
    pinyin = _optional_text(entry, 'pron[@type="hypy"]')
    definition = _optional_text(entry, "defn")
    references = _references(card)
    return PlecoEntry(
        source_id=_source_id(simplified, traditional, pinyin, definition, references),
        headword_simplified=simplified,
        headword_traditional=traditional,
        pinyin=pinyin,
        definition=definition,
        sentences=(),
        dictionary_references=references,
    )
