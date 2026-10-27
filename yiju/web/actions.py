import io
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

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

from yiju.core.scheduling import now_utc
from yiju.storage.database import ConflictError, parameters, transaction
from yiju.storage.names import remove_name, update_name
from yiju.storage.pending import (
    FORMAT,
    NAME_FORMAT,
    FormatError,
    approve,
    approve_name,
    approve_sentence,
    discard,
    edit_name,
    edit_sentence,
    edit_word,
    editing,
    preview,
    remove_sentence,
    remove_word,
    stage,
    stage_names,
    waiting,
)
from yiju.storage.pending import (
    remove_name as remove_pending_name,
)
from yiju.storage.sentences import set_hidden, update_sentence
from yiju.storage.words import reclassify, set_manual, set_paused

from . import database, refresh_caches
from .library import item_response

if TYPE_CHECKING:
    from werkzeug.wrappers import Response

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
        name_format=NAME_FORMAT,
        errors=errors,
        chinese=chinese,
        translation=translation,
        text=request.form.get("text", ""),
    ), 400


@actions.post("/import/names")
def start_name_import() -> Response | tuple[str, int]:
    text = request.form.get("names", "")
    try:
        stage_names(database(), text)
    except FormatError as error:
        return render_template(
            "import.html",
            waiting=waiting(database()),
            format=FORMAT,
            name_format=NAME_FORMAT,
            errors=error.errors,
            names=text,
        ), 400
    return redirect(url_for("pages.pending_page"), code=303)


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
            rules=form.get(f"{prefix}-rules"),
        )
    for index, word in enumerate(json.loads(form.get("shown", "[]"))):
        prefix = f"w{index}"
        edit_word(
            connection,
            str(word),
            pinyin=form.get(f"{prefix}-pinyin"),
            meanings=form.get(f"{prefix}-meanings"),
        )
    for name_id in json.loads(form.get("names", "[]")):
        prefix = f"n{name_id}"
        edit_name(
            connection,
            int(name_id),
            pinyin=form.get(f"{prefix}-pinyin"),
            english=form.get(f"{prefix}-english"),
            gender=form.get(f"{prefix}-gender"),
        )


def _discard_item(connection: sqlite3.Connection) -> str | None:
    form = request.form
    if form.get("discard") is not None:
        remove_sentence(connection, integer("discard"))
        return "Discarded the sentence."
    if form.get("discard-name") is not None:
        remove_pending_name(connection, integer("discard-name"))
        return "Discarded the name."
    word = form.get("discard-word")
    if word is not None:
        remove_word(connection, word)
        return "Discarded the word."
    return None


def _added(added: dict[str, int]) -> str:
    parts = [
        f"{added[kind]} {kind if added[kind] != 1 else kind[:-1]}"
        for kind in ("sentences", "words", "names")
        if added[kind] or kind != "names"
    ]
    return f"Added {', '.join(parts[:-1])} and {parts[-1]}."


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
            discarded = _discard_item(connection)
        if discarded is not None:
            flash(discarded)
            return redirect(location, code=303)
        single = request.form.get("approve")
        message = None
        if request.form.get("approve-name") is not None:
            approve_name(
                connection,
                preview(connection).version,
                integer("approve-name"),
                now_utc(),
            )
            message = "Added the name."
        elif step == "approve" or single is not None:
            message = _approve_items(connection, single)
    except (ConflictError, LookupError) as error:
        flash(str(error))
        return redirect(location, code=303)
    if message is not None:
        refresh_caches()
        flash(message)
    return redirect(location, code=303)


def _approve_items(connection: sqlite3.Connection, single: str | None) -> str | None:
    result = preview(connection)
    words = {word.word for word in result.words}
    sentence = None
    if single is not None:
        sentence_id = integer("approve")
        sentence = next(
            (item for item in result.sentences if item.id == sentence_id), None
        )
        if sentence is None:
            flash("That sentence is no longer waiting for your check.")
            return None
        words &= {token.text for token in sentence.words}
    if words - set(json.loads(request.form.get("shown", "[]"))):
        flash("The word list changed. Check the new words, then approve again.")
        return None
    if sentence is None:
        return _added(approve(connection, result.version, now_utc()))
    added = approve_sentence(connection, result.version, sentence.id, now_utc())
    count = added["words"]
    return f"Added {sentence.chinese}" + (
        f" with {count} new word{'' if count == 1 else 's'}." if count else ""
    )


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
                request.form.get("rules"),
            )
    except ConflictError:
        raise
    except ValueError as error:
        abort(400, str(error))
    refresh_caches()
    return item_response("sentences", str(sentence_id), now_utc())


@actions.post("/names/<int:name_id>")
def save_name(name_id: int) -> Response:
    connection = database()
    try:
        if request.form.get("remove") is not None:
            removed = remove_name(connection, name_id, integer("version"))
            flash(f"Removed {removed}.")
        else:
            update_name(
                connection,
                name_id,
                integer("version"),
                request.form.get("pinyin", ""),
                request.form.get("english", ""),
                request.form.get("gender", ""),
            )
            flash("Saved the name.")
    except ConflictError:
        raise
    except ValueError as error:
        abort(400, str(error))
    return redirect(url_for("pages.names"), code=303)


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
