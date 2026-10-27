import json
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from conftest import add, set_strength
from playwright.sync_api import Browser, Page, expect, sync_playwright
from werkzeug.serving import make_server

from yiju.core.definitions import prompt_senses
from yiju.storage.database import connect
from yiju.storage.graph import refresh
from yiju.web import audio, create_app
from yiju.web.strokes import DATA_VERSION

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Generator

    from flask import Flask

CONTENT = """\
我去。 | I am going.
我学习中文。 | I study Chinese.
上
"""
STROKES = {
    "strokes": ["M 100 400 L 900 400 L 900 460 L 100 460 Z"],
    "medians": [[[100, 430], [900, 430]]],
    "radStrokes": [],
}


class Server:
    def __init__(self, app: Flask, url: str) -> None:
        self.app = app
        self.url = url

    def database(self) -> sqlite3.Connection:
        return connect(Path(self.app.config["DATABASE"]))

    def only(self, word: str, card_type: str) -> None:
        with self.database() as db:
            db.execute(
                "UPDATE cards SET suspended = "
                "NOT coalesce(word = ? AND card_type = ?, 0)",
                (word, card_type),
            )

    def reviews(self) -> int:
        with self.database() as db:
            return int(db.execute("SELECT count(*) FROM reviews").fetchone()[0])


def wait_for_scroll(page: Page, value: int) -> None:
    deadline = time.monotonic() + 5
    while page.evaluate("scrollY") != value:
        assert time.monotonic() < deadline, page.evaluate("scrollY")
        page.wait_for_timeout(50)


@pytest.fixture(scope="module")
def browser() -> Generator[Browser]:
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(
            args=["--disable-dev-shm-usage", "--disable-gpu"]
        )
        yield instance
        instance.close()


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Server]:
    app = create_app(tmp_path / "browser.sqlite3")
    strokes = tmp_path / "strokes"
    strokes.mkdir()
    for character in "我去学习中文上":
        path = strokes / f"{DATA_VERSION}-{ord(character):x}.json"
        path.write_text(json.dumps(STROKES))
    recording = tmp_path / "recording.mp3"
    recording.write_bytes(b"ID3")
    monkeypatch.setattr(audio, "cached_audio", lambda _text, _directory: recording)
    with connect(Path(app.config["DATABASE"])) as db:
        add(db, CONTENT)
        db.execute("UPDATE words SET manual = 1 WHERE word IN ('上', '学习')")
        refresh(db)
    http = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield Server(app, f"http://127.0.0.1:{http.server_port}")
    http.shutdown()
    thread.join()


@pytest.fixture
def page(browser: Browser, server: Server) -> Generator[Page]:
    context = browser.new_context(viewport={"width": 1280, "height": 820})
    context.add_cookies([{"name": "yiju-audio", "value": "off", "url": server.url}])
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    yield page
    context.close()
    assert not errors


def test_navigation_swaps_pages_without_reloading(page: Page, server: Server) -> None:
    page.goto(server.url)
    page.evaluate("window.marker = 1")
    page.get_by_role("navigation", name="Main").get_by_role(
        "link", name="Library"
    ).click()
    expect(page).to_have_url(f"{server.url}/library")
    expect(page).to_have_title("Words · Library · Yiju")
    expect(page.locator('.tabs a[aria-current="page"]')).to_have_text("Library")
    page.go_back()
    expect(page).to_have_url(f"{server.url}/")
    assert page.evaluate("window.marker") == 1


def test_back_and_forward_restore_scroll(page: Page, server: Server) -> None:
    page.set_viewport_size({"width": 1280, "height": 400})
    page.goto(f"{server.url}/settings")
    page.mouse.wheel(0, 300)
    wait_for_scroll(page, 300)
    page.get_by_role("navigation", name="Main").get_by_role(
        "link", name="Library"
    ).click()
    expect(page).to_have_url(f"{server.url}/library")
    assert page.evaluate("scrollY") == 0
    page.go_back()
    wait_for_scroll(page, 300)
    page.go_forward()
    wait_for_scroll(page, 0)


def test_library_rows_expand_inline_and_refresh_counts(
    page: Page, server: Server
) -> None:
    page.goto(f"{server.url}/library")
    item = page.locator('.item[data-item="学习"]')
    item.locator(".row").click()
    expect(item.locator(".details")).to_be_visible()
    assert "open=" in page.url
    paused = page.locator(".chips a", has_text="Paused")
    expect(paused).to_contain_text("0")
    item.get_by_role("button", name="Pause").click()
    expect(item.locator(".row .status")).to_have_text("Paused")
    expect(paused).to_contain_text("1")
    item.locator(".row").click()
    expect(item.locator(".details")).to_be_hidden()
    assert "open=" not in page.url


def test_ratings_are_saved_before_leaving_study(page: Page, server: Server) -> None:
    with server.database() as db:
        db.execute(
            "UPDATE cards SET suspended = NOT coalesce(card_type = 'recognition' "
            "AND word IN ('我', '去'), 0)"
        )
    page.goto(f"{server.url}/study")
    for _ in range(2):
        page.get_by_role("button", name="Show answer").click()
        page.get_by_role("button", name=re.compile("Good")).click()
    expect(page.locator(".done")).to_be_visible()
    page.get_by_title("Leave study").click()
    expect(page).to_have_url(f"{server.url}/")
    assert server.reviews() == 2


def test_strokes_play_only_on_the_back_of_a_card(page: Page, server: Server) -> None:
    server.only("我", "recognition")
    page.goto(f"{server.url}/study")
    glyph = page.locator(".headword .glyph")
    expect(glyph).to_have_class(re.compile(r"\bstroke-ready\b"))
    glyph.click()
    expect(page.locator(".stroke-overlay")).to_have_count(0)
    page.get_by_role("button", name="Show answer").click()
    glyph.click()
    expect(page.locator(".stroke-overlay svg")).to_have_count(1)


def test_prompt_shows_every_sense(page: Page, server: Server) -> None:
    server.only("上", "production")
    page.goto(f"{server.url}/study")
    with server.database() as db:
        readings = db.execute("SELECT readings FROM words WHERE word = '上'").fetchone()
    total = len(prompt_senses(readings[0]))
    assert total > 3
    expect(page.locator(".prompt .senses > li")).to_have_count(total)


def test_pad_keeps_its_place_and_collapses_extra_characters(
    page: Page, server: Server
) -> None:
    server.only("上", "production")
    page.goto(f"{server.url}/study")
    canvas = page.locator(".pad-canvas")
    canvas.wait_for()
    page.evaluate("Promise.all(document.getAnimations().map(a => a.finished))")
    start = canvas.bounding_box()
    assert start is not None
    for _ in range(12):
        page.mouse.move(start["x"] + 40, start["y"] + 40)
        page.mouse.down()
        page.mouse.move(start["x"] + 160, start["y"] + 60, steps=4)
        page.mouse.up()
        page.get_by_role("button", name="Next character").click()
    assert canvas.bounding_box() == start
    expect(page.locator(".pad-more")).to_be_visible()
    thumbs = page.locator(".pad-thumbs")
    assert thumbs.evaluate("element => element.scrollWidth <= element.clientWidth")
    page.locator(".pad-more").click()
    expect(page.locator(".pad-overview .pad-thumb")).to_have_count(13)


def test_pasted_sentences_are_checked_then_approved(page: Page, server: Server) -> None:
    page.goto(f"{server.url}/import")
    page.locator("textarea[name=text]").fill("他跑得很快。 | He runs fast.\n")
    page.get_by_role("button", name="Check").nth(1).click()
    expect(page.get_by_role("heading", name="Check before adding")).to_be_visible()
    expect(page.locator(".token.several", has_text="得")).to_be_visible()
    pinyin = page.locator("input[name=s1-pinyin]")
    expect(pinyin).to_have_value("tā pǎo dé hěn kuài.")
    reading = page.get_by_role("group", name="Reading of 得")
    expect(reading.get_by_role("radio", name="dé to obtain")).to_be_checked()
    reading.locator("label", has_text="structural particle").click()
    expect(pinyin).to_have_value("tā pǎo de hěn kuài.")
    pinyin.fill("Tā pǎo děi hěn kuài.")
    expect(reading.get_by_role("radio", name="děi to have to")).to_be_checked()
    pinyin.fill("Tā pǎo de hěn kuài.")
    page.get_by_role("button", name="Approve all").click()
    expect(page.locator(".toast")).to_contain_text("Added 1 sentence")
    with server.database() as db:
        assert (
            db.execute(
                "SELECT pinyin FROM sentences WHERE chinese = '他跑得很快。'"
            ).fetchone()[0]
            == "Tā pǎo de hěn kuài."
        )


def test_missed_words_are_sent_with_a_sentence_rating(
    page: Page, server: Server
) -> None:
    with server.database() as db:
        set_strength(db, "我", stability=10, learned=False)
        set_strength(db, "去", stability=10, learned=False)
        db.execute(
            "UPDATE cards SET suspended = NOT coalesce(sentence_id = 1 "
            "AND card_type = 'recognition', 0)"
        )
    page.goto(f"{server.url}/study")
    page.get_by_role("button", name="Show answer").click()
    page.get_by_title("Mark 去 as a word you missed").click()
    page.get_by_role("button", name=re.compile("Again")).click()
    expect(page.locator(".done")).to_be_visible()
    page.get_by_title("Leave study").click()
    expect(page).to_have_url(f"{server.url}/")
    with server.database() as db:
        assert dict(db.execute("SELECT word, role FROM review_words").fetchall()) == {
            "去": "marked"
        }
