import os
import secrets
import sqlite3
from pathlib import Path
from typing import cast

from flask import Flask, current_app, g

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


def _close_database(_error: BaseException | None) -> None:
    connection = g.pop("database", None)
    if connection is not None:
        connection.close()


def create_app(database_path: Path | None = None) -> Flask:
    app = Flask(__name__)
    path = (
        database_path or Path(os.environ.get("YIJU_DATA_DIR", "data")) / "yiju.sqlite3"
    )
    initialize(path)
    app.config.update(
        DATABASE=str(path),
        SECRET_KEY=_secret(path.with_suffix(".secret")),
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    register_handlers(app)
    app.teardown_appcontext(_close_database)
    from .actions import actions
    from .pages import pages

    app.register_blueprint(pages)
    app.register_blueprint(actions)
    return app
