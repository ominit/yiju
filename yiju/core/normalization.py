import unicodedata


def normalize_text(text: str) -> str:
    return unicodedata.normalize("NFC", text).strip()
