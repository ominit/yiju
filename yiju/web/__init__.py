import os
import re
import secrets
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, cast

from flask import Flask, current_app, g, url_for
from markupsafe import Markup, escape

from yiju.core import dictionary
from yiju.core.names import SLOT, label, reading
from yiju.core.pronunciation import pronunciation
from yiju.storage.database import connect, initialize
from yiju.storage.pending import waiting

from .security import register_handlers

if TYPE_CHECKING:
    from flask.json.provider import DefaultJSONProvider


def database() -> sqlite3.Connection:
    if "database" not in g:
        g.database = connect(Path(current_app.config["DATABASE"]))
    return cast("sqlite3.Connection", g.database)


def _secret(path: Path) -> bytes:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes()
    with os.fdopen(descriptor, "wb") as stream:
        value = secrets.token_bytes(32)
        stream.write(value)
        return value


def _asset(filename: str) -> str:
    path = Path(current_app.static_folder or "") / filename
    return url_for("static", filename=filename, v=int(path.stat().st_mtime))


def slot_markup(text: str) -> Markup:
    parts = []
    position = 0
    for match in SLOT.finditer(text):
        shown, letter = label(match.group())
        parts += [
            escape(text[position : match.start()]),
            Markup('<span class="slot">{}{}</span>').format(
                shown, Markup("<sub>{}</sub>").format(letter) if letter else ""
            ),
        ]
        position = match.end()
    parts.append(escape(text[position:]))
    return Markup("").join(parts)


WORDS = re.compile(r"\s+|\S+")


def word_diff(before: str, after: str, side: str) -> Markup:
    old, new = WORDS.findall(before), WORDS.findall(after)
    wrap = Markup("<del>{}</del>") if side == "before" else Markup("<ins>{}</ins>")
    parts = []
    matcher = SequenceMatcher(None, old, new, autojunk=False)
    for op, start, end, first, last in matcher.get_opcodes():
        chunk = "".join(old[start:end] if side == "before" else new[first:last])
        core = chunk.strip()
        if op == "equal" or not core:
            parts.append(slot_markup(chunk))
            continue
        lead = chunk[: len(chunk) - len(chunk.lstrip())]
        trail = chunk[len(chunk.rstrip()) :]
        parts += [
            escape(lead),
            wrap.format(slot_markup(core)),
            escape(trail),
        ]
    return Markup("").join(parts)


def _close_database(_error: BaseException | None) -> None:
    connection = g.pop("database", None)
    if connection is not None:
        connection.close()


def _waiting_context() -> dict[str, int]:
    try:
        return {"waiting_count": waiting(database())}
    except sqlite3.Error:
        return {"waiting_count": 0}


def refresh_caches() -> None:
    current_app.extensions["yiju.prefill"].notify()


def create_app(database_path: Path | None = None, *, prefill: bool = False) -> Flask:
    app = Flask(__name__)
    cast("DefaultJSONProvider", app.json).sort_keys = False
    app.jinja_env.filters["pronunciation"] = pronunciation
    app.jinja_env.filters["reading"] = reading
    app.jinja_env.filters["slots"] = slot_markup
    app.jinja_env.filters["diff"] = word_diff
    app.add_template_global(_asset, "asset")
    path = (
        database_path or Path(os.environ.get("YIJU_DATA_DIR", "data")) / "yiju.sqlite3"
    )
    initialize(path)
    dictionary.use(path.parent / dictionary.FILENAME)
    app.config.update(
        DATABASE=str(path),
        SECRET_KEY=_secret(path.with_suffix(".secret")),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    register_handlers(app)
    app.context_processor(_waiting_context)
    app.teardown_appcontext(_close_database)
    from .actions import actions
    from .agents import agents
    from .audio import audio
    from .flags import flags
    from .install import install
    from .pages import pages
    from .prefill import Prefill
    from .strokes import strokes
    from .study import study

    app.extensions["yiju.prefill"] = Prefill(path, app.logger)
    if prefill:
        app.extensions["yiju.prefill"].start()

    app.register_blueprint(pages)
    app.register_blueprint(study)
    app.register_blueprint(actions)
    app.register_blueprint(agents)
    app.register_blueprint(audio)
    app.register_blueprint(flags)
    app.register_blueprint(install)
    app.register_blueprint(strokes)
    return app
