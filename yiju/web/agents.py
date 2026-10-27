from typing import Any, cast

from flask import Blueprint, Response, abort, jsonify, request, url_for

from yiju.core import dictionary
from yiju.core.scheduling import now_utc
from yiju.storage import pending
from yiju.storage.database import ConflictError
from yiju.storage.export import (
    SENTENCE_STATUSES,
    WORD_STATUSES,
    flagged_words,
    list_sentences,
    list_words,
    review_log,
    word_info,
)
from yiju.storage.queue import overview

from . import database
from .security import conflict

agents = Blueprint("agents", __name__, url_prefix="/api/agent")


@agents.errorhandler(pending.FormatError)
def format_error(error: pending.FormatError) -> tuple[Response, int]:
    return jsonify(
        error="The text is not in the import format.", lines=error.errors
    ), 400


@agents.errorhandler(LookupError)
def missing(error: LookupError) -> tuple[Response, int]:
    return jsonify(error=str(error)), 404


@agents.errorhandler(ValueError)
def invalid(error: ValueError) -> tuple[str | Response, int]:
    if isinstance(error, ConflictError):
        return conflict(error)
    return jsonify(error=str(error)), 400


def _body() -> dict[str, object]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, "Expected a JSON body.")
    return cast("dict[str, object]", data)


def _optional[T](data: dict[str, object], name: str, kind: type[T]) -> T | None:
    value = data.get(name)
    if value is not None and not isinstance(value, kind):
        abort(400, f"Invalid {name}.")
    return value


def _status(choices: tuple[str, ...]) -> str | None:
    value = request.args.get("status")
    if value is not None and value not in choices:
        abort(400, f"Status must be one of {', '.join(choices)}.")
    return value


def _flag(name: str) -> bool:
    return request.args.get(name, "") not in {"", "0", "false"}


def _pending() -> dict[str, Any]:
    result = pending.preview(database()).to_json()
    result["check_screen"] = url_for("pages.pending_page")
    return result


@agents.get("/stats")
def stats() -> dict[str, int]:
    return overview(database(), now_utc())


@agents.get("/words")
def words() -> list[dict[str, object]]:
    return list_words(
        database(),
        word_status=_status(WORD_STATUSES),
        flagged=_flag("flagged"),
        backlog=_flag("backlog"),
    )


@agents.get("/words/<word>")
def word(word: str) -> dict[str, object]:
    info = word_info(database(), word, now_utc())
    if info is None:
        message = f"{word} is not in the library."
        raise LookupError(message)
    return info


@agents.get("/sentences")
def sentences() -> list[dict[str, object]]:
    return list_sentences(database(), _status(SENTENCE_STATUSES))


@agents.get("/flags")
def flags() -> list[dict[str, object]]:
    return flagged_words(database())


@agents.get("/reviews")
def reviews() -> list[dict[str, object]]:
    return review_log(database(), request.args.get("since"))


@agents.get("/dictionary")
def dictionary_status() -> dict[str, object]:
    dictionary.ensure()
    return {"path": str(dictionary.SOURCE.path), "published": dictionary.published()}


@agents.post("/dictionary")
def dictionary_update() -> dict[str, object]:
    published = dictionary.update()
    return {"path": str(dictionary.SOURCE.path), "published": published}


@agents.get("/pending")
def show_pending() -> dict[str, Any]:
    return _pending()


@agents.post("/pending")
def add_pending() -> dict[str, Any]:
    text = _optional(_body(), "text", str)
    if text is None:
        abort(400, "Missing text.")
    pending.stage(database(), text)
    return _pending()


@agents.post("/pending/sentences/<int:sentence_id>")
def edit_pending_sentence(sentence_id: int) -> dict[str, Any]:
    data = _body()
    with pending.editing(database()):
        pending.edit_sentence(
            database(),
            sentence_id,
            words=_optional(data, "words", str),
            pinyin=_optional(data, "pinyin", str),
            translation=_optional(data, "translation", str),
            included=_optional(data, "included", bool),
        )
    return _pending()


@agents.post("/pending/sentences/<int:sentence_id>/remove")
def remove_pending_sentence(sentence_id: int) -> dict[str, Any]:
    with pending.editing(database()):
        pending.remove_sentence(database(), sentence_id)
    return _pending()


@agents.post("/pending/words/<word>")
def edit_pending_word(word: str) -> dict[str, Any]:
    data = _body()
    if word not in {item.word for item in pending.preview(database()).words}:
        message = f"{word} is not a new word waiting for a check."
        raise LookupError(message)
    with pending.editing(database()):
        pending.edit_word(
            database(),
            word,
            pinyin=_optional(data, "pinyin", str),
            meanings=_optional(data, "meanings", str),
            included=_optional(data, "included", bool),
        )
    return _pending()
