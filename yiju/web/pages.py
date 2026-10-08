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
from yiju.core.selection import analyze
from yiju.storage.database import settings, vocabulary_words
from yiju.storage.learning import known_sentences, known_words
from yiju.storage.queue import overview

from . import database
from .studying import study_response

pages = Blueprint("pages", __name__)
PAGE_SIZE = 24


def _page_number() -> int:
    return max(1, min(1_000_000, request.args.get("page", 1, type=int)))


@pages.get("/")
def home() -> str:
    connection = database()
    now = now_utc()
    history = connection.execute(
        "SELECT * FROM imports ORDER BY id DESC LIMIT 5"
    ).fetchall()
    upcoming = connection.execute(
        "SELECT min(due) FROM cards WHERE reviews > 0 AND suspended = 0 AND due > ?",
        (now.isoformat(),),
    ).fetchone()[0]
    return render_template(
        "home.html", stats=overview(connection, now), history=history, upcoming=upcoming
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
    connection = database()
    state = request.args.get("status", "accepted")
    if state not in {"accepted", "rejected"}:
        abort(400)
    page = _page_number()
    sort = request.args.get("sort", "difficulty")
    if sort not in {"difficulty", "length", "source"}:
        abort(400)
    config = settings(connection)
    known = known_words(connection, now_utc())
    vocabulary = vocabulary_words(connection)
    rows = connection.execute(
        "SELECT s.*, e.word FROM sentences s JOIN entries e ON s.entry_id = e.id "
        "WHERE s.status = ? ORDER BY s.rowid",
        (state,),
    ).fetchall()
    items = [
        (
            row,
            analyze(
                tuple(json.loads(row["tokens"])),
                known,
                row["word"],
                int(config["max_unknown"]),
                include_zero=bool(config["include_zero"]),
                vocabulary=vocabulary,
            ),
        )
        for row in rows
    ]
    if sort == "difficulty":
        items.sort(key=lambda item: (-item[1].score, item[0]["id"]))
    elif sort == "length":
        items.sort(key=lambda item: (len(item[1].tokens), item[0]["id"]))
    counts = {
        row["status"]: row["n"]
        for row in connection.execute(
            "SELECT status, count(*) AS n FROM sentences GROUP BY status"
        )
    }
    return render_template(
        "sentences.html",
        items=items[(page - 1) * PAGE_SIZE : page * PAGE_SIZE],
        counts=counts,
        state=state,
        sort=sort,
        page=page,
        more=page * PAGE_SIZE < counts.get(state, 0),
        learned=known_sentences(connection, now_utc()),
    )


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
    connection = database()
    query = request.args.get("q", "").strip()[:100]
    page = _page_number()
    rows = connection.execute(
        "SELECT e.*, (SELECT sum(c2.reviews) FROM cards c2 "
        "WHERE c2.word = e.word AND c2.sentence_id IS NULL) AS reviews, "
        "c.suspended, c.id AS card_id, c.version, "
        "(SELECT group_concat(e2.definition_json, char(10)) "
        "FROM entries e2 WHERE e2.word = e.word) AS definitions_json, "
        "(SELECT group_concat(DISTINCT e2.pinyin) FROM entries e2 "
        "WHERE e2.word = e.word) AS all_pinyin "
        "FROM entries e JOIN cards c ON c.entry_id = e.id AND c.sentence_id IS NULL "
        "AND c.direction = 'recognition' "
        "WHERE instr(e.word, ?) > 0 OR EXISTS(SELECT 1 FROM entries e2 "
        "WHERE e2.word = e.word AND instr(lower(e2.definition), lower(?)) > 0) "
        "ORDER BY e.word LIMIT ? OFFSET ?",
        (query, query, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE),
    ).fetchall()
    return render_template(
        "library.html",
        entries=rows[:PAGE_SIZE],
        definitions={
            row["word"]: combine_definitions(row["definitions_json"])
            for row in rows[:PAGE_SIZE]
        },
        known=known_words(connection, now_utc()),
        q=query,
        page=page,
        more=len(rows) > PAGE_SIZE,
    )


@pages.get("/settings")
def preferences() -> str:
    connection = database()
    return render_template(
        "settings.html",
        config=settings(connection),
        stats=overview(connection, now_utc()),
    )


@pages.get("/healthz")
def health() -> Response:
    database().execute("SELECT 1")
    return Response("ok\n", mimetype="text/plain")
