import math
import re
import unicodedata
from dataclasses import dataclass
from itertools import product
from typing import TYPE_CHECKING

from pypinyin import Style, lazy_pinyin

from .dictionary import join_syllables
from .pronunciation import pronunciation
from .text import HAN

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from random import Random

GENDERS = ("male", "female")
MAX_PEOPLE = 6
SLOT = re.compile(r"\{([^{}]*)\}")
NAME_SLOT = re.compile(r"\{([A-Z])\}")
LETTER = re.compile(r"\{([A-Z])(?=[.}])")
CHINESE_SLOT = re.compile(r"([A-Z])(?:\.([他她]))?")
ENGLISH_SLOT = re.compile(r"([A-Z])(?:\.([Tt]he(?:y|m|ir|irs|mself)))?")
RULE = re.compile(r"([A-Z])(?:=(male|female)|(=|!=)([A-Z]))")
CHINESE_PRONOUNS = {"male": "他", "female": "她"}
ENGLISH_PRONOUNS = {
    "male": {
        "they": "he",
        "them": "him",
        "their": "his",
        "theirs": "his",
        "themself": "himself",
    },
    "female": {
        "they": "she",
        "them": "her",
        "their": "her",
        "theirs": "hers",
        "themself": "herself",
    },
}
STAND_IN = ("某", "mǒu")
SURNAME_READINGS = {
    "曾": "zēng",
    "单": "shàn",
    "解": "xiè",
    "仇": "qiú",
    "朴": "piáo",
    "查": "zhā",
    "区": "ōu",
    "乐": "yuè",
    "华": "huà",
    "任": "rén",
    "翟": "zhái",
    "缪": "miào",
    "覃": "qín",
    "盖": "gě",
    "种": "chóng",
    "尉迟": "yùchí",
    "万俟": "mòqí",
}
COMPOUND_SURNAMES = frozenset(
    {
        "欧阳",
        "司马",
        "诸葛",
        "上官",
        "东方",
        "司徒",
        "皇甫",
        "慕容",
        "令狐",
        "夏侯",
        "尉迟",
        "万俟",
    }
)
CHINESE_SLOTS = "Use {A} for a name and {A.他} for 他 or 她."
ENGLISH_SLOTS = (
    "Use {A} for a name and {A.they}, {A.them}, {A.their}, {A.theirs} or "
    "{A.themself} for a pronoun."
)
RULE_FORMS = "Use A=male, A=female, A=B or A!=B."

type Rule = tuple[str, str, str]
type Gendering = dict[str, str]
type Part = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class Name:
    id: int
    chinese: str
    pinyin: str
    english: str
    gender: str


def is_slot(token: str) -> bool:
    return SLOT.fullmatch(token) is not None


def strip_slots(text: str) -> str:
    return SLOT.sub("", text)


def _group(match: re.Match[str], index: int) -> str:
    value = match.group(index)
    return value if isinstance(value, str) else ""


def letters(text: str) -> tuple[str, ...]:
    return tuple(sorted({_group(match, 1) for match in LETTER.finditer(text)}))


def clean_rules(text: str) -> str:
    return " ".join(text.split())


def parse_rules(text: str) -> tuple[Rule, ...]:
    rules: list[Rule] = []
    for part in text.split():
        match = RULE.fullmatch(part)
        if match is None:
            message = f'"{part}" is not a gender rule. {RULE_FORMS}'
            raise ValueError(message)
        letter, gender, relation, other = (
            _group(match, index) for index in range(1, 5)
        )
        rules.append((letter, "=", gender) if gender else (letter, relation, other))
    return tuple(rules)


def _holds(rule: Rule, genders: Gendering) -> bool:
    letter, relation, other = rule
    target = other if other in GENDERS else genders[other]
    return (genders[letter] == target) == (relation == "=")


def genderings(people: Sequence[str], rules: Sequence[Rule]) -> list[Gendering]:
    found = []
    for values in product(GENDERS, repeat=len(people)):
        genders = dict(zip(people, values, strict=True))
        if all(_holds(rule, genders) for rule in rules):
            found.append(genders)
    return found


def _ways(genders: Gendering, counts: Mapping[str, int]) -> int:
    return math.prod(
        math.perm(counts.get(gender, 0), list(genders.values()).count(gender))
        for gender in GENDERS
    )


def fixed_genders(people: Sequence[str], rules: Sequence[Rule]) -> dict[str, str]:
    options = genderings(people, rules)
    return {
        letter: options[0][letter]
        for letter in people
        if options and len({option[letter] for option in options}) == 1
    }


def _syntax_error(chinese: str, translation: str) -> str | None:
    for text, grammar, side, help_text in (
        (chinese, CHINESE_SLOT, "sentence", CHINESE_SLOTS),
        (translation, ENGLISH_SLOT, "translation", ENGLISH_SLOTS),
    ):
        if re.search(r"[{}]", strip_slots(text)):
            return f"The {side} has a brace outside a slot. {help_text}"
        for match in SLOT.finditer(text):
            if grammar.fullmatch(_group(match, 1)) is None:
                return f"{match.group()} is not a slot the {side} can use. {help_text}"
    return None


def _people_error(people: tuple[str, ...], named: tuple[str, ...]) -> str | None:
    for letter in people:
        if letter not in named:
            return f"The translation never mentions {letter}."
    for letter in named:
        if letter not in people:
            return f"The sentence never mentions {letter}."
    if len(people) > MAX_PEOPLE:
        return f"Use at most {MAX_PEOPLE} people in one sentence."
    return None


def _rules_error(people: tuple[str, ...], rules: str) -> str | None:
    try:
        parsed = parse_rules(rules)
    except ValueError as error:
        return str(error)
    for letter, _relation, other in parsed:
        for value in (letter, other):
            if value not in GENDERS and value not in people:
                return f"The gender rules mention {value}, who is not in the sentence."
    if not genderings(people, parsed):
        return "The gender rules contradict each other."
    return None


def slot_error(
    chinese: str, translation: str, rules: str, pinyin: str | None = None
) -> str | None:
    people = letters(chinese)
    error = (
        _syntax_error(chinese, translation)
        or _people_error(people, letters(translation))
        or _rules_error(people, rules)
    )
    if (
        error is None
        and pinyin is not None
        and NAME_SLOT.findall(pinyin) != NAME_SLOT.findall(chinese)
    ):
        return "Keep the name slots in the pinyin, in the same order as the sentence."
    return error


def pick(
    people: Sequence[str], rules: Sequence[Rule], pool: Sequence[Name], random: Random
) -> dict[str, Name] | None:
    by_gender = {
        gender: [name for name in pool if name.gender == gender] for gender in GENDERS
    }
    counts = {gender: len(names) for gender, names in by_gender.items()}
    options = genderings(people, rules)
    weights = [_ways(option, counts) for option in options]
    if not any(weights):
        return None
    chosen = random.choices(options, weights)[0]
    picked: dict[str, Name] = {}
    for gender in GENDERS:
        slots = [letter for letter in people if chosen[letter] == gender]
        picked |= dict(
            zip(slots, random.sample(by_gender[gender], len(slots)), strict=True)
        )
    return picked


def _amount(count: int, gender: str | None = None) -> str:
    kind = "name" if gender is None else f"{gender} name"
    return f"{count} {kind}{'' if count == 1 else 's'}"


def missing(
    people: Sequence[str], rules: Sequence[Rule], counts: Mapping[str, int]
) -> str | None:
    options = genderings(people, rules)
    if any(_ways(option, counts) for option in options):
        return None
    if not rules:
        return _amount(len(people))
    needs = [
        {gender: list(option.values()).count(gender) for gender in GENDERS}
        for option in options
    ]

    def shortfall(need: dict[str, int]) -> int:
        return sum(max(0, need[gender] - counts.get(gender, 0)) for gender in GENDERS)

    least = min(map(shortfall, needs), default=0)
    phrases = dict.fromkeys(
        " and ".join(
            _amount(need[gender], gender) for gender in GENDERS if need[gender]
        )
        for need in needs
        if shortfall(need) == least
    )
    return " or ".join(phrases)


def _pieces(
    chinese: str, pinyin: str, names: Mapping[str, Name] | None, rules: str
) -> tuple[list[tuple[str, str]], str]:
    fixed = (
        {} if names is not None else fixed_genders(letters(chinese), _safe_rules(rules))
    )
    pieces: list[tuple[str, str]] = []
    position = 0
    for match in SLOT.finditer(chinese):
        pieces.append((chinese[position : match.start()], ""))
        position = match.end()
        slot = CHINESE_SLOT.fullmatch(match.group(1))
        if slot is None:
            pieces.append((match.group(), ""))
            continue
        letter, pronoun = slot.groups()
        if pronoun:
            gender = names[letter].gender if names is not None else fixed.get(letter)
            pieces.append(
                (CHINESE_PRONOUNS.get(gender or "", "他"), f"pronoun:{letter}")
            )
        elif names is not None:
            pieces.append((names[letter].chinese, f"name:{letter}"))
        else:
            pieces.append((STAND_IN[0], f"slot:{letter}"))
    pieces.append((chinese[position:], ""))

    def spoken(match: re.Match[str]) -> str:
        letter = match.group(1)
        if names is not None:
            return names[letter].pinyin if letter in names else match.group()
        return STAND_IN[1]

    return pieces, NAME_SLOT.sub(spoken, pinyin)


def _safe_rules(rules: str) -> tuple[Rule, ...]:
    try:
        return parse_rules(rules)
    except ValueError:
        return ()


def reading(
    chinese: str,
    pinyin: str | None,
    rules: str = "",
    names: Mapping[str, Name] | None = None,
) -> list[Part]:
    if not SLOT.search(chinese):
        return [
            (text, syllable, "") for text, syllable in pronunciation(chinese, pinyin)
        ]
    pieces, spoken = _pieces(chinese, pinyin or "", names, rules)
    text = "".join(piece for piece, _kind in pieces)
    pairs = pronunciation(text, spoken)
    if len(pairs) != len(text):
        if names is None:
            return [(SLOT.sub(_label, chinese), pinyin or "", "")]
        return [(text, syllable, "") for text, syllable in pairs]
    kinds = [kind for piece, kind in pieces for _ in piece]
    fixed = fixed_genders(letters(chinese), _safe_rules(rules))
    parts: list[Part] = []
    for (character, syllable), kind in zip(pairs, kinds, strict=True):
        if kind.startswith("slot:"):
            parts.append((kind[5:], "", kind))
        elif kind.startswith("pronoun:") and names is None and kind[8:] not in fixed:
            parts.append(("他/她", syllable, kind))
        else:
            parts.append((character, syllable, kind))
    return parts


def _label(match: re.Match[str]) -> str:
    letter, _dot, form = _group(match, 1).partition(".")
    if not form:
        return letter
    return "他/她" if form in CHINESE_PRONOUNS.values() else form


def label(slot: str) -> tuple[str, str]:
    match = SLOT.fullmatch(slot)
    if match is None:
        return slot, ""
    letter = _group(match, 1).partition(".")[0]
    text = _label(match)
    return text, "" if text == letter else letter


def chinese_text(chinese: str, names: Mapping[str, Name]) -> str:
    pieces, _spoken = _pieces(chinese, "", names, "")
    return "".join(piece for piece, _kind in pieces)


def _english(match: re.Match[str], names: Mapping[str, Name]) -> str:
    slot = ENGLISH_SLOT.fullmatch(match.group(1))
    if slot is None or slot.group(1) not in names:
        return match.group()
    letter, form = slot.groups()
    name = names[letter]
    if form is None:
        return name.english
    word = ENGLISH_PRONOUNS[name.gender][form.lower()]
    return word.capitalize() if form[0].isupper() else word


def english(translation: str, names: Mapping[str, Name]) -> str:
    return SLOT.sub(lambda match: _english(match, names), translation)


def prompt(translation: str, names: Mapping[str, Name]) -> list[tuple[str, str]]:
    parts = []
    position = 0
    for match in NAME_SLOT.finditer(translation):
        if match.group(1) not in names:
            continue
        parts.append((english(translation[position : match.start()], names), ""))
        parts.append((names[match.group(1)].english, names[match.group(1)].chinese))
        position = match.end()
    parts.append((english(translation[position:], names), ""))
    return [part for part in parts if part[0]]


def _surname(chinese: str) -> int:
    return 2 if len(chinese) > 2 and chinese[:2] in COMPOUND_SURNAMES else 1  # noqa: PLR2004


def name_pinyin(chinese: str) -> str:
    size = _surname(chinese)
    parts = [
        SURNAME_READINGS.get(chinese[:size])
        or join_syllables(lazy_pinyin(chinese[:size], style=Style.TONE)),
        join_syllables(lazy_pinyin(chinese[size:], style=Style.TONE)),
    ]
    return " ".join(part[:1].upper() + part[1:] for part in parts if part)


def english_spelling(pinyin: str) -> str:
    return unicodedata.normalize(
        "NFC",
        "".join(
            character
            for character in unicodedata.normalize("NFD", pinyin)
            if not unicodedata.combining(character) or character == "̈"
        ),
    )


def name_error(chinese: str) -> str | None:
    if not re.fullmatch(f"[{HAN}]{{1,4}}", chinese):
        return f"{chinese} is not a name. Write one to four Chinese characters."
    return None
