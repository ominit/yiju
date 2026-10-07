import hashlib
import json
import sqlite3
from dataclasses import asdict

from yiju.core.extraction import extract_examples
from yiju.core.normalization import normalize_text
from yiju.core.pleco_parser import parse_pleco_xml
from yiju.core.scheduling import new_card, now_utc
from yiju.core.text import (
    clean_definition,
    comparison_text,
    contains_han,
    display_pinyin,
)
from yiju.core.tokenizer import tokenize

from .database import transaction


def identity(kind: str, text: str) -> str:
    return hashlib.sha256(f"{kind}:{comparison_text(text)}".encode()).hexdigest()


def import_xml(connection: sqlite3.Connection, data: bytes, filename: str) -> int:
    digest = hashlib.sha256(data).hexdigest()
    existing = connection.execute(
        "SELECT id FROM imports WHERE digest = ?", (digest,)
    ).fetchone()
    if existing is not None:
        return int(existing["id"])
    parsed = parse_pleco_xml(data)
    words = tuple(
        sorted(
            {
                normalize_text(entry.headword_simplified)
                for entry in parsed.entries
                if entry.headword_simplified is not None
            }
            | {row[0] for row in connection.execute("SELECT word FROM entries")}
        )
    )
    prepared = [
        (
            entry,
            [
                (example, tokenize(example.chinese, words))
                for example in extract_examples(entry.definition or "")
            ],
        )
        for entry in parsed.entries
    ]
    summary = {
        "parsed": len(parsed.entries),
        "added": 0,
        "duplicates": 0,
        "sentences": 0,
        "skipped": 0,
        "issues": [asdict(issue) for issue in parsed.issues],
    }
    now = now_utc()
    with transaction(connection):
        existing = connection.execute(
            "SELECT id FROM imports WHERE digest = ?", (digest,)
        ).fetchone()
        if existing is not None:
            return int(existing["id"])
        cursor = connection.execute(
            "INSERT INTO imports(filename, digest, created, summary) VALUES "
            "(?, ?, ?, '{}')",
            (filename, digest, now.isoformat()),
        )
        import_id = cursor.lastrowid
        if import_id is None:
            message = "Import did not receive an ID."
            raise RuntimeError(message)
        for entry, examples in prepared:
            if entry.headword_simplified is None or not contains_han(
                entry.headword_simplified
            ):
                summary["skipped"] += 1
                continue
            word = normalize_text(entry.headword_simplified)
            inserted = connection.execute(
                "INSERT OR IGNORE INTO entries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.source_id,
                    word,
                    entry.headword_traditional,
                    display_pinyin(entry.pinyin),
                    clean_definition(entry.definition or ""),
                    entry.definition or "",
                    json.dumps([asdict(r) for r in entry.dictionary_references]),
                    import_id,
                ),
            ).rowcount
            if not inserted:
                summary["duplicates"] += 1
                continue
            summary["added"] += 1
            card_id = identity("word", word)
            connection.execute(
                "INSERT OR IGNORE INTO cards(id, entry_id, word, scheduling, due) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    card_id,
                    entry.source_id,
                    word,
                    new_card(card_id, now).to_json(),
                    now.isoformat(),
                ),
            )
            for example, tokens in examples:
                if word not in tokens:
                    continue
                summary["sentences"] += connection.execute(
                    "INSERT OR IGNORE INTO sentences(id, entry_id, chinese, pinyin, "
                    "translation, tokens) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        identity("sentence", example.chinese),
                        entry.source_id,
                        example.chinese,
                        example.pinyin or "",
                        example.translation or "",
                        json.dumps(tokens),
                    ),
                ).rowcount
        connection.execute(
            "UPDATE imports SET summary = ? WHERE id = ?",
            (json.dumps(summary), import_id),
        )
    return int(import_id)
