import io
import json
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from conftest import SAMPLE, add, database, set_strength
from werkzeug.serving import make_server

from yiju import cli
from yiju.storage.learning import review_card

if TYPE_CHECKING:
    from collections.abc import Generator

    from flask import Flask
    from flask.testing import FlaskClient


@pytest.fixture
def server(app: Flask) -> Generator[str]:
    http = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{http.server_port}"
    http.shutdown()
    thread.join()


def run(
    server: str | None,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    *args: str,
    stdin: str = "",
    status: int = 0,
) -> Any:  # noqa: ANN401
    url = [] if server is None else ["--url", server]
    monkeypatch.setattr("sys.argv", ["yiju", *args, *url])
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    if status:
        with pytest.raises(SystemExit) as exit_info:
            cli.main()
        assert exit_info.value.code == status
    else:
        cli.main()
    return json.loads(capsys.readouterr().out)


def test_agent_imports_wait_for_the_user_and_report_what_they_would_add(
    app: Flask,
    server: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run(
        server,
        capsys,
        monkeypatch,
        "import",
        "add",
        stdin="我很喜欢葡萄。 | I really like grapes.\n嗯嗯嗯 好 | Mm, good.\n",
    )
    assert result["check_screen"] == "/pending"
    first = result["sentences"][0]
    assert first["id"] == 1
    assert [word["text"] for word in first["words"]] == ["我", "很", "喜欢", "葡萄"]
    assert first["pinyin"] == "wǒ hěn xǐhuan pútao."
    assert first["words"][3]["meanings"] == ["grape"]
    assert not result["ready"]
    assert "Sentence 2: 嗯嗯嗯 has no definition." in result["issues"]
    words = {word["word"]: word for word in result["words"]}
    assert "readings" not in words["葡萄"]
    assert words["嗯嗯嗯"]["found"] is False
    with database(app) as db:
        assert db.execute("SELECT count(*) FROM words").fetchone()[0] == 0
    result = run(
        server,
        capsys,
        monkeypatch,
        "import",
        "add",
        stdin="苹果\n我很喜欢葡萄。 | I like grapes a lot.\n",
    )
    assert [sentence["id"] for sentence in result["sentences"]] == [1]
    assert result["already_waiting"] == ["我很喜欢葡萄。"]
    words = {word["word"] for word in result["words"]}
    assert "苹果" in words
    assert "嗯嗯嗯" not in words
    shown = run(server, capsys, monkeypatch, "import", "show")
    assert len(shown["sentences"]) == 2
    assert "already_waiting" not in shown
    result = run(
        server, capsys, monkeypatch, "import", "edit-sentence", "2", "--rules", ""
    )
    assert [sentence["id"] for sentence in result["sentences"]] == [2]
    assert [word["word"] for word in result["words"]] == ["嗯嗯嗯", "好"]


def test_agents_can_revise_waiting_items(
    server: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run(
        server,
        capsys,
        monkeypatch,
        "import",
        "add",
        stdin="他跑得很快。 | He runs fast.\n嗯嗯嗯 好 | Mm, good.\n",
    )
    result = run(
        server,
        capsys,
        monkeypatch,
        "import",
        "edit-sentence",
        "1",
        "--pinyin",
        "Tā pǎo de hěn kuài.",
    )
    assert result["sentences"][0]["pinyin"] == "Tā pǎo de hěn kuài."
    assert result["sentences"][0]["translation"] == "He runs fast."
    result = run(
        server,
        capsys,
        monkeypatch,
        "import",
        "edit-word",
        "嗯嗯嗯",
        "--pinyin",
        "ǹg",
        "--meanings",
        "hmm\\nexpression of doubt",
    )
    word = next(word for word in result["words"] if word["word"] == "嗯嗯嗯")
    assert word["manual"]
    assert word["readings"][0]["meanings"] == [
        "hmm",
        "expression of doubt",
    ]
    assert result["ready"]
    assert (
        "give meanings too"
        in (
            run(
                server,
                capsys,
                monkeypatch,
                "import",
                "edit-word",
                "嗯嗯嗯",
                "--pinyin",
                "ńg",
                "--meanings",
                "",
                status=1,
            )["error"]
        )
    )
    assert (
        run(
            server,
            capsys,
            monkeypatch,
            "import",
            "edit-sentence",
            "1",
            "--words",
            "他 跑 得 很 慢",
            status=1,
        )["error"]
        == "The words must spell out the sentence's characters in order."
    )
    result = run(server, capsys, monkeypatch, "import", "show")
    assert result["sentences"][0]["pinyin"] == "Tā pǎo de hěn kuài."
    word = next(word for word in result["words"] if word["word"] == "嗯嗯嗯")
    assert word["manual"]
    run(server, capsys, monkeypatch, "import", "add", stdin="苹果 好\n")
    result = run(server, capsys, monkeypatch, "import", "remove-word", "苹果")
    assert "苹果" not in {word["word"] for word in result["words"]}
    assert (
        "goes with that sentence"
        in run(server, capsys, monkeypatch, "import", "remove-word", "好", status=1)[
            "error"
        ]
    )
    result = run(server, capsys, monkeypatch, "import", "remove-sentence", "2")
    assert result["sentences"] == []
    result = run(server, capsys, monkeypatch, "import", "show")
    assert [sentence["id"] for sentence in result["sentences"]] == [1]


def test_agents_stage_names_and_gender_rules(
    app: Flask,
    server: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        "surname first" in run(None, capsys, monkeypatch, "names", "format")["format"]
    )
    assert run(server, capsys, monkeypatch, "names", "list") == []
    result = run(
        server, capsys, monkeypatch, "names", "add", stdin="王芳 female\n李伟 male\n"
    )
    assert [name["english"] for name in result["names"]] == ["Wang Fang", "Li Wei"]
    assert result["adds"]["names"] == 2
    assert result["already_waiting"] == []
    result = run(server, capsys, monkeypatch, "names", "add", stdin="王芳 female\n")
    assert result["already_waiting"] == ["王芳"]
    result = run(
        server,
        capsys,
        monkeypatch,
        "names",
        "edit",
        "2",
        "--english",
        "Wei Li",
        "--gender",
        "female",
    )
    assert [(name["english"], name["gender"]) for name in result["names"]] == [
        ("Wei Li", "female")
    ]
    run(server, capsys, monkeypatch, "names", "remove", "1")
    result = run(server, capsys, monkeypatch, "import", "show")
    assert [name["chinese"] for name in result["names"]] == ["李伟"]
    result = run(
        server,
        capsys,
        monkeypatch,
        "import",
        "add",
        stdin="{A}喜欢{B}。 | {A} likes {B}.\n",
    )
    assert ["slot" in word for word in result["sentences"][0]["words"]] == [
        True,
        False,
        True,
    ]
    result = run(
        server,
        capsys,
        monkeypatch,
        "import",
        "edit-sentence",
        "1",
        "--rules",
        "A=male A=female",
    )
    assert "Sentence 1: The gender rules contradict each other." in result["issues"]
    result = run(
        server, capsys, monkeypatch, "import", "edit-sentence", "1", "--rules", "A!=B"
    )
    assert result["sentences"][0]["rules"] == "A!=B"
    monkeypatch.setattr("sys.argv", ["yiju", "names", "approve"])
    with pytest.raises(SystemExit):
        cli.main()
    with database(app) as db:
        assert db.execute("SELECT count(*) FROM names").fetchone()[0] == 0


@pytest.mark.parametrize("action", ["approve", "discard"])
def test_agents_cannot_approve_or_discard_imports(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    monkeypatch.setattr("sys.argv", ["yiju", "import", action])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 2
    capsys.readouterr()


def test_bad_input_returns_json_errors(
    server: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run(
        server, capsys, monkeypatch, "import", "add", stdin="hello\n", status=1
    )
    assert result["error"] == "The text is not in the import format."
    assert result["lines"][0].startswith("Line 1:")
    assert run(server, capsys, monkeypatch, "word", "葡萄") == {
        "word": "葡萄",
        "status": "not in the library",
        "readings": [{"pinyin": "pútao", "meanings": ["grape"]}],
    }
    assert run(server, capsys, monkeypatch, "word", "嗯嗯嗯", status=1) == {
        "error": "嗯嗯嗯 is not in the library or CC-CEDICT."
    }
    assert run(server, capsys, monkeypatch, "word", "葡萄", "嗯嗯嗯") == [
        {
            "word": "葡萄",
            "status": "not in the library",
            "readings": [{"pinyin": "pútao", "meanings": ["grape"]}],
        },
        {"word": "嗯嗯嗯", "error": "嗯嗯嗯 is not in the library or CC-CEDICT."},
    ]
    assert (
        "no pending sentence 9"
        in run(server, capsys, monkeypatch, "import", "remove-sentence", "9", status=1)[
            "error"
        ]
    )
    run(server, capsys, monkeypatch, "import", "add", stdin="你好 | Hello\n")
    assert (
        "not a new word"
        in run(
            server,
            capsys,
            monkeypatch,
            "import",
            "edit-word",
            "葡萄",
            "--meanings",
            "x",
            status=1,
        )["error"]
    )


def test_an_unreachable_server_is_a_json_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run("http://127.0.0.1:9", capsys, monkeypatch, "stats", status=1)
    assert result["error"].startswith("Could not reach yiju at http://127.0.0.1:9")


def test_agents_can_query_words_sentences_flags_and_reviews(
    app: Flask,
    server: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    with database(app) as db:
        add(db, SAMPLE, now)
        db.execute(
            "INSERT INTO flags VALUES ('去', 'Check the tone.', ?, ?)",
            (now.isoformat(), now.isoformat()),
        )
        set_strength(db, "去", stability=1, learned=False, now=now)
        card = db.execute(
            "SELECT * FROM cards WHERE word = '我' AND card_type = 'recognition'"
        ).fetchone()
        review_card(db, card["id"], card["version"], 3, now)
    word = run(server, capsys, monkeypatch, "word", "我")
    assert word["status"] == "learning"
    assert word["strength"]["success_days"] == 1
    assert word["sentences"][0]["chinese"] == "我去。"
    assert word["readings"][0]["pinyin"] == "wǒ"
    learning = run(server, capsys, monkeypatch, "words", "--status", "learning")
    assert {item["word"] for item in learning} == {"我", "去"}
    backlog = run(server, capsys, monkeypatch, "words", "--backlog")
    assert [item["word"] for item in backlog] == ["苹果"]
    flagged = run(server, capsys, monkeypatch, "words", "--flagged")
    assert [item["word"] for item in flagged] == ["去"]
    assert run(server, capsys, monkeypatch, "flags")[0]["note"] == "Check the tone."
    sentences = run(server, capsys, monkeypatch, "sentences", "--status", "unlocked")
    assert [sentence["words"] for sentence in sentences] == [["我", "去"]]
    assert run(server, capsys, monkeypatch, "stats")["learning"] == 2
    (entry,) = run(server, capsys, monkeypatch, "reviews")
    assert entry["word"] == "我"
    assert entry["role"] == "card"
    assert entry["parameters"]["learned_stability"] == 7.0
    assert run(server, capsys, monkeypatch, "reviews", "--since", "2999-01-01") == []
    assert "format" in run(None, capsys, monkeypatch, "import", "format")


def test_agent_routes_take_json_without_a_form_token(
    app: Flask, client: FlaskClient
) -> None:
    form = client.post("/api/agent/pending", data={"text": "苹果"})
    assert form.status_code == 400
    assert form.json == {"error": "Expected a JSON body."}
    with database(app) as db:
        assert db.execute("SELECT count(*) FROM pending_words").fetchone()[0] == 0
    staged = client.post("/api/agent/pending", json={"text": "苹果"})
    assert staged.status_code == 200
    assert staged.json is not None
    assert [word["word"] for word in staged.json["words"]] == ["苹果"]
    study = client.post("/api/reviews", json={"card": 1})
    assert study.status_code == 400
    assert study.json is not None
    assert "expired" in study.json["error"]
    assert client.get("/api/agent/words?status=nope").status_code == 400


def test_agents_propose_library_changes_that_wait_for_the_user(
    app: Flask,
    server: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with database(app) as db:
        add(db, "我喜欢喝茶。 | I like drinking tea.\n我去。 | I am going.\n")

    def library(*args: str, status: int = 0) -> Any:  # noqa: ANN401
        return run(server, capsys, monkeypatch, "library", *args, status=status)

    result = library("edit-sentence", "1", "--words", "我 喜欢 喝 茶")
    [change] = result["changes"]
    assert change["kind"] == "sentence"
    assert change["action"] == "edit"
    assert change["after"]["pinyin"] == "wǒ xǐhuan hē chá."
    assert [word["word"] for word in result["words"]] == ["喝", "茶"]
    assert result["adds"] == {"sentences": 0, "words": 2, "names": 0, "changes": 1}
    assert result["ready"]
    assert library("edit-sentence", "1", "--words", "我 茶", status=1) == {
        "error": "The words must spell out the sentence's characters in order."
    }
    assert "no sentence 9" in library("remove-sentence", "9", status=1)["error"]
    assert "not in your library" in library("remove-word", "茶", status=1)["error"]
    removal = library("remove-word", "喝茶")["changes"][0]
    assert removal["action"] == "remove"
    assert removal["issues"] == []
    definition = library("edit-word", "去", "--meanings", "to go\\nto leave")
    assert definition["changes"][0]["after"] == [
        {"pinyin": "qù", "meanings": ["to go", "to leave"]}
    ]
    shown = run(server, capsys, monkeypatch, "import", "show")
    assert [item["id"] for item in shown["changes"]] == [1, 2, 3]
    assert run(server, capsys, monkeypatch, "stats")["pending"] == 3
    run(server, capsys, monkeypatch, "import", "remove-change", "3")
    assert (
        "no pending change 3"
        in run(server, capsys, monkeypatch, "import", "remove-change", "3", status=1)[
            "error"
        ]
    )
    with database(app) as db:
        assert db.execute("SELECT count(*) FROM pending_changes").fetchone()[0] == 2
        assert (
            db.execute(
                "SELECT count(*) FROM sentence_words WHERE word = '喝茶'"
            ).fetchone()[0]
            == 1
        )
