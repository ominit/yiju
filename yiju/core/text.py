import re
import unicodedata

from pypinyin.contrib.tone_convert import to_tone

from .normalization import normalize_text

HAN = r"\u3400-\u4dbf\u4e00-\u9fff"
CROSS_REFERENCE = re.compile("\uead1.*?\uead2", re.DOTALL)


def clean_definition(text: str) -> str:
    def replace_reference(match: re.Match[str]) -> str:
        parts = match.group().split("\uead0")
        return parts[-1].rstrip("\uead2") if len(parts) > 1 else ""

    text = CROSS_REFERENCE.sub(replace_reference, text)
    return "".join(char for char in text if unicodedata.category(char) != "Co")


def display_pinyin(text: str | None) -> str:
    text = re.sub(
        r"[a-zA-ZüÜvV:]+[1-5]",
        lambda match: to_tone(match.group()) + " ",
        (text or "").replace("//", " "),
    )
    return " ".join(text.split())


def comparison_text(text: str) -> str:
    return "".join(normalize_text(text).split())


def contains_han(text: str) -> bool:
    return re.search(f"[{HAN}]", text) is not None


def short_definition(text: str) -> str:
    first_chinese = re.search(f"[{HAN}]", text)
    lead = text[: first_chinese.start()] if first_chinese is not None else text
    return lead.strip().rstrip("([,; ")[:240] or text[:240]
