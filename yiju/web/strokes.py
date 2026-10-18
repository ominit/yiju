import asyncio
import json
import os
import tempfile
from http import HTTPStatus
from pathlib import Path
from threading import Lock
from typing import cast
from urllib.parse import quote

from aiohttp import ClientError, ClientSession, ClientTimeout
from flask import Blueprint, Response, abort, current_app, send_file

from yiju.core.text import contains_han

strokes = Blueprint("strokes", __name__)
DATA_VERSION = "2.0.1"
_locks = tuple(Lock() for _ in range(16))
ERRORS = (OSError, TimeoutError, ClientError, ValueError)


async def fetch_character(character: str) -> dict[str, object]:
    url = (
        f"https://cdn.jsdelivr.net/npm/hanzi-writer-data@{DATA_VERSION}/"
        f"{quote(character)}.json"
    )
    async with (
        ClientSession(timeout=ClientTimeout(total=15, connect=5)) as session,
        session.get(url) as response,
    ):
        if response.status == HTTPStatus.NOT_FOUND:
            raise LookupError(character)
        response.raise_for_status()
        data = await response.json()
    if (
        not isinstance(data, dict)
        or not data.get("strokes")
        or len(data["strokes"]) != len(data.get("medians", []))
    ):
        message = "Invalid stroke-order data."
        raise ValueError(message)
    return cast("dict[str, object]", data)


def character_path(character: str, directory: Path) -> Path:
    return directory / f"{DATA_VERSION}-{ord(character):x}.json"


def cached_character(character: str, directory: Path) -> Path:
    destination = character_path(character, directory)
    with _locks[ord(character) % len(_locks)]:
        if destination.is_file():
            return destination
        data = asyncio.run(fetch_character(character))
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(suffix=".json", dir=directory)
        staging = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False)
            staging.replace(destination)
        finally:
            staging.unlink(missing_ok=True)
    return destination


@strokes.get("/strokes/characters/<character>")
def character_strokes(character: str) -> Response:
    if len(character) != 1 or not contains_han(character):
        abort(404)
    try:
        path = cached_character(
            character, Path(current_app.config["DATABASE"]).resolve().parent / "strokes"
        )
    except LookupError:
        abort(404, "Stroke order is unavailable for this character.")
    except ERRORS:
        current_app.logger.exception("Could not load stroke-order data")
        abort(503, "Stroke order is unavailable. Try again.")
    return send_file(path, mimetype="application/json", conditional=True)
