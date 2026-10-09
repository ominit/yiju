import json
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import current_app, jsonify, redirect, render_template, request, url_for
from fsrs import Card
from werkzeug.wrappers import Response

from yiju.core.definitions import Definition, combine_definitions
from yiju.core.scheduling import intervals, now_utc
from yiju.core.selection import analyze
from yiju.storage.database import settings, vocabulary_words
from yiju.storage.learning import known_words
from yiju.storage.queue import card_family, study_queue

from . import database
from .strokes import available_strokes


def _sentence_definitions(
    connection: sqlite3.Connection, words: tuple[str, ...]
) -> list[dict[str, str | Definition]]:
    entries: dict[str, list[sqlite3.Row]] = {word: [] for word in words}
    for row in connection.execute(
        "SELECT word, pinyin, definition_json FROM entries "
        "WHERE word IN (SELECT value FROM json_each(?)) ORDER BY rowid",
        (json.dumps(words),),
    ):
        entries[row["word"]].append(row)
    definitions: list[dict[str, str | Definition]] = []
    for word, rows in entries.items():
        meanings = list(dict.fromkeys(str(row["definition_json"]) for row in rows))
        definitions.append(
            {
                "word": word,
                "pinyin": ", ".join(dict.fromkeys(str(row["pinyin"]) for row in rows)),
                "definition": combine_definitions("\n".join(meanings)),
            }
        )
    return definitions


def _card_context(
    connection: sqlite3.Connection, card: sqlite3.Row, now: datetime
) -> dict[str, object]:
    config = settings(connection)
    analysis = None
    sentence_definitions = []
    if card["tokens"] is not None:
        analysis = analyze(
            tuple(json.loads(card["tokens"])),
            known_words(connection, now),
            card["word"],
            int(config["max_unknown"]),
            include_zero=bool(config["include_zero"]) or bool(card["sentence_started"]),
            vocabulary=vocabulary_words(connection),
        )
        sentence_definitions = _sentence_definitions(
            connection,
            tuple(
                dict.fromkeys(
                    token
                    for token in analysis.tokens
                    if token in analysis.unknown or not analysis.unknown
                )
            ),
        )
    return {
        "card": card,
        "card_family": card_family(card),
        "flagged_words": {
            row["word"] for row in connection.execute("SELECT word FROM word_flags")
        },
        "intervals": intervals(
            Card.from_json(card["scheduling"]), now, float(config["retention"])
        ),
        "analysis": analysis,
        "sentence_definitions": sentence_definitions,
        "word_definition": combine_definitions(card["definition_json"]),
        "stroke_glyphs": available_strokes(
            card["chinese"] or card["word"],
            Path(current_app.config["DATABASE"]).resolve().parent / "strokes",
        ),
    }


def study_response() -> str | Response:
    template = (
        "study_content.html"
        if request.headers.get("X-Yiju-Partial") == "study"
        else "study.html"
    )
    connection = database()
    now = now_utc()
    include_listening = (
        request.headers.get("X-Yiju-Audio", request.args.get("audio")) != "muted"
    )
    card_id = request.args.get("card")
    queue = study_queue(
        connection,
        now,
        include_listening=include_listening,
        preferred_card_id=card_id,
    )
    last_review = connection.execute(
        "SELECT id FROM reviews WHERE undone = 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    card = next(
        (row for row in queue if row["id"] == card_id), queue[0] if queue else None
    )
    if card_id is not None and (card is None or card_id != card["id"]):
        return redirect(
            url_for("pages.study", audio="muted" if not include_listening else None)
        )
    batch = ([card] if card is not None else []) + [
        row for row in queue if card is not None and row["id"] != card["id"]
    ][:11]
    if template == "study_content.html" and request.headers.get("X-Yiju-Buffer") != "1":
        batch = []
    prepared = [
        render_template(
            "study_content.html",
            **_card_context(connection, row, now),
            count=max(1, len(queue) - index),
            answer=False,
            last_review=last_review,
            include_listening=include_listening,
        )
        for index, row in enumerate(batch)
    ]
    if request.headers.get("X-Yiju-Buffer") == "1":
        return jsonify(
            cards=prepared,
            review_id=last_review["id"] if last_review else None,
            count=len(queue),
            include_listening=include_listening,
        )
    return render_template(
        template,
        **(
            _card_context(connection, card, now) if card is not None else {"card": None}
        ),
        count=len(queue),
        answer=request.args.get("answer") == "1",
        last_review=last_review,
        prepared=prepared[1:],
        include_listening=include_listening,
    )
