import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

from yiju.storage.database import connect, initialize
from yiju.web import create_app


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
