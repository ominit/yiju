import io
import json
import sqlite3
import tempfile
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    request,
    send_file,
    url_for,
)
from werkzeug.wrappers import Response

from yiju.core.pleco_parser import PlecoParseError
from yiju.core.scheduling import now_utc
from yiju.storage.database import transaction
from yiju.storage.importing import import_xml
from yiju.storage.learning import ConflictError
from yiju.storage.sentences import update_sentence

from . import database, refresh_caches
from .library import item_response

actions = Blueprint("actions", __name__)
SETTING_RANGES = {
    "max_unknown": (1, 5),
    "new_per_day": (0, 200),
    "known_days": (1, 30),
    "known_stability": (1, 365),
    "retention": (0.7, 0.99),
    "known_retrievability": (0.5, 0.99),
}


def integer(name: str) -> int:
    value = request.form.get(name, type=int)
    if value is None:
        abort(400, f"Missing or invalid {name}.")
    return value


@actions.post("/import")
def upload() -> Response:
    upload_file = request.files.get("file")
    if upload_file is None or not bool(upload_file.filename):
        abort(400, "Choose a Pleco XML export first.")
    try:
        import_id = import_xml(
            database(),
            upload_file.read(),
            Path(upload_file.filename or "export.xml").name[:200],
        )
    except PlecoParseError as error:
        abort(400, str(error))
    refresh_caches()
    return redirect(url_for("pages.import_result", import_id=import_id), code=303)


@actions.post("/sentences/<sentence_id>")
def save_sentence(sentence_id: str) -> str:
    try:
        update_sentence(
            database(),
            sentence_id,
            integer("version"),
            request.form.get("chinese", ""),
            request.form.get("pinyin", ""),
            request.form.get("translation", ""),
            request.form.get("status", "accepted"),
            now_utc(),
        )
    except ConflictError:
        raise
    except ValueError as error:
        abort(400, str(error))
    refresh_caches()
    return item_response("sentences", sentence_id, now_utc())


@actions.post("/words/<word>/pause")
def pause_word(word: str) -> str:
    connection = database()
    with transaction(connection):
        card = connection.execute(
            "SELECT suspended FROM cards WHERE word = ? AND sentence_id IS NULL "
            "AND direction = 'recognition' AND version = ?",
            (word, integer("version")),
        ).fetchone()
        if card is None:
            message = "This word has changed. Reload the page."
            raise ConflictError(message)
        connection.execute(
            "UPDATE cards SET suspended = ?, version = version + 1 "
            "WHERE word = ? AND sentence_id IS NULL",
            (1 - card["suspended"], word),
        )
    return item_response("words", word, now_utc())


@actions.post("/settings")
def save_settings() -> Response:
    values: dict[str, int | float | bool] = {
        "include_zero": request.form.get("include_zero") == "on"
    }
    for key, (low, high) in SETTING_RANGES.items():
        value = request.form.get(key, type=float)
        if value is None or not low <= value <= high:
            abort(400, f"{key} must be between {low} and {high}.")
        if isinstance(low, int) and not value.is_integer():
            abort(400, f"{key} must be a whole number.")
        values[key] = int(value) if isinstance(low, int) else value
    with transaction(database()):
        database().executemany(
            "UPDATE settings SET value = ? WHERE key = ?",
            [(json.dumps(value), key) for key, value in values.items()],
        )
    flash("Settings saved.")
    return redirect(url_for("pages.preferences"), code=303)


@actions.get("/backup")
def backup() -> Response:
    with tempfile.TemporaryDirectory(prefix="yiju-backup-") as directory:
        path = Path(directory) / "yiju.sqlite3"
        with sqlite3.connect(path) as destination:
            database().backup(destination)
        destination.close()
        data = io.BytesIO(path.read_bytes())
    return send_file(
        data,
        as_attachment=True,
        download_name=f"yiju-{now_utc():%Y%m%d-%H%M%S}.sqlite3",
        mimetype="application/vnd.sqlite3",
    )
