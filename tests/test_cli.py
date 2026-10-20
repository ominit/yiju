import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import SAMPLE, add, set_strength

from yiju import cli
from yiju.storage.database import connect, initialize
from yiju.storage.learning import review_card


@pytest.fixture
def data(tmp_path: Path) -> Path:
    initialize(tmp_path / "yiju.sqlite3")
    return tmp_path


def run(
    data: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    *args: str,
    stdin: str = "",
    status: int = 0,
) -> Any:  # noqa: ANN401
    command, *rest = args
    if command == "import":
        action, *rest = rest
        argv = ["yiju", "import", action, "--data-dir", str(data), *rest]
    else:
        argv = ["yiju", command, "--data-dir", str(data), *rest]
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    if status:
        with pytest.raises(SystemExit) as exit_info:
            cli.main()
        assert exit_info.value.code == status
    else:
        cli.main()
    return json.loads(capsys.readouterr().out)


def test_agent_imports_wait_for_the_user_and_report_what_they_would_add(
    data: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run(
        data,
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
    assert not result["ready"]
    assert "Sentence 2: 嗯嗯嗯 has no definition." in result["issues"]
    words = {word["word"]: word for word in result["words"]}
    assert words["葡萄"]["readings"][0]["senses"][0]["text"] == "grape"
    assert words["嗯嗯嗯"]["found"] is False
    with connect(data / "yiju.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM words").fetchone()[0] == 0
    result = run(data, capsys, monkeypatch, "import", "add", stdin="苹果\n")
    assert len(result["sentences"]) == 2
    assert run(data, capsys, monkeypatch, "import", "show") == result


def test_agents_can_revise_waiting_items(
    data: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run(
        data,
        capsys,
        monkeypatch,
        "import",
        "add",
        stdin="他跑得很快。 | He runs fast.\n嗯嗯嗯 好 | Mm, good.\n",
    )
    result = run(
        data,
        capsys,
        monkeypatch,
        "import",
        "edit-sentence",
        "1",
        "--pinyin",
        "Tā pǎo de hěn kuài.",
    )
    assert result["sentences"][0]["pinyin"] == "Tā pǎo de hěn kuài."
    result = run(
        data,
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
    assert [sense["text"] for sense in word["readings"][0]["senses"]] == [
        "hmm",
        "expression of doubt",
    ]
    assert result["ready"]
    result = run(data, capsys, monkeypatch, "import", "remove-sentence", "2")
    assert [sentence["id"] for sentence in result["sentences"]] == [1]


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
    data: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run(data, capsys, monkeypatch, "import", "add", stdin="hello\n", status=1)
    assert result["error"] == "The text is not in the import format."
    assert result["lines"][0].startswith("Line 1:")
    assert run(data, capsys, monkeypatch, "word", "葡萄", status=1) == {
        "error": "葡萄 is not in the library."
    }
    assert (
        "no pending sentence 9"
        in run(data, capsys, monkeypatch, "import", "remove-sentence", "9", status=1)[
            "error"
        ]
    )
    run(data, capsys, monkeypatch, "import", "add", stdin="你好 | Hello\n")
    assert (
        "not a new word"
        in run(
            data,
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


def test_agents_can_query_words_sentences_flags_and_reviews(
    data: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    with connect(data / "yiju.sqlite3") as db:
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
    word = run(data, capsys, monkeypatch, "word", "我")
    assert word["status"] == "learning"
    assert word["strength"]["success_days"] == 1
    assert word["sentences"][0]["chinese"] == "我去。"
    assert word["readings"][0]["pinyin"] == "wǒ"
    learning = run(data, capsys, monkeypatch, "words", "--status", "learning")
    assert {item["word"] for item in learning} == {"我", "去"}
    backlog = run(data, capsys, monkeypatch, "words", "--backlog")
    assert [item["word"] for item in backlog] == ["苹果"]
    flagged = run(data, capsys, monkeypatch, "words", "--flagged")
    assert [item["word"] for item in flagged] == ["去"]
    assert run(data, capsys, monkeypatch, "flags")[0]["note"] == "Check the tone."
    sentences = run(data, capsys, monkeypatch, "sentences", "--status", "unlocked")
    assert [sentence["words"] for sentence in sentences] == [["我", "去"]]
    assert run(data, capsys, monkeypatch, "stats")["learning"] == 2
    (entry,) = run(data, capsys, monkeypatch, "reviews")
    assert entry["word"] == "我"
    assert entry["role"] == "card"
    assert entry["parameters"]["learned_stability"] == 7.0
    assert run(data, capsys, monkeypatch, "reviews", "--since", "2999-01-01") == []
    assert "format" in run(data, capsys, monkeypatch, "import", "format")
