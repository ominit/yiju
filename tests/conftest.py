import io
import re
import sqlite3
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from flask import Flask, template_rendered
from flask.testing import FlaskClient
from fsrs import Card, State
from werkzeug.test import TestResponse

from yiju.storage.database import connect, initialize
from yiju.web import create_app

ITEM_URL = re.compile(r"^/(?:library/words|sentences)/[^/]+$")


def seed_known_word(
    connection: sqlite3.Connection, word: str, now: datetime | None = None
) -> None:
    now = now or datetime.now(UTC)
    card = Card(
        card_id=1,
        state=State.Review,
        stability=20,
        difficulty=3,
        due=now,
        last_review=now,
    )
    connection.execute(
        "INSERT INTO knowledge VALUES (?, ?, ?, ?)",
        (word, card.to_json(), 3, now.date().isoformat()),
    )
    connection.commit()


@pytest.fixture
def xml_export() -> bytes:
    return """<?xml version="1.0" encoding="UTF-8"?>
<plecoflash formatversion="2"><cards>
<card language="chinese"><entry><headword charset="sc">我</headword>
<pron type="hypy">wo3</pron><defn>pronoun I; me 我去。 Wǒ qù. I am going.</defn>
</entry><dictref dictid="TEST" entryid="1"/></card>
<card language="chinese"><entry><headword charset="sc">去</headword>
<pron type="hypy">qu4</pron><defn>verb go 我去。 Wǒ qù. I am going.</defn>
</entry><dictref dictid="TEST" entryid="2"/></card>
<card language="chinese"><entry><headword charset="sc">学习</headword>
<pron type="hypy">xue2xi2</pron><defn>verb study
我学习中文。 Wǒ xuéxí Zhōngwén. I study Chinese.</defn>
</entry><dictref dictid="TEST" entryid="3"/></card>
</cards></plecoflash>""".encode()


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


def csrf(client: FlaskClient) -> str:
    client.get("/")
    with client.session_transaction() as session:
        return str(session["csrf"])


def import_export(
    client: FlaskClient, data: bytes, token: str | None = None
) -> TestResponse:
    return client.post(
        "/import",
        data={"csrf": token or csrf(client), "file": (io.BytesIO(data), "words.xml")},
    )


def post_json(
    client: FlaskClient, url: str, payload: dict[str, object], token: str
) -> TestResponse:
    return client.post(url, json=payload, headers={"X-CSRF-Token": token})


def rate(
    client: FlaskClient, token: str, card: sqlite3.Row, rating: int = 3
) -> TestResponse:
    return post_json(
        client,
        "/api/reviews",
        {"card": card["id"], "version": card["version"], "rating": rating},
        token,
    )


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
