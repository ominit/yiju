import re
from functools import lru_cache

from .dictionary import decode, is_reference
from .text import HAN

CHINESE = re.compile(rf"[{HAN}·]+(?:\s*\([^()]*\))?")


def _masked(text: str) -> str:
    text = CHINESE.sub("…", text)
    text = re.sub(r"\([\s…,;]*\)", "", text)
    text = re.sub(r"…(?:[\s,;]*…)+", "…", text)
    text = re.sub(r"\s+([,;.])", r"\1", " ".join(text.split()))
    return text.strip(" ;,")


@lru_cache(maxsize=2048)
def prompt_senses(readings: str) -> tuple[str, ...]:
    prompts: dict[str, None] = {}
    for reading in decode(readings):
        for sense in reading.senses:
            if is_reference(sense):
                continue
            text = _masked(sense.text)
            if text and text != "…":
                prompts[text] = None
    return tuple(prompts)


def readings_payload(readings: str) -> list[dict[str, object]]:
    return [
        {
            "pinyin": reading.pinyin,
            "senses": [
                {"text": sense.text, "notes": list(sense.notes), "usage": sense.usage}
                for sense in reading.senses
            ],
            "classifiers": list(reading.classifiers),
            "also_pronounced": list(reading.also_pronounced),
        }
        for reading in decode(readings)
    ]


def summary(readings: str, limit: int = 3) -> str:
    meanings = dict.fromkeys(
        sense.text
        for reading in decode(readings)
        for sense in reading.senses
        if not is_reference(sense)
    )
    return "; ".join(list(meanings)[:limit])


def search_text(readings: str) -> str:
    return " ".join(
        [reading.pinyin for reading in decode(readings)]
        + [sense.text for reading in decode(readings) for sense in reading.senses]
    )


def brief_readings(readings: str) -> list[dict[str, object]]:
    return [
        {
            "pinyin": reading.pinyin,
            "meanings": [
                f"{sense.text} ({', '.join(sense.notes)})"
                if sense.notes
                else sense.text
                for sense in reading.senses
            ],
        }
        | ({"classifiers": list(reading.classifiers)} if reading.classifiers else {})
        | (
            {"also_pronounced": list(reading.also_pronounced)}
            if reading.also_pronounced
            else {}
        )
        for reading in decode(readings)
    ]
