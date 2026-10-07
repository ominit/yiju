import json
import sqlite3

from flask import redirect, render_template, request, url_for
from fsrs import Card
from werkzeug.wrappers import Response

from yiju.core.definitions import Definition, combine_definitions
from yiju.core.scheduling import intervals, now_utc
from yiju.core.selection import analyze
from yiju.storage.database import settings, vocabulary_words
from yiju.storage.learning import known_words
from yiju.storage.queue import overview, study_queue

from . import database


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


def study_response() -> str | Response:
    template = (
        "study_content.html"
        if request.headers.get("X-Yiju-Partial") == "study"
        else "study.html"
    )
    connection = database()
    now = now_utc()
    queue = study_queue(connection, now)
    last_review = connection.execute(
        "SELECT id FROM reviews WHERE undone = 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not queue:
        return render_template(
            template,
            card=None,
            stats=overview(connection, now),
            last_review=last_review,
        )
    card_id = request.args.get("card")
    card = next((row for row in queue if row["id"] == card_id), queue[0])
    if card_id is not None and card_id != card["id"]:
        return redirect(url_for("pages.study"))
    analysis = None
    sentence_definitions = []
    config = settings(connection)
    if card["tokens"] is not None:
        analysis = analyze(
            tuple(json.loads(card["tokens"])),
            known_words(connection, now),
            card["word"],
            int(config["max_unknown"]),
            include_zero=bool(config["include_zero"]),
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
    return render_template(
        template,
        card=card,
        count=len(queue),
        answer=request.args.get("answer") == "1",
        intervals=intervals(
            Card.from_json(card["scheduling"]), now, float(config["retention"])
        ),
        analysis=analysis,
        sentence_definitions=sentence_definitions,
        word_definition=combine_definitions(card["definition_json"]),
        last_review=last_review,
    )
