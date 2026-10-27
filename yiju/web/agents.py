from typing import Any, cast

from flask import Blueprint, Response, abort, jsonify, request, url_for

from yiju.core import dictionary
from yiju.core.definitions import brief_readings
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
from yiju.storage.names import pool
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


def _pending(touched: pending.Staged | None = None) -> dict[str, Any]:
    preview = pending.preview(database())
    result = preview.to_json()
    if touched is not None:
        edited = {
            word.word: word for word in preview.words if word.word in touched.words
        }
        shown_ids = set(touched.sentences).union(
            *(word.sentences for word in edited.values())
        )
        result["sentences"] = [
            sentence for sentence in result["sentences"] if sentence["id"] in shown_ids
        ]
        shown = {
            token["text"]
            for sentence in result["sentences"]
            for token in sentence["words"]
        }
        result["words"] = [
            word
            | (
                {"readings": brief_readings(edited[word["word"]].encoded)}
                if word["word"] in edited
                else {}
            )
            for word in result["words"]
            if word["word"] in shown or word["word"] in touched.words
        ]
        result["names"] = [
            name for name in result["names"] if name["id"] in touched.names
        ]
        if touched.waiting is not None:
            result["already_waiting"] = list(touched.waiting)
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
    if info is not None:
        return info
    readings = dictionary.lookup(word)
    if not readings:
        message = f"{word} is not in the library or CC-CEDICT."
        raise LookupError(message)
    return {
        "word": word,
        "status": "not in the library",
        "readings": brief_readings(dictionary.encode(readings)),
    }


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
    return _pending(pending.stage(database(), text))


@agents.post("/pending/sentences/<int:sentence_id>")
def edit_pending_sentence(sentence_id: int) -> dict[str, Any]:
    data = _body()
    words = _optional(data, "words", str)
    with pending.editing(database()):
        error = (
            None
            if words is None
            else pending.words_error(database(), sentence_id, words)
        )
        if error is not None:
            raise ValueError(error)
        pending.edit_sentence(
            database(),
            sentence_id,
            words=words,
            pinyin=_optional(data, "pinyin", str),
            translation=_optional(data, "translation", str),
            rules=_optional(data, "rules", str),
        )
    return _pending(pending.Staged(sentences=(sentence_id,)))


@agents.post("/pending/sentences/<int:sentence_id>/remove")
def remove_pending_sentence(sentence_id: int) -> dict[str, Any]:
    with pending.editing(database()):
        pending.remove_sentence(database(), sentence_id)
    return _pending(pending.Staged())


@agents.post("/pending/words/<word>")
def edit_pending_word(word: str) -> dict[str, Any]:
    data = _body()
    if word not in {item.word for item in pending.preview(database()).words}:
        message = f"{word} is not a new word waiting for a check."
        raise LookupError(message)
    pinyin = _optional(data, "pinyin", str)
    meanings = _optional(data, "meanings", str)
    if pinyin is not None and not (meanings or "").strip():
        message = (
            "A pinyin only goes with your own meanings, so give meanings too. "
            "To choose the reading a sentence uses, edit the sentence's pinyin."
        )
        raise ValueError(message)
    with pending.editing(database()):
        pending.edit_word(
            database(),
            word,
            pinyin=pinyin,
            meanings=meanings,
        )
    return _pending(pending.Staged(words=(word,)))


@agents.post("/pending/words/<word>/remove")
def remove_pending_word(word: str) -> dict[str, Any]:
    with pending.editing(database()):
        pending.remove_word(database(), word)
    return _pending(pending.Staged())


@agents.get("/names")
def names() -> list[dict[str, object]]:
    return [
        {
            "id": name.id,
            "chinese": name.chinese,
            "pinyin": name.pinyin,
            "english": name.english,
            "gender": name.gender,
        }
        for name in pool(database())
    ]


@agents.post("/pending/names")
def add_pending_names() -> dict[str, Any]:
    text = _optional(_body(), "text", str)
    if text is None:
        abort(400, "Missing text.")
    return _pending(pending.stage_names(database(), text))


@agents.post("/pending/names/<int:name_id>")
def edit_pending_name(name_id: int) -> dict[str, Any]:
    data = _body()
    with pending.editing(database()):
        pending.edit_name(
            database(),
            name_id,
            pinyin=_optional(data, "pinyin", str),
            english=_optional(data, "english", str),
            gender=_optional(data, "gender", str),
        )
    return _pending(pending.Staged(names=(name_id,)))


@agents.post("/pending/names/<int:name_id>/remove")
def remove_pending_name(name_id: int) -> dict[str, Any]:
    with pending.editing(database()):
        pending.remove_name(database(), name_id)
    return _pending(pending.Staged())
