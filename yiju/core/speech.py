import re
import unicodedata

from pypinyin.contrib.tone_convert import to_finals, to_initials

from .pronunciation import pronunciation
from .text import contains_han

SYMBOLS = {
    "b": "ㄅ",
    "p": "ㄆ",
    "m": "ㄇ",
    "f": "ㄈ",
    "d": "ㄉ",
    "t": "ㄊ",
    "n": "ㄋ",
    "l": "ㄌ",
    "g": "ㄍ",
    "k": "ㄎ",
    "h": "ㄏ",
    "j": "ㄐ",
    "q": "ㄑ",
    "x": "ㄒ",
    "zh": "ㄓ",
    "ch": "ㄔ",
    "sh": "ㄕ",
    "r": "ㄖ",
    "z": "ㄗ",
    "c": "ㄘ",
    "s": "ㄙ",
    "a": "ㄚ",
    "o": "ㄛ",
    "e": "ㄜ",
    "ie": "ㄝ",
    "ai": "ㄞ",
    "ei": "ㄟ",
    "ao": "ㄠ",
    "ou": "ㄡ",
    "an": "ㄢ",
    "en": "ㄣ",
    "ang": "ㄤ",
    "eng": "ㄥ",
    "er": "ㄦ",
    "i": "ㄧ",
    "u": "ㄨ",
    "v": "ㄩ",
    "ii": "ㄭ",
    "iii": "十",
    "ve": "月",
    "ia": "压",
    "ian": "言",
    "iang": "阳",
    "iao": "要",
    "in": "阴",
    "ing": "应",
    "iong": "用",
    "iou": "又",
    "ong": "中",
    "ua": "穵",
    "uai": "外",
    "uan": "万",
    "uang": "王",
    "uei": "为",
    "uen": "文",
    "ueng": "瓮",
    "uo": "我",
    "van": "元",
    "vn": "云",
}
PUNCTUATION = {
    "、": ",",
    "，": ",",
    ",": ",",
    "。": ".",
    "．": ".",
    ".": ".",
    "！": "!",
    "!": "!",
    "？": "?",
    "?": "?",
    "：": ":",
    ":": ":",
    "；": ";",
    ";": ";",
    "…": "…",
    "—": "—",
    "（": "(",
    "(": "(",
    "）": ")",
    ")": ")",
    **dict.fromkeys("“《「【«", "“"),
    **dict.fromkeys("”》」】»", "”"),
}
TONE_MARKS = ("āēīōūǖ", "áéíóúǘ", "ǎěǐǒǔǚ", "àèìòùǜ")
PLAIN = str.maketrans("āáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜü", "aaaaeeeeiiiioooouuuuvvvvv")
NEUTRAL = 5
ERHUA = "r"

type Syllable = tuple[str, str, int]


def tone(syllable: str) -> int:
    for number, marks in enumerate(TONE_MARKS, 1):
        if any(mark in syllable for mark in marks):
            return number
    return NEUTRAL


def _phrases(text: str, pinyin: str) -> list[list[list[Syllable]] | str]:
    groups = iter(len(group) for group in re.findall(r"[^\W\d_]+", pinyin))
    phrases: list[list[list[Syllable]] | str] = []
    phrase: list[list[Syllable]] = []
    left = 0
    for character, syllable in pronunciation(text, pinyin):
        if not contains_han(character):
            if character in PUNCTUATION:
                if phrase:
                    phrases.append(phrase)
                    phrase = []
                phrases.append(PUNCTUATION[character])
            elif not unicodedata.category(character).startswith(("P", "S", "Z")):
                message = f"Cannot pronounce {character!r}."
                raise ValueError(message)
            continue
        if len(character) != 1 or not syllable or " / " in syllable:
            message = f"The pinyin does not match {text!r}."
            raise ValueError(message)
        if left == 0:
            phrase.append([])
            left = next(groups, 0)
        phrase[-1].append((character, syllable.lower(), tone(syllable)))
        left -= len(syllable)
    if phrase:
        phrases.append(phrase)
    return phrases


def sandhi(phrase: list[list[Syllable]]) -> list[int]:
    flat = [
        (character, syllable, value, index == 0)
        for word in phrase
        for index, (character, syllable, value) in enumerate(word)
        if syllable != ERHUA
    ]
    tones = [value for _, _, value, _ in flat]
    for index, (character, _, value, first) in enumerate(flat[:-1]):
        following = flat[index + 1][2]
        if character == "不" and value == 4 and following == 4:  # noqa: PLR2004
            tones[index] = 2
        if character == "一" and value == 1 and first:
            tones[index] = 2 if following in {4, NEUTRAL} else 4
    for index in range(len(flat) - 2, -1, -1):
        if flat[index][2] == 3 and flat[index + 1][2] == 3:  # noqa: PLR2004
            tones[index] = 2
    return tones


def _symbols(syllable: str, value: int) -> list[str]:
    plain = syllable.translate(PLAIN)
    initial = to_initials(plain, strict=True)
    final = to_finals(plain, strict=True)
    if final == "i" and initial in {"z", "c", "s"}:
        final = "ii"
    elif final == "i" and initial in {"zh", "ch", "sh", "r"}:
        final = "iii"
    parts = [initial, final] if initial else [final]
    if any(part not in SYMBOLS for part in parts):
        message = f"Cannot pronounce {syllable!r}."
        raise ValueError(message)
    return [*(SYMBOLS[part] for part in parts), str(value)]


def _word(word: list[Syllable], tones: list[int]) -> str:
    symbols: list[str] = []
    for _, syllable, _ in word:
        if syllable == ERHUA:
            if symbols:
                symbols.insert(-1, "R")
            continue
        symbols += _symbols(syllable, tones.pop(0))
    return "".join(symbols)


def phonemes(text: str, pinyin: str) -> str:
    pieces: list[str] = []
    for phrase in _phrases(text, pinyin):
        if isinstance(phrase, str):
            pieces.append(phrase + " ")
            continue
        tones = sandhi(phrase)
        pieces.append("/".join(_word(word, tones) for word in phrase))
    spoken = "".join(pieces).strip()
    if not any(symbol in spoken for symbol in "12345"):
        message = f"There is nothing to say in {text!r}."
        raise ValueError(message)
    return spoken
