import os
import secrets
import sqlite3
from pathlib import Path
from typing import cast

from flask import Flask, current_app, g, url_for

from yiju.core import dictionary
from yiju.core.pronunciation import pronunciation
from yiju.storage.database import connect, initialize

from .security import register_handlers


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


def _close_database(_error: BaseException | None) -> None:
    connection = g.pop("database", None)
    if connection is not None:
        connection.close()


def refresh_caches() -> None:
    current_app.extensions["yiju.prefill"].notify()


def create_app(database_path: Path | None = None, *, prefill: bool = False) -> Flask:
    app = Flask(__name__)
    app.jinja_env.filters["pronunciation"] = pronunciation
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
