import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from flask import Blueprint, abort, render_template, request, url_for
from fsrs import Card

from yiju.core.definitions import prompt_senses, readings_payload
from yiju.core.dictionary import decode, readings_pinyin
from yiju.core.pronunciation import pronunciation
from yiju.core.scheduling import intervals, now_utc
from yiju.core.strength import MEMORY
from yiju.storage.database import ConflictError, settings
from yiju.storage.learning import review_card, undo_review
from yiju.storage.queue import card_family, counts, study_queue
from yiju.storage.sentences import set_hidden, update_sentence
from yiju.storage.words import status, strength

from . import database, refresh_caches

study = Blueprint("study", __name__)
BATCH_SIZE = 12


@dataclass(frozen=True, slots=True)
class StudyContext:
    now: datetime
    config: dict[str, int | float | bool]
    flagged: frozenset[str]


def _context(connection: sqlite3.Connection, now: datetime) -> StudyContext:
    return StudyContext(
        now,
        settings(connection),
        frozenset(row["word"] for row in connection.execute("SELECT word FROM flags")),
    )


def _reading(text: str, pinyin: str | None) -> list[list[str]]:
    return [
        [characters, syllable] for characters, syllable in pronunciation(text, pinyin)
    ]


def _strength(row: sqlite3.Row, now: datetime) -> dict[str, object]:
    current = strength(row)
    recall = current.recall(now)
    return {
        "stability": round(current.stability, 1),
        "days": current.success_days,
        "recall": None if recall is None else round(recall * 100),
    }


def _sentence_words(
    connection: sqlite3.Connection, sentence_id: int, context: StudyContext
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    rows = connection.execute(
        "SELECT sw.word, w.* FROM sentence_words sw "
        "JOIN words w ON w.word = sw.word WHERE sw.sentence_id = ? "
        "ORDER BY sw.position",
        (sentence_id,),
    ).fetchall()
    tokens: list[dict[str, object]] = [
        {"text": row["word"], "status": status(row)} for row in rows
    ]
    words: list[dict[str, object]] = [
        {
            "word": row["word"],
            "reading": _reading(row["word"], readings_pinyin(decode(row["readings"]))),
            "readings": readings_payload(row["readings"]),
            "flagged": row["word"] in context.flagged,
            "status": status(row),
            "strength": _strength(row, context.now),
        }
        for row in {row["word"]: row for row in rows}.values()
    ]
    return tokens, words


def card_payload(
    connection: sqlite3.Connection, card: sqlite3.Row, context: StudyContext
) -> dict[str, object]:
    sentence = card["sentence_id"] is not None
    text = card["chinese"] if sentence else card["word"]
    pinyin = (
        card["sentence_pinyin"]
        if sentence
        else readings_pinyin(decode(card["readings"]))
    )
    memory = Card.from_json(card["scheduling"])
    payload: dict[str, object] = {
        "id": card["id"],
        "version": card["version"],
        "family": card_family(card),
        "direction": card["card_type"],
        "kind": "sentence" if sentence else "word",
        "word": card["word"],
        "text": text,
        "reading": _reading(text, pinyin),
        "new": not card["reviews"],
        "reviews": card["reviews"],
        "stability": None if memory.stability is None else round(memory.stability, 1),
        "recall": round(MEMORY.get_card_retrievability(memory, context.now) * 100)
        if card["reviews"]
        else None,
        "flagged": card["word"] in context.flagged,
        "audio": url_for("audio.pronunciation", card_id=card["id"]),
        "intervals": intervals(
            memory,
            context.now,
            float(context.config["retention"]),
        ),
    }
    if sentence:
        tokens, words = _sentence_words(connection, card["sentence_id"], context)
        payload |= {
            "sentence": {
                "id": card["sentence_id"],
                "version": card["sentence_version"],
                "pinyin": card["sentence_pinyin"],
            },
            "translation": card["translation"],
            "tokens": tokens,
            "words": words,
        }
    else:
        word = connection.execute(
            "SELECT * FROM words WHERE word = ?", (card["word"],)
        ).fetchone()
        payload |= {
            "prompt": list(prompt_senses(card["readings"])),
            "readings": readings_payload(card["readings"]),
            "status": status(word),
            "strength": _strength(word, context.now),
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
        "counts": counts(queue),
        "lastReview": last["id"] if last else None,
        "learnedAt": {
            "stability": context.config["learned_stability"],
            "days": context.config["learned_days"],
        },
        "cards": [card_payload(connection, row, context) for row in queue[:BATCH_SIZE]],
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


def _marked(data: dict[str, object]) -> tuple[str, ...]:
    value = data.get("marked", [])
    if not isinstance(value, list) or not all(isinstance(word, str) for word in value):
        abort(400, "Missing or invalid marked.")
    return tuple(dict.fromkeys(cast("list[str]", value)))


@study.get("/study")
def page() -> str:
    return render_template("study.html", batch=study_batch())


@study.get("/api/study")
def batch() -> dict[str, object]:
    return study_batch()


@study.post("/api/reviews")
def rate() -> dict[str, object]:
    data = _body()
    card_id = _json_field(data, "card")
    rating = _json_field(data, "rating")
    version = _json_field(data, "version")
    marked = _marked(data)
    if rating not in {1, 2, 3, 4}:
        abort(400, "Choose Again, Hard, Good, or Easy for a card.")
    connection = database()
    saved = _saved_rating(connection, card_id, version, rating)
    if saved is not None:
        return {"review": saved}
    if not study_queue(connection, now_utc(), card_id=card_id):
        message = "This card is no longer due."
        raise ConflictError(message)
    try:
        review = review_card(connection, card_id, version, rating, now_utc(), marked)
    except ConflictError:
        raise
    except ValueError as error:
        abort(400, str(error))
    return {"review": review}


def _saved_rating(
    connection: sqlite3.Connection, card_id: int, version: int, rating: int
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


@study.post("/api/sentences/<int:sentence_id>")
def edit_sentence(sentence_id: int) -> dict[str, object]:
    data = _body()
    card_id = _json_field(data, "card")
    connection = database()
    try:
        update_sentence(
            connection,
            sentence_id,
            _json_field(data, "version"),
            _text_field(data, "pinyin"),
            _text_field(data, "translation"),
        )
    except ConflictError:
        raise
    except ValueError as error:
        abort(400, str(error))
    now = now_utc()
    rows = study_queue(connection, now, card_id=card_id)
    return {
        "card": card_payload(connection, rows[0], _context(connection, now))
        if rows
        else None
    }


@study.post("/api/sentences/<int:sentence_id>/skip")
def skip_sentence(sentence_id: int) -> dict[str, object]:
    set_hidden(database(), sentence_id, _json_field(_body(), "version"), hidden=True)
    refresh_caches()
    return {"ok": True}
