import gzip
import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from conftest import (
    SAMPLE,
    add,
    check_form,
    csrf,
    database,
    import_text,
    page_context,
    set_strength,
    start_import,
    states,
)

from yiju.storage.database import settings
from yiju.storage.pending import editing, propose_removal, propose_sentence, stage
from yiju.web import create_app

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient

JSON = {"Accept": "application/json"}


@pytest.mark.parametrize(
    ("path", "title", "section"),
    [
        ("/", "Today · Yiju", "home"),
        ("/study", "Study · Yiju", "study"),
        ("/library", "Words · Library · Yiju", "library"),
        ("/sentences", "Sentences · Library · Yiju", "library"),
        ("/settings", "Settings · Yiju", "settings"),
        ("/import", "Add · Yiju", "library"),
        ("/pending", "Check before adding · Yiju", "library"),
        ("/about", "About · Yiju", "settings"),
    ],
)
def test_pages_have_titles_and_mark_the_current_section(
    client: FlaskClient, path: str, title: str, section: str
) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert f"<title>{title}</title>" in response.get_data(as_text=True)
    assert page_context(client, path)["section"] == section


def test_pages_compress_when_supported(client: FlaskClient) -> None:
    plain = client.get("/settings")
    compressed = client.get("/settings", headers={"Accept-Encoding": "gzip"})
    assert compressed.headers["Content-Encoding"] == "gzip"
    assert gzip.decompress(compressed.data) == plain.data
    assert "Accept-Encoding" in compressed.vary
    backup = client.get("/backup", headers={"Accept-Encoding": "gzip"})
    assert "Content-Encoding" not in backup.headers


def test_one_sentence_goes_through_the_check_screen_before_it_is_added(
    client: FlaskClient, app: Flask
) -> None:
    token = csrf(client)
    response = client.post(
        "/import",
        data={
            "csrf": token,
            "chinese": "  我很喜欢葡萄。 ",
            "translation": "I like grapes.",
        },
    )
    assert response.status_code == 303
    assert response.headers["Location"] == "/pending"
    html = client.get("/pending").get_data(as_text=True)
    assert "Check before adding" in html
    assert "wǒ hěn xǐhuan pútao." in html
    assert "grape" in html
    assert "Reading of" not in html
    with database(app) as db:
        assert db.execute("SELECT count(*) FROM words").fetchone()[0] == 0
    assert page_context(client, "/pending")["pending"].ready


def test_everything_added_waits_on_one_check_screen(
    client: FlaskClient, app: Flask
) -> None:
    start_import(client, "我去。 | I am going.\n")
    with database(app) as db:
        stage(db, "我学习中文。 | I study Chinese.\n苹果\n")
    assert "3 items waiting for your check" in client.get("/").get_data(as_text=True)
    assert "3 items waiting" in client.get("/import").get_data(as_text=True)
    waiting = page_context(client, "/pending")["pending"]
    assert [sentence.chinese for sentence in waiting.sentences] == [
        "我去。",
        "我学习中文。",
    ]
    response = client.post("/pending", data={**check_form(client), "step": "approve"})
    page = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Added 2 sentences and 5 words." in page
    assert "Nothing is waiting for your check." in page
    with database(app) as db:
        assert states(db) == {
            "我": "unlocked",
            "去": "unlocked",
            "学习": "locked",
            "中文": "locked",
            "苹果": "locked",
        }
    assert "waiting for your check" not in client.get("/").get_data(as_text=True)


def test_one_sentence_can_be_approved_on_its_own(
    client: FlaskClient, app: Flask
) -> None:
    start_import(
        client,
        "我去。 | I am going.\n我学习中文。 | I study Chinese.\n嗯嗯嗯 好 | Mm.\n",
    )
    response = client.post("/pending", data={**check_form(client), "approve": "2"})
    page = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Added 我学习中文。 with 3 new words." in page
    with database(app) as db:
        assert {row[0] for row in db.execute("SELECT word FROM words")} == {
            "我",
            "学习",
            "中文",
        }
        assert [row[0] for row in db.execute("SELECT chinese FROM sentences")] == [
            "我学习中文。"
        ]
    waiting = page_context(client, "/pending")["pending"]
    assert [sentence.id for sentence in waiting.sentences] == [1, 3]
    assert [token.new for token in waiting.sentences[0].words] == [False, True]
    client.post("/pending", data={**check_form(client), "approve": "3"})
    assert "Fix the problems in this sentence" in client.get("/pending").get_data(
        as_text=True
    )
    client.post("/pending", data={**check_form(client), "approve": "9"})
    assert "no longer waiting" in client.get("/pending").get_data(as_text=True)
    with database(app) as db:
        assert db.execute("SELECT count(*) FROM sentences").fetchone()[0] == 1


def test_changing_the_split_requires_another_look_before_approval(
    client: FlaskClient, app: Flask
) -> None:
    start_import(client, "我学习中文。 | I study Chinese.\n")
    form = check_form(client)
    form["s1-words"] = "我 学 习 中文"
    response = client.post("/pending", data={**form, "step": "approve"})
    assert response.status_code == 303
    assert "The word list changed" in client.get("/pending").get_data(as_text=True)
    with database(app) as db:
        assert db.execute("SELECT count(*) FROM words").fetchone()[0] == 0
        assert db.execute("SELECT words FROM pending_sentences").fetchone()[0] == (
            '["我", "学", "习", "中文"]'
        )
    client.post("/pending", data={**check_form(client), "step": "approve"})
    with database(app) as db:
        assert {row[0] for row in db.execute("SELECT word FROM words")} == {
            "我",
            "学",
            "习",
            "中文",
        }


def test_a_discarded_sentence_leaves_the_rest_ready(
    client: FlaskClient, app: Flask
) -> None:
    start_import(client, "你好 | Hello\n嗯嗯嗯 好 | Mm, good.\n")
    client.post("/pending", data={**check_form(client), "discard": "2"})
    assert "Discarded the sentence." in client.get("/pending").get_data(as_text=True)
    client.post("/pending", data={**check_form(client), "step": "approve"})
    with database(app) as db:
        assert [row[0] for row in db.execute("SELECT word FROM words")] == ["你好"]
        assert db.execute("SELECT count(*) FROM pending_sentences").fetchone()[0] == 0


def test_problems_block_approval_and_stale_forms_are_rejected(
    client: FlaskClient, app: Flask
) -> None:
    start_import(client, "嗯嗯嗯 好 | Mm, good.\n")
    page = client.get("/pending").get_data(as_text=True)
    assert "Fix these before approving" in page
    assert "CC-CEDICT has no entry" in page
    form = check_form(client)
    client.post("/pending", data={**form, "step": "approve"})
    assert "Fix the problems" in client.get("/pending").get_data(as_text=True)
    with database(app) as db:
        stage(db, "你好 | Hello\n")
    stale = client.post("/pending", data={**form, "step": "save"})
    assert stale.status_code == 303
    assert "changed while you were checking" in client.get("/pending").get_data(
        as_text=True
    )
    discard = client.post("/pending", data={**form, "step": "discard"})
    assert discard.headers["Location"] == "/pending"
    form = check_form(client)
    index = [word for word in form if form[word] == "" and word.endswith("meanings")]
    form[index[0]] = "hmm"
    client.post("/pending", data={**form, "step": "approve"})
    with database(app) as db:
        assert (
            db.execute("SELECT source FROM words WHERE word = '嗯嗯嗯'").fetchone()[0]
            == "manual"
        )


def test_discarding_and_bad_input_leave_the_library_alone(
    client: FlaskClient, app: Flask
) -> None:
    token = csrf(client)
    response = client.post("/import", data={"csrf": token, "text": "hello\n"})
    assert response.status_code == 400
    assert "Line 1:" in response.get_data(as_text=True)
    assert "hello" in response.get_data(as_text=True)
    response = client.post(
        "/import", data={"csrf": token, "chinese": "我很好", "translation": ""}
    )
    assert response.status_code == 400
    assert client.post("/import", data={"text": "你好 | Hello"}).status_code == 400
    start_import(client, "你好 | Hello\n")
    response = client.post("/pending", data={**check_form(client), "step": "discard"})
    assert response.headers["Location"] == "/pending"
    with database(app) as db:
        assert db.execute("SELECT count(*) FROM pending_sentences").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM words").fetchone()[0] == 0


def test_settings_validate_and_reclassify_words(
    client: FlaskClient, app: Flask
) -> None:
    with database(app) as db:
        add(db, SAMPLE)
        set_strength(db, "我", stability=10)
    token = csrf(client)
    data = {
        "csrf": token,
        "new_per_day": 12,
        "retention": 0.9,
        "learned_stability": 14,
        "unlearned_stability": 12,
        "learned_days": 3,
        "sentence_pass_weight": 0.2,
        "sentence_fail_weight": 0.25,
    }
    saved = client.post("/settings", data=data, follow_redirects=True)
    assert b"Settings saved." in saved.data
    with database(app) as db:
        assert settings(db)["new_per_day"] == 12
        assert (
            db.execute("SELECT learned FROM words WHERE word = '我'").fetchone()[0] == 0
        )
    invalid = client.post("/settings", data={**data, "new_per_day": -1}, headers=JSON)
    assert invalid.status_code == 400
    assert invalid.json is not None
    assert "new_per_day" in invalid.json["error"]
    inverted = client.post("/settings", data={**data, "unlearned_stability": 20})
    assert inverted.status_code == 400
    assert (
        client.post("/settings", data={**data, "retention": "nan"}).status_code == 400
    )
    assert client.post("/settings", data={"csrf": "错"}).status_code == 400


def test_errors_are_json_for_api_requests_and_html_for_pages(
    client: FlaskClient, app: Flask
) -> None:
    with database(app) as db:
        add(db, SAMPLE)
        version = db.execute("SELECT version FROM words WHERE word = '去'").fetchone()[
            0
        ]
    token = csrf(client)
    stale = {"csrf": token, "version": version + 1, "paused": "1"}
    conflict = client.post("/words/去/pause", data=stale)
    assert conflict.status_code == 409
    assert b"<!DOCTYPE html>" in conflict.data
    conflict = client.post("/words/去/pause", data=stale, headers={"X-Fragment": "1"})
    assert conflict.status_code == 409
    assert conflict.json is not None
    assert "changed" in conflict.json["error"]
    expired = client.post("/settings", data={"csrf": "expired"}, headers=JSON)
    assert expired.status_code == 400
    assert expired.json is not None
    assert "expired" in expired.json["error"]
    missing = client.get("/api/missing")
    assert missing.status_code == 404
    assert missing.json is not None


def test_backup_contains_data_and_restart_preserves_it(
    client: FlaskClient, app: Flask, tmp_path: Path
) -> None:
    import_text(client)
    response = client.get("/backup")
    assert response.status_code == 200
    backup = tmp_path / "backup.sqlite3"
    backup.write_bytes(response.data)
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT count(*) FROM words").fetchone()[0] == 5
    restarted = create_app(Path(app.config["DATABASE"]))
    assert restarted.secret_key == app.secret_key
    with restarted.test_client() as other:
        assert "of 5 learned" in other.get("/").get_data(as_text=True)


def test_imported_text_is_escaped(client: FlaskClient) -> None:
    start_import(client, "我去。 | <script>alert(1)</script>\n")
    response = client.get("/pending")
    assert b"<script>alert(1)</script>" not in response.data
    assert b"&lt;script&gt;" in response.data
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    client.post("/pending", data={**check_form(client), "step": "approve"})
    for url, headers in [
        ("/sentences", {}),
        ("/sentences/1", {"X-Fragment": "1"}),
        ("/study", {}),
    ]:
        response = client.get(url, headers=headers)
        assert response.status_code == 200
        assert b"<script>alert(1)</script>" not in response.data


def test_about_page_credits_cc_cedict(client: FlaskClient) -> None:
    page = client.get("/about").get_data(as_text=True)
    assert "CC-CEDICT" in page
    assert "creativecommons.org/licenses/by-sa/4.0" in page
    assert "Denisowski" in page


def test_library_changes_wait_on_the_check_screen(
    client: FlaskClient, app: Flask
) -> None:
    with database(app) as db:
        add(db, "我喜欢喝茶。 | I like drinking tea.\n我去。 | I am going.\n")
        stage_changes(db)
    tabs = page_context(client, "/pending")["tabs"]
    assert [(tab["key"], len(tab["items"]), tab["problems"]) for tab in tabs] == [
        ("edits", 1, ()),
        ("removals", 1, ()),
    ]
    assert page_context(client, "/pending")["selected"] == "edits"
    assert page_context(client, "/pending?tab=removals")["selected"] == "removals"
    assert page_context(client, "/pending?tab=nonsense")["selected"] == "edits"
    html = client.get("/pending").get_data(as_text=True)
    assert re.search(r'data-tab-panel="edits"\s*>', html)
    assert re.search(r'data-tab-panel="removals"\s+hidden', html)
    assert "changes <strong>2</strong> items already in your library" in html
    assert '<del class="token gone" title="Leaves this sentence">喝茶</del>' in html
    assert "Deletes its cards" not in html
    assert ">Sentence 2<" not in html
    assert '<span class="word-row-han" lang="zh-Hans">去</span>' in html
    assert html.count('<span class="status status-adding">Adding</span>') == 2
    form = check_form(client)
    form["c1-translation"] = "I like tea."
    response = client.post("/pending", data={**form, "approve-change": "1"})
    page = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Changed 我喜欢喝茶。 and added 2 new words." in page
    with database(app) as db:
        assert (
            db.execute("SELECT translation FROM sentences WHERE id = 1").fetchone()[0]
            == "I like tea."
        )
    response = client.post(
        "/pending",
        data={**check_form(client), "discard-change": "2", "tab": "removals"},
    )
    assert response.headers["Location"] == "/pending?tab=removals"
    page = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Discarded the change." in page
    assert "Nothing is waiting for your check." in page
    with database(app) as db:
        stage_changes(db, sentence=False)
    response = client.post("/pending", data={**check_form(client), "step": "approve"})
    page = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Made 1 change to your library." in page
    with database(app) as db:
        assert [row[0] for row in db.execute("SELECT id FROM sentences")] == [1]


def stage_changes(db: sqlite3.Connection, *, sentence: bool = True) -> None:
    with editing(db):
        if sentence:
            propose_sentence(db, 1, words="我 喜欢 喝 茶")
        propose_removal(db, "sentence", "2")
