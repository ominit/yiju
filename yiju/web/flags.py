from flask import Blueprint, Response, abort, jsonify, request

from yiju.core.scheduling import now_utc
from yiju.storage.database import transaction
from yiju.storage.export import flagged_words

from . import database

flags = Blueprint("flags", __name__)
MAX_NOTE_LENGTH = 2000


@flags.post("/flags/words")
def save_flag() -> Response:
    word = request.form.get("word", "").strip()
    note = request.form.get("note")
    flagged = request.form.get("flagged")
    if flagged not in {"0", "1"}:
        abort(400, "Choose whether to flag this word.")
    if note is not None and len(note) > MAX_NOTE_LENGTH:
        abort(400, "Keep flag notes within 2,000 characters.")
    connection = database()
    with transaction(connection):
        if not connection.execute(
            "SELECT 1 FROM words WHERE word = ?", (word,)
        ).fetchone():
            abort(404, "This word is not in the library.")
        if flagged == "1":
            now = now_utc().isoformat()
            connection.execute(
                "INSERT INTO flags(word, note, created, updated) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(word) DO UPDATE SET "
                "note = CASE WHEN ? THEN excluded.note ELSE flags.note END, "
                "updated = excluded.updated",
                (word, (note or "").strip(), now, now, note is not None),
            )
        else:
            connection.execute("DELETE FROM flags WHERE word = ?", (word,))
    return jsonify(word=word, flagged=flagged == "1")


@flags.get("/flags/words/export")
def export_flags() -> Response:
    response = jsonify(version=2, words=flagged_words(database()))
    response.headers["Content-Disposition"] = (
        'attachment; filename="yiju-flagged-words.json"'
    )
    return response
