import io
import json
import re
import sqlite3
import threading
import time
from collections.abc import Generator
from pathlib import Path

import pytest
from flask import Flask
from playwright.sync_api import Browser, Page, expect, sync_playwright
from werkzeug.serving import make_server

from yiju.storage.database import connect
from yiju.web import audio, create_app
from yiju.web.strokes import DATA_VERSION

EXPORT = """<?xml version="1.0" encoding="UTF-8"?>
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
<card language="chinese"><entry><headword charset="sc">上</headword>
<pron type="hypy">shang4</pron><defn>noun 1 above 2 up 3 upper 4 previous</defn>
</entry><dictref dictid="TEST" entryid="4"/></card>
</cards></plecoflash>"""
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

    def only(self, word: str, direction: str) -> None:
        with self.database() as db:
            db.execute(
                "UPDATE cards SET suspended = NOT (word = ? AND direction = ? "
                "AND sentence_id IS NULL)",
                (word, direction),
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
    for character in "我去学习上":
        path = strokes / f"{DATA_VERSION}-{ord(character):x}.json"
        path.write_text(json.dumps(STROKES))
    recording = tmp_path / "recording.mp3"
    recording.write_bytes(b"ID3")
    monkeypatch.setattr(audio, "cached_audio", lambda _text, _directory: recording)
    client = app.test_client()
    client.get("/")
    with client.session_transaction() as session:
        token = str(session["csrf"])
    client.post(
        "/import",
        data={"csrf": token, "file": (io.BytesIO(EXPORT.encode()), "words.xml")},
    )
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
            "UPDATE cards SET suspended = NOT (direction = 'recognition' "
            "AND sentence_id IS NULL AND word IN ('我', '去'))"
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


def test_prompt_expands_to_every_sense(page: Page, server: Server) -> None:
    server.only("上", "production")
    page.goto(f"{server.url}/study")
    senses = page.locator(".prompt .senses > li")
    expect(senses).to_have_count(3)
    page.get_by_role("button", name="+1 more sense").click()
    expect(senses).to_have_count(4)


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
