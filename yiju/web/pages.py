from datetime import timedelta
from typing import Any

from flask import Blueprint, redirect, render_template, request
from werkzeug.wrappers import Response

from yiju.core.dictionary import published
from yiju.core.scheduling import now_utc
from yiju.storage.database import settings
from yiju.storage.pending import (
    FORMAT,
    NAME_FORMAT,
    Preview,
    preview,
    waiting,
)
from yiju.storage.queue import overview
from yiju.storage.stats import activity

from . import database
from .library import item_location, item_response, library_counts, library_response

pages = Blueprint("pages", __name__)
FORECAST_DAYS = 7
CHECK_TABS = {
    "sentences": "Sentences",
    "words": "Words",
    "names": "Names",
    "edits": "Edits",
    "removals": "Removals",
}


@pages.get("/")
def home() -> str:
    connection = database()
    now = now_utc()
    stats = overview(connection, now)
    due_by_day = dict(
        connection.execute(
            "SELECT substr(due, 1, 10) AS day, count(*) FROM cards "
            "WHERE reviews > 0 AND suspended = 0 AND due > ? AND due < ? "
            "GROUP BY day",
            (
                now.isoformat(),
                (now.date() + timedelta(days=FORECAST_DAYS)).isoformat(),
            ),
        ).fetchall()
    )
    forecast = [
        {
            "date": day,
            "count": due_by_day.get(day.isoformat(), 0)
            + (stats["due"] if offset == 0 else 0),
        }
        for offset in range(FORECAST_DAYS)
        for day in [now.date() + timedelta(days=offset)]
    ]
    upcoming = connection.execute(
        "SELECT min(due) FROM cards WHERE reviews > 0 AND suspended = 0 AND due > ?",
        (now.isoformat(),),
    ).fetchone()[0]
    return render_template(
        "home.html",
        stats=stats,
        activity=activity(connection, now),
        forecast=forecast,
        forecast_max=max(1, *(day["count"] for day in forecast)),
        upcoming=upcoming,
    )


@pages.get("/import")
def import_page() -> str:
    return render_template(
        "import.html",
        waiting=waiting(database()),
        format=FORMAT,
        name_format=NAME_FORMAT,
    )


def check_tabs(result: Preview) -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "label": label,
            "items": items,
            "problems": result.problems[key],
        }
        for key, label in CHECK_TABS.items()
        if (items := result.section(key))
    ]


@pages.get("/pending")
def pending_page() -> str:
    result = preview(database())
    tabs = check_tabs(result)
    keys = [tab["key"] for tab in tabs]
    wanted = request.args.get("tab", "")
    return render_template(
        "pending.html",
        pending=result,
        tabs=tabs,
        selected=wanted if wanted in keys else next(iter(keys), ""),
        word_index={word.word: index for index, word in enumerate(result.words)},
        new_words={word.word: word for word in result.words},
    )


@pages.get("/library")
def library() -> str:
    return library_response("words", now_utc())


@pages.get("/sentences")
def sentences() -> str:
    return library_response("sentences", now_utc())


def library_item(kind: str, key: str) -> str | Response:
    if request.headers.get("X-Fragment") is not None:
        return item_response(kind, key, now_utc())
    return redirect(item_location(kind, key, now_utc()))


@pages.get("/library/names")
def names() -> str:
    connection = database()
    return render_template(
        "names.html",
        names=connection.execute("SELECT * FROM names ORDER BY id").fetchall(),
        counts=library_counts(connection),
    )


@pages.get("/library/words/<word>")
def word_detail(word: str) -> str | Response:
    return library_item("words", word)


@pages.get("/sentences/<sentence_id>")
def sentence_detail(sentence_id: str) -> str | Response:
    return library_item("sentences", sentence_id)


@pages.get("/settings")
def preferences() -> str:
    return render_template("settings.html", config=settings(database()))


@pages.get("/about")
def about() -> str:
    return render_template("about.html", dictionary_date=published())


@pages.get("/healthz")
def health() -> Response:
    database().execute("SELECT 1")
    return Response("ok\n", mimetype="text/plain")
