import gzip
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import pytest
from flask import Flask, template_rendered
from fsrs import Card, State

from yiju.core import dictionary
from yiju.storage.database import connect, initialize
from yiju.storage.graph import refresh
from yiju.storage.pending import approve, preview, stage
from yiju.web import create_app

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Generator

    from flask.testing import FlaskClient
    from werkzeug.test import TestResponse

DICTIONARY = Path(__file__).with_name("cedict.u8")
ITEM_URL = re.compile(r"^/(?:library/words|sentences)/[^/]+$")
SAMPLE = """\
我去。 | I am going.
我学习中文。 | I study Chinese.
苹果
"""
WORDS = {"我", "去", "学习", "中文", "苹果"}
SENTENCES = {"我去。", "我学习中文。"}


def add(
    connection: sqlite3.Connection, text: str, now: datetime | None = None
) -> dict[str, int]:
    now = now or datetime.now(UTC)
    stage(connection, text)
    return approve(connection, preview(connection).version, now)


def set_strength(
    connection: sqlite3.Connection,
    word: str,
    *,
    stability: float = 20,
    learned: bool = True,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    memory = Card(
        card_id=0,
        state=State.Review,
        stability=stability,
        difficulty=5,
        due=now,
        last_review=now,
    )
    connection.execute(
        "UPDATE words SET state = 'seen', memory = ?, success_days = ?, "
        "last_success = ?, learned = ? WHERE word = ?",
        (memory.to_json(), 3, now.date().isoformat(), int(learned), word),
    )
    refresh(connection, [word])
    connection.commit()


def states(connection: sqlite3.Connection, table: str = "words") -> dict[str, str]:
    key = "word" if table == "words" else "chinese"
    return {
        row[0]: row[1]
        for row in connection.execute(f"SELECT {key}, state FROM {table}")  # noqa: S608
    }


@pytest.fixture(autouse=True)
def small_dictionary(tmp_path: Path) -> None:
    path = tmp_path / dictionary.FILENAME
    path.write_bytes(gzip.compress(DICTIONARY.read_bytes()))
    dictionary.use(path)


@pytest.fixture
def connection(tmp_path: Path) -> Generator[sqlite3.Connection]:
    path = tmp_path / "test.sqlite3"
    initialize(path)
    db = connect(path)
    yield db
    db.close()


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    app = create_app(tmp_path / "web.sqlite3")
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


def database(app: Flask) -> sqlite3.Connection:
    return connect(Path(app.config["DATABASE"]))


def csrf(client: FlaskClient) -> str:
    client.get("/")
    with client.session_transaction() as session:
        return str(session["csrf"])


def check_form(client: FlaskClient) -> dict[str, str]:
    with database(client.application) as db:
        waiting = preview(db)
    form = {
        "csrf": csrf(client),
        "version": waiting.version,
        "sentences": json.dumps([sentence.id for sentence in waiting.sentences]),
        "shown": json.dumps([word.word for word in waiting.words]),
    }
    for sentence in waiting.sentences:
        prefix = f"s{sentence.id}"
        form[f"{prefix}-words"] = " ".join(token.text for token in sentence.words)
        form[f"{prefix}-pinyin"] = sentence.pinyin
        form[f"{prefix}-translation"] = sentence.translation
    for index, word in enumerate(waiting.words):
        form[f"w{index}-pinyin"] = word.pinyin
        form[f"w{index}-meanings"] = word.meanings
    return form


def start_import(client: FlaskClient, text: str) -> None:
    response = client.post("/import", data={"csrf": csrf(client), "text": text})
    assert response.status_code == 303
    assert response.headers["Location"] == "/pending"


def import_text(client: FlaskClient, text: str = SAMPLE) -> TestResponse:
    start_import(client, text)
    response = client.post("/pending", data={**check_form(client), "step": "approve"})
    assert response.status_code == 303
    return response


def post_json(
    client: FlaskClient, url: str, payload: dict[str, object], token: str
) -> TestResponse:
    return client.post(url, json=payload, headers={"X-CSRF-Token": token})


def rate(
    client: FlaskClient,
    token: str,
    card: dict[str, Any] | sqlite3.Row,
    rating: int = 3,
    marked: list[str] | None = None,
) -> TestResponse:
    payload: dict[str, object] = {
        "card": card["id"],
        "version": card["version"],
        "rating": rating,
    }
    if marked is not None:
        payload["marked"] = marked
    return post_json(client, "/api/reviews", payload, token)


def study_cards(client: FlaskClient) -> list[dict[str, Any]]:
    batch = client.get("/api/study").json
    assert batch is not None
    return batch["cards"]


def page_context(client: FlaskClient, url: str) -> dict[str, Any]:
    recorded: list[dict[str, Any]] = []

    def record(_sender: Flask, context: dict[str, Any], **_extra: object) -> None:
        recorded.append(context)

    headers = {"X-Fragment": "1"} if ITEM_URL.match(urlsplit(url).path) else {}
    with template_rendered.connected_to(record, client.application):
        response = client.get(url, headers=headers)
    assert response.status_code == 200
    assert len(recorded) == 1
    return recorded[0]
