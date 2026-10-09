import json

from flask import (
    Blueprint,
    abort,
    render_template,
    request,
)
from werkzeug.wrappers import Response

from yiju.core.definitions import combine_definitions
from yiju.core.scheduling import now_utc
from yiju.storage.database import settings
from yiju.storage.progress import activity
from yiju.storage.queue import overview

from . import database
from .library import library_response
from .studying import study_response

pages = Blueprint("pages", __name__)


@pages.get("/")
def home() -> str:
    connection = database()
    now = now_utc()
    upcoming = connection.execute(
        "SELECT min(due) FROM cards WHERE reviews > 0 AND suspended = 0 AND due > ?",
        (now.isoformat(),),
    ).fetchone()[0]
    stats = overview(connection, now)
    learning = (
        connection.execute(
            "SELECT count(*) FROM knowledge WHERE word IN (SELECT word FROM entries)"
        ).fetchone()[0]
        - stats["known"]
    )
    return render_template(
        "home.html",
        stats=stats,
        upcoming=upcoming,
        activity=activity(connection, now),
        learning=learning,
        unstarted=max(0, stats["words"] - stats["known"] - learning),
    )


@pages.get("/study")
def study() -> str | Response:
    return study_response()


@pages.get("/import")
def import_page() -> str:
    return render_template("import.html")


@pages.get("/imports/<int:import_id>")
def import_result(import_id: int) -> str:
    row = (
        database()
        .execute("SELECT * FROM imports WHERE id = ?", (import_id,))
        .fetchone()
    )
    if row is None:
        abort(404)
    return render_template(
        "import_result.html", batch=row, summary=json.loads(row["summary"])
    )


@pages.get("/sentences")
def sentences() -> str:
    return library_response("sentences", now_utc())


@pages.get("/sentences/<sentence_id>")
def sentence_detail(sentence_id: str) -> str:
    row = (
        database()
        .execute(
            "SELECT s.*, e.word, e.raw_definition FROM sentences s JOIN "
            "entries e ON s.entry_id = e.id WHERE s.id = ?",
            (sentence_id,),
        )
        .fetchone()
    )
    if row is None:
        abort(404)
    return render_template(
        "sentence.html", sentence=row, return_to=request.args.get("return_to")
    )


@pages.get("/library")
def library() -> str:
    return library_response("words", now_utc())


@pages.get("/library/entries/<entry_id>/examples")
def library_examples(entry_id: str) -> str:
    rows = (
        database()
        .execute(
            "SELECT definition_json FROM entries WHERE word = "
            "(SELECT word FROM entries WHERE id = ?) ORDER BY rowid",
            (entry_id,),
        )
        .fetchall()
    )
    if not rows:
        abort(404)
    return render_template(
        "library_examples.html",
        value=combine_definitions("\n".join(row["definition_json"] for row in rows)),
    )


@pages.get("/settings")
def preferences() -> str:
    connection = database()
    return render_template(
        "settings.html",
        config=settings(connection),
    )


@pages.get("/healthz")
def health() -> Response:
    database().execute("SELECT 1")
    return Response("ok\n", mimetype="text/plain")
