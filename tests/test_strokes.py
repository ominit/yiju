import asyncio
import io
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import override

import pytest
from conftest import csrf
from flask import Flask
from flask.testing import FlaskClient

from yiju.storage.database import connect
from yiju.web import strokes


@dataclass
class Glyph:
    classes: str
    text: str = ""
    paths: list[dict[str, str | None]] = field(default_factory=list)


class GlyphParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.glyphs: list[Glyph] = []
        self.character: Glyph | None = None

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = attributes.get("class") or ""
        if tag == "span" and "character" in classes.split():
            self.character = Glyph(classes)
            self.glyphs.append(self.character)
        elif tag == "path" and self.character is not None:
            self.character.paths.append(attributes)

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag == "span":
            self.character = None

    @override
    def handle_data(self, data: str) -> None:
        if self.character is not None:
            self.character.text += data


@pytest.fixture
def fetched_characters(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def fake_character(character: str) -> dict[str, object]:
        calls.append(character)
        await asyncio.sleep(0.02)
        return {"strokes": ["M 0 0 L 10 10"], "medians": [[[0, 0], [10, 10]]]}

    monkeypatch.setattr(strokes, "fetch_character", fake_character)
    return calls


def test_character_data_is_cached_on_server_and_shared_between_cards(
    client: FlaskClient, app: Flask, fetched_characters: list[str]
) -> None:
    for _ in range(2):
        response = client.get("/strokes/characters/去")
        assert response.status_code == 200
        assert response.json is not None
        assert response.json["strokes"] == ["M 0 0 L 10 10"]
    assert fetched_characters == ["去"]
    directory = Path(app.config["DATABASE"]).parent / "strokes"
    assert len(list(directory.glob("*.json"))) == 1


def test_concurrent_character_requests_fetch_once(
    tmp_path: Path, fetched_characters: list[str]
) -> None:
    directory = tmp_path / "strokes"
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(
            pool.map(lambda _: strokes.cached_character("我", directory), range(4))
        )
    assert len(set(paths)) == 1
    assert fetched_characters == ["我"]
    assert len(list(directory.iterdir())) == 1


@pytest.mark.parametrize("value", ["a", "ab", "去我", ".", "。"])
def test_only_single_chinese_characters_are_requested(
    client: FlaskClient, fetched_characters: list[str], value: str
) -> None:
    assert client.get(f"/strokes/characters/{value}").status_code == 404
    assert fetched_characters == []


@pytest.mark.parametrize(
    ("error", "status"), [(LookupError("missing"), 404), (OSError("offline"), 503)]
)
def test_failed_character_fetches_are_retryable_and_not_cached(
    client: FlaskClient,
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    status: int,
) -> None:
    calls = 0

    async def unavailable(_character: str) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(strokes, "fetch_character", unavailable)
    for _ in range(2):
        response = client.get(
            "/strokes/characters/去", headers={"X-Yiju-Partial": "page"}
        )
        assert response.status_code == status
        assert response.json is not None
        assert "unavailable" in response.json["error"]
    assert calls == 2
    assert not (Path(app.config["DATABASE"]).parent / "strokes").exists()


def test_study_cached_glyphs_render_without_fetching_or_polluting_character_text(
    client: FlaskClient,
    app: Flask,
    xml_export: bytes,
    fetched_characters: list[str],
) -> None:
    token = csrf(client)
    client.post(
        "/import", data={"csrf": token, "file": (io.BytesIO(xml_export), "words.xml")}
    )
    directory = Path(app.config["DATABASE"]).parent / "strokes"
    strokes.cached_character("去", directory)
    with connect(Path(app.config["DATABASE"])) as db:
        card = db.execute(
            "SELECT id FROM cards WHERE word = '去' AND sentence_id IS NULL "
            "AND direction = 'recognition'"
        ).fetchone()[0]
    parser = GlyphParser()
    parser.feed(client.get(f"/study?card={card}").data.decode())
    rendered = [glyph for glyph in parser.glyphs if "stroke-rendered" in glyph.classes]
    assert rendered
    assert all(glyph.text == "去" for glyph in rendered)
    assert all(glyph.paths == [{"d": "M 0 0 L 10 10"}] for glyph in rendered)
    assert fetched_characters == ["去"]


def test_study_marks_uncached_characters_without_network_fetching(
    client: FlaskClient, app: Flask, xml_export: bytes, fetched_characters: list[str]
) -> None:
    token = csrf(client)
    client.post(
        "/import", data={"csrf": token, "file": (io.BytesIO(xml_export), "words.xml")}
    )
    with connect(Path(app.config["DATABASE"])) as db:
        card = db.execute(
            "SELECT id FROM cards WHERE word = '去' AND sentence_id IS NULL "
            "AND direction = 'recognition'"
        ).fetchone()[0]
    parser = GlyphParser()
    parser.feed(client.get(f"/study?card={card}").data.decode())
    pending = [glyph for glyph in parser.glyphs if "stroke-pending" in glyph.classes]
    assert any(glyph.text == "去" for glyph in pending)
    assert not any("stroke-rendered" in glyph.classes for glyph in parser.glyphs)
    assert fetched_characters == []


def test_cached_glyph_paths_are_escaped_and_invalid_cache_does_not_break_study(
    client: FlaskClient, app: Flask, xml_export: bytes
) -> None:
    token = csrf(client)
    client.post(
        "/import", data={"csrf": token, "file": (io.BytesIO(xml_export), "words.xml")}
    )
    directory = Path(app.config["DATABASE"]).parent / "strokes"
    directory.mkdir()
    path = directory / f"{strokes.DATA_VERSION}-{ord('去'):x}.json"
    unsafe = 'M 0 0" onload="alert(1)'
    path.write_text(json.dumps({"strokes": [unsafe]}))
    with connect(Path(app.config["DATABASE"])) as db:
        card = db.execute(
            "SELECT id FROM cards WHERE word = '去' AND sentence_id IS NULL "
            "AND direction = 'recognition'"
        ).fetchone()[0]
    parser = GlyphParser()
    parser.feed(client.get(f"/study?card={card}").data.decode())
    rendered = [glyph for glyph in parser.glyphs if "stroke-rendered" in glyph.classes]
    assert rendered
    assert all(glyph.paths == [{"d": unsafe}] for glyph in rendered)
    path.write_text("invalid")
    response = client.get(f"/study?card={card}")
    assert response.status_code == 200
    parser = GlyphParser()
    parser.feed(response.data.decode())
    assert not any("stroke-rendered" in glyph.classes for glyph in parser.glyphs)
