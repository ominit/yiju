import json

from flask import Blueprint, Response, abort, jsonify, request

from yiju.core.scheduling import now_utc
from yiju.storage.database import transaction

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
            "SELECT 1 FROM entries WHERE word = ?", (word,)
        ).fetchone():
            abort(404, "This word is not in the library.")
        if flagged == "1":
            now = now_utc().isoformat()
            connection.execute(
                "INSERT INTO word_flags(word, note, created, updated) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(word) DO UPDATE SET "
                "note = CASE WHEN ? THEN excluded.note ELSE word_flags.note END, "
                "updated = excluded.updated",
                (word, (note or "").strip(), now, now, note is not None),
            )
        else:
            connection.execute("DELETE FROM word_flags WHERE word = ?", (word,))
    return jsonify(word=word, flagged=flagged == "1")


@flags.get("/flags/words/export")
def export_flags() -> Response:
    connection = database()
    words = []
    for flag in connection.execute("SELECT * FROM word_flags ORDER BY created, word"):
        entries = [
            {
                "id": row["id"],
                "traditional": row["traditional"],
                "pinyin": row["pinyin"],
                "definition": row["definition"],
                "structured_definition": json.loads(row["definition_json"]),
                "original_definition": row["raw_definition"],
                "references": json.loads(row["references_json"]),
                "import_filename": row["filename"],
            }
            for row in connection.execute(
                "SELECT e.*, i.filename FROM entries e "
                "JOIN imports i ON i.id = e.import_id "
                "WHERE e.word = ? ORDER BY e.rowid",
                (flag["word"],),
            )
        ]
        sentences = [
            dict(row)
            for row in connection.execute(
                "SELECT s.id, s.entry_id, s.chinese, s.pinyin, s.translation, "
                "s.status, "
                "s.version FROM sentences s JOIN entries e ON e.id = s.entry_id "
                "WHERE e.word = ? OR EXISTS "
                "(SELECT 1 FROM json_each(s.tokens) WHERE value = ?) ORDER BY s.id",
                (flag["word"], flag["word"]),
            )
        ]
        words.append({**dict(flag), "entries": entries, "sentences": sentences})
    response = jsonify(version=1, words=words)
    response.headers["Content-Disposition"] = (
        'attachment; filename="yiju-flagged-words.json"'
    )
    return response
