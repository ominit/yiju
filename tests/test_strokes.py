import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

from yiju.web import strokes


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
            "/strokes/characters/去", headers={"Accept": "application/json"}
        )
        assert response.status_code == status
        assert response.json is not None
        assert "unavailable" in response.json["error"]
    assert calls == 2
    assert not (Path(app.config["DATABASE"]).parent / "strokes").exists()
