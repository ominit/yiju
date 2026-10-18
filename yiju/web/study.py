import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from flask import Blueprint, abort, render_template, request, url_for
from fsrs import Card
from werkzeug.wrappers import Response

from yiju.core.definitions import Definition, combine_definitions, prompt_senses
from yiju.core.pronunciation import pronunciation
from yiju.core.scheduling import intervals, now_utc
from yiju.core.selection import analyze
from yiju.storage.database import settings, vocabulary_words
from yiju.storage.learning import ConflictError, known_words, review_card, undo_review
from yiju.storage.queue import card_family, study_queue
from yiju.storage.sentences import update_sentence

from . import database, refresh_caches

study = Blueprint("study", __name__)
BATCH_SIZE = 12


@dataclass(frozen=True, slots=True)
class StudyContext:
    now: datetime
    config: dict[str, int | float | bool]
    known: frozenset[str]
    vocabulary: frozenset[str]
    flagged: frozenset[str]


def _context(connection: sqlite3.Connection, now: datetime) -> StudyContext:
    return StudyContext(
        now,
        settings(connection),
        known_words(connection, now),
        vocabulary_words(connection),
        frozenset(
            row["word"] for row in connection.execute("SELECT word FROM word_flags")
        ),
    )


def _reading(text: str, pinyin: str | None) -> list[list[str]]:
    return [
        [characters, syllable] for characters, syllable in pronunciation(text, pinyin)
    ]


def _definition(value: Definition) -> list[dict[str, object]]:
    return [
        {
            "pos": sense.part_of_speech,
            "meaning": sense.meaning,
            "examples": [
                {
                    "chinese": example.chinese,
                    "pinyin": example.pinyin or "",
                    "translation": example.translation or "",
                }
                for example in sense.examples
            ],
        }
        for sense in value.senses
        if sense.meaning or sense.examples
    ]


def _sentence_words(
    connection: sqlite3.Connection,
    words: tuple[str, ...],
    flagged: frozenset[str],
    unknown: tuple[str, ...],
) -> list[dict[str, object]]:
    entries: dict[str, list[sqlite3.Row]] = {word: [] for word in words}
    for row in connection.execute(
        "SELECT word, pinyin, definition_json FROM entries "
        "WHERE word IN (SELECT value FROM json_each(?)) ORDER BY rowid",
        (json.dumps(words),),
    ):
        entries[row["word"]].append(row)
    result: list[dict[str, object]] = []
    for word, rows in entries.items():
        pinyin = ", ".join(dict.fromkeys(str(row["pinyin"]) for row in rows))
        meanings = "\n".join(dict.fromkeys(str(row["definition_json"]) for row in rows))
        result.append(
            {
                "word": word,
                "reading": _reading(word, pinyin),
                "senses": _definition(combine_definitions(meanings)),
                "flagged": word in flagged,
                "unknown": word in unknown,
            }
        )
    return result


def _card(
    connection: sqlite3.Connection, card: sqlite3.Row, context: StudyContext
) -> dict[str, object]:
    sentence = card["sentence_id"] is not None
    text = card["chinese"] if sentence else card["word"]
    payload: dict[str, object] = {
        "id": card["id"],
        "version": card["version"],
        "family": card_family(card),
        "direction": card["direction"],
        "kind": "sentence" if sentence else "word",
        "word": card["word"],
        "text": text,
        "reading": _reading(
            text, card["sentence_pinyin"] if sentence else card["word_pinyin"]
        ),
        "new": not card["reviews"],
        "flagged": card["word"] in context.flagged,
        "audio": url_for("audio.pronunciation", card_id=card["id"]),
        "intervals": intervals(
            Card.from_json(card["scheduling"]),
            context.now,
            float(context.config["retention"]),
        ),
    }
    if sentence:
        analysis = analyze(
            tuple(json.loads(card["tokens"])),
            context.known,
            card["word"],
            int(context.config["max_unknown"]),
            include_zero=bool(context.config["include_zero"])
            or bool(card["sentence_started"]),
            vocabulary=context.vocabulary,
        )
        explained = tuple(
            dict.fromkeys(
                token for token in analysis.tokens if token in context.vocabulary
            )
        )
        payload |= {
            "sentence": {
                "id": card["sentence_id"],
                "version": card["sentence_version"],
                "pinyin": card["sentence_pinyin"],
            },
            "translation": card["translation"],
            "tokens": [
                {"text": token, "unknown": token in analysis.unknown}
                for token in analysis.tokens
            ],
            "words": _sentence_words(
                connection, explained, context.flagged, analysis.unknown
            ),
        }
    else:
        senses = prompt_senses(card["definition_json"])
        payload |= {
            "prompt": [{"pos": part, "meaning": meaning} for part, meaning in senses],
            "senses": _definition(combine_definitions(card["definition_json"])),
        }
    return payload


def study_batch() -> dict[str, object]:
    connection = database()
    now = now_utc()
    queue = study_queue(connection, now)
    context = _context(connection, now)
    last = connection.execute(
        "SELECT id FROM reviews WHERE undone = 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "count": len(queue),
        "lastReview": last["id"] if last else None,
        "cards": [_card(connection, row, context) for row in queue[:BATCH_SIZE]],
    }


def _json_field(data: dict[str, object], name: str) -> int:
    value = data.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        abort(400, f"Missing or invalid {name}.")
    return value


def _body() -> dict[str, object]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, "Expected a JSON body.")
    return cast("dict[str, object]", data)


@study.get("/study")
def page() -> str:
    return render_template("study.html", batch=study_batch())


@study.get("/api/study")
def batch() -> dict[str, object]:
    return study_batch()


@study.post("/api/reviews")
def rate() -> dict[str, object]:
    data = _body()
    card_id = data.get("card")
    rating = _json_field(data, "rating")
    version = _json_field(data, "version")
    if not isinstance(card_id, str) or rating not in {1, 2, 3, 4}:
        abort(400, "Choose Again, Hard, Good, or Easy for a card.")
    connection = database()
    saved = _saved_rating(connection, card_id, version, rating)
    if saved is not None:
        return {"review": saved}
    if not study_queue(connection, now_utc(), card_id=card_id):
        message = "This card is no longer due."
        raise ConflictError(message)
    return {"review": review_card(connection, card_id, version, rating, now_utc())}


def _saved_rating(
    connection: sqlite3.Connection, card_id: str, version: int, rating: int
) -> int | None:
    saved = connection.execute(
        "SELECT id, rating FROM reviews WHERE card_id = ? AND undone = 0 "
        "AND json_extract(card_before, '$.version') = ?",
        (card_id, version),
    ).fetchone()
    if saved is None:
        return None
    if saved["rating"] != rating:
        message = "This card was already rated differently."
        raise ConflictError(message)
    return int(saved["id"])


@study.post("/api/reviews/undo")
def undo() -> dict[str, object]:
    undo_review(database(), _json_field(_body(), "review"))
    return {"ok": True}


def _text_field(data: dict[str, object], name: str) -> str:
    value = data.get(name, "")
    if not isinstance(value, str):
        abort(400, f"Missing or invalid {name}.")
    return value


@study.post("/api/sentences/<sentence_id>")
def edit_sentence(sentence_id: str) -> dict[str, object]:
    data = _body()
    card_id = data.get("card")
    if not isinstance(card_id, str):
        abort(400, "Missing or invalid card.")
    connection = database()
    try:
        update_sentence(
            connection,
            sentence_id,
            _json_field(data, "version"),
            _text_field(data, "chinese"),
            _text_field(data, "pinyin"),
            _text_field(data, "translation"),
            "accepted",
            now_utc(),
        )
    except ConflictError:
        raise
    except ValueError as error:
        abort(400, str(error))
    refresh_caches()
    now = now_utc()
    rows = study_queue(connection, now, card_id=card_id)
    return {
        "card": _card(connection, rows[0], _context(connection, now)) if rows else None
    }


@study.post("/api/sentences/<sentence_id>/skip")
def skip_sentence(sentence_id: str) -> dict[str, object] | Response:
    connection = database()
    row = connection.execute(
        "SELECT * FROM sentences WHERE id = ?", (sentence_id,)
    ).fetchone()
    if row is None:
        abort(404)
    try:
        update_sentence(
            connection,
            sentence_id,
            _json_field(_body(), "version"),
            row["chinese"],
            row["pinyin"],
            row["translation"],
            "rejected",
            now_utc(),
        )
    except ConflictError:
        raise
    except ValueError as error:
        abort(400, str(error))
    return {"ok": True}
