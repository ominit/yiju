import re

from pypinyin.contrib.tone_convert import to_tone

HAN = r"\u3400-\u4dbf\u4e00-\u9fff"


def display_pinyin(text: str | None) -> str:
    text = re.sub(
        r"[a-zA-ZüÜvV:]+[1-5]",
        lambda match: to_tone(match.group()) + " ",
        (text or "").replace("//", " "),
    )
    return " ".join(text.split())


def contains_han(text: str) -> bool:
    return re.search(f"[{HAN}]", text) is not None
