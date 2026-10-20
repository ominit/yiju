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
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.wrappers import Response

from yiju.core.scheduling import now_utc
from yiju.storage.database import ConflictError, parameters, transaction
from yiju.storage.pending import (
    FORMAT,
    FormatError,
    approve,
    approve_sentence,
    discard,
    edit_sentence,
    edit_word,
    editing,
    preview,
    stage,
    waiting,
)
from yiju.storage.sentences import set_hidden, update_sentence
from yiju.storage.words import reclassify, set_manual, set_paused

from . import database, refresh_caches
from .library import item_response

actions = Blueprint("actions", __name__)
SETTING_RANGES = {
    "new_per_day": (0, 200),
    "retention": (0.7, 0.99),
    "learned_stability": (1.0, 365.0),
    "unlearned_stability": (0.5, 365.0),
    "learned_days": (1, 30),
    "sentence_pass_weight": (0.0, 1.0),
    "sentence_fail_weight": (0.0, 1.0),
}


def integer(name: str) -> int:
    value = request.form.get(name, type=int)
    if value is None:
        abort(400, f"Missing or invalid {name}.")
    return value


def _one_line(value: str) -> str:
    return " ".join(value.split())


@actions.post("/import")
def start_import() -> Response | tuple[str, int]:
    chinese = request.form.get("chinese", "")
    translation = request.form.get("translation", "")
    text = request.form.get("text", "")
    errors = []
    if chinese.strip() and not translation.strip():
        errors = ["Add a translation for the sentence."]
    else:
        if chinese.strip():
            text = f"{_one_line(chinese)}\t{_one_line(translation)}"
        try:
            stage(database(), text)
        except FormatError as error:
            errors = error.errors
        else:
            return redirect(url_for("pages.pending_page"), code=303)
    return render_template(
        "import.html",
        waiting=waiting(database()),
        format=FORMAT,
        errors=errors,
        chinese=chinese,
        translation=translation,
        text=request.form.get("text", ""),
    ), 400


def _apply_edits(connection: sqlite3.Connection) -> None:
    form = request.form
    for sentence_id in json.loads(form.get("sentences", "[]")):
        prefix = f"s{sentence_id}"
        edit_sentence(
            connection,
            int(sentence_id),
            words=form.get(f"{prefix}-words"),
            pinyin=form.get(f"{prefix}-pinyin"),
            translation=form.get(f"{prefix}-translation"),
            included=form.get(f"{prefix}-included") == "on",
        )
    for index, word in enumerate(json.loads(form.get("shown", "[]"))):
        prefix = f"w{index}"
        edit_word(
            connection,
            str(word),
            pinyin=form.get(f"{prefix}-pinyin"),
            meanings=form.get(f"{prefix}-meanings"),
            included=form.get(f"{prefix}-included") == "on",
        )


@actions.post("/pending")
def save_pending() -> Response:
    connection = database()
    step = request.form.get("step", "save")
    location = url_for("pages.pending_page")
    checked = request.form.get("version", "")
    try:
        if step == "discard":
            discard(connection, checked)
            flash("Discarded everything waiting for your check.")
            return redirect(url_for("pages.import_page"), code=303)
        with editing(connection, checked):
            _apply_edits(connection)
        single = request.form.get("approve")
        if step != "approve" and single is None:
            return redirect(location, code=303)
        result = preview(connection)
        words = {word.word for word in result.words if word.included}
        sentence = None
        if single is not None:
            sentence_id = integer("approve")
            sentence = next(
                (item for item in result.sentences if item.id == sentence_id), None
            )
            if sentence is None:
                flash("That sentence is no longer waiting for your check.")
                return redirect(location, code=303)
            words &= {token.text for token in sentence.words}
        if words - set(json.loads(request.form.get("shown", "[]"))):
            flash("The word list changed. Check the new words, then approve again.")
            return redirect(location, code=303)
        if sentence is None:
            added = approve(connection, result.version, now_utc())
            message = (
                f"Added {added['sentences']} sentences and {added['words']} words."
            )
        else:
            added = approve_sentence(connection, result.version, sentence.id, now_utc())
            count = added["words"]
            message = f"Added {sentence.chinese}" + (
                f" with {count} new word{'' if count == 1 else 's'}." if count else ""
            )
    except ConflictError as error:
        flash(str(error))
        return redirect(location, code=303)
    refresh_caches()
    flash(message)
    return redirect(location, code=303)


@actions.post("/words/<word>/pause")
def pause_word(word: str) -> str:
    set_paused(
        database(), word, integer("version"), paused=request.form.get("paused") == "1"
    )
    return item_response("words", word, now_utc())


@actions.post("/words/<word>/unlock")
def unlock_word(word: str) -> str:
    set_manual(
        database(), word, integer("version"), manual=request.form.get("manual") == "1"
    )
    return item_response("words", word, now_utc())


@actions.post("/sentences/<int:sentence_id>")
def save_sentence(sentence_id: int) -> str:
    connection = database()
    hidden = request.form.get("hidden")
    try:
        if hidden is not None:
            set_hidden(
                connection, sentence_id, integer("version"), hidden=hidden == "1"
            )
        else:
            update_sentence(
                connection,
                sentence_id,
                integer("version"),
                request.form.get("pinyin", ""),
                request.form.get("translation", ""),
            )
    except ConflictError:
        raise
    except ValueError as error:
        abort(400, str(error))
    refresh_caches()
    return item_response("sentences", str(sentence_id), now_utc())


@actions.post("/settings")
def save_settings() -> Response:
    values: dict[str, int | float] = {}
    for key, (low, high) in SETTING_RANGES.items():
        value = request.form.get(key, type=float)
        if value is None or not low <= value <= high:
            abort(400, f"{key} must be between {low} and {high}.")
        if isinstance(low, int) and not value.is_integer():
            abort(400, f"{key} must be a whole number.")
        values[key] = int(value) if isinstance(low, int) else value
    if values["unlearned_stability"] > values["learned_stability"]:
        abort(400, "A word must drop out below the stability it needs to be learned.")
    connection = database()
    with transaction(connection):
        connection.executemany(
            "UPDATE settings SET value = ? WHERE key = ?",
            [(json.dumps(value), key) for key, value in values.items()],
        )
        reclassify(connection, parameters(connection))
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
