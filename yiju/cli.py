import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, urlencode

from .storage import pending
from .storage.export import SENTENCE_STATUSES, WORD_STATUSES

AGENT_HELP = """\
Commands other than serve and backup print JSON and are meant for scripts and
agents. They talk to a running web app through its /api/agent routes, at the
address in --url or YIJU_URL. Imports never change the library directly:
`yiju import add` puts the items on the check screen in the web app, next to
anything else waiting there, and prints exactly what approving would add.
Agents can revise waiting items with the edit commands, but only the user can
approve them.
"""
DEFAULT_URL = "http://127.0.0.1:8000"

type Body = dict[str, object]
type Call = tuple[str, str, Body | None]


def _data_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--data-dir", type=Path, default=Path(os.environ.get("YIJU_DATA_DIR", "data"))
    )


def _url(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--url",
        default=os.environ.get("YIJU_URL", DEFAULT_URL),
        help=f"Address of the running web app. Defaults to YIJU_URL or {DEFAULT_URL}.",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yiju",
        description=AGENT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("serve", help="Run the web app.")
    server.add_argument("--host", default=os.environ.get("YIJU_HOST", "0.0.0.0"))
    server.add_argument(
        "--port", type=int, default=int(os.environ.get("YIJU_PORT", "8000"))
    )
    _data_dir(server)
    backup = commands.add_parser("backup", help="Copy the database to a new file.")
    backup.add_argument("destination", type=Path)
    _data_dir(backup)

    word = commands.add_parser("word", help="Show one word's state and strength.")
    word.add_argument("word")
    words = commands.add_parser("words", help="List words.")
    words.add_argument("--status", choices=WORD_STATUSES)
    words.add_argument("--flagged", action="store_true")
    words.add_argument(
        "--backlog", action="store_true", help="Only words not in any sentence."
    )
    sentences = commands.add_parser("sentences", help="List sentences.")
    sentences.add_argument("--status", choices=SENTENCE_STATUSES)
    commands.add_parser("flags", help="List flagged words with notes.")
    commands.add_parser("stats", help="Summarize the library and today's queue.")
    reviews = commands.add_parser(
        "reviews",
        help="Print the review log: every word each review moved, its recall "
        "before, its strength before and after, and the parameters in force.",
    )
    reviews.add_argument("--since", help="ISO date or time, for example 2026-10-01.")
    refresh = commands.add_parser(
        "dictionary",
        help="Download CC-CEDICT if it is missing and report its date.",
    )
    refresh.add_argument(
        "--update", action="store_true", help="Download the latest copy."
    )
    for name in (
        "word",
        "words",
        "sentences",
        "flags",
        "stats",
        "reviews",
        "dictionary",
    ):
        _url(commands.choices[name])

    _import_parser(
        commands.add_parser("import", help="Add and revise items waiting for a check.")
    )
    return parser


def _import_parser(imports: argparse.ArgumentParser) -> None:
    actions = imports.add_subparsers(dest="action", required=True)
    actions.add_parser("format", help="Print the import text format.")
    add = actions.add_parser(
        "add", help="Add text in the import format to the items waiting for a check."
    )
    add.add_argument(
        "file", nargs="?", default="-", help="File to read, or - for stdin."
    )
    actions.add_parser("show", help="Show what the waiting items would add.")
    sentence = actions.add_parser("edit-sentence", help="Revise one sentence.")
    sentence.add_argument("id", type=int)
    sentence.add_argument("--words", help='Space-separated words, e.g. "我 很 好".')
    sentence.add_argument("--pinyin")
    sentence.add_argument("--translation")
    _inclusion(sentence)
    word_edit = actions.add_parser("edit-word", help="Revise one new word.")
    word_edit.add_argument("word")
    word_edit.add_argument("--pinyin")
    word_edit.add_argument(
        "--meanings",
        help="Meanings separated by newlines or \\n. Replaces the dictionary "
        "definition; an empty value restores it.",
    )
    _inclusion(word_edit)
    remove = actions.add_parser("remove-sentence", help="Drop a sentence.")
    remove.add_argument("id", type=int)
    for name, action in actions.choices.items():
        if name != "format":
            _url(action)


def _inclusion(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--include", dest="included", action="store_true", default=None)
    group.add_argument("--exclude", dest="included", action="store_false")


def _print(value: object) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _path(*parts: str | int) -> str:
    return "/" + "/".join(quote(str(part), safe="") for part in parts)


def _query(path: str, **params: str | bool | None) -> str:
    present = {
        name: "1" if value is True else value
        for name, value in params.items()
        if value not in {None, False}
    }
    return f"{path}?{urlencode(present)}" if present else path


def _import(args: argparse.Namespace) -> Call:
    if args.action == "add":
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
        return "POST", "/pending", {"text": text}
    if args.action == "edit-sentence":
        return (
            "POST",
            _path("pending", "sentences", args.id),
            {
                "words": args.words,
                "pinyin": args.pinyin,
                "translation": args.translation,
                "included": args.included,
            },
        )
    if args.action == "edit-word":
        return (
            "POST",
            _path("pending", "words", args.word),
            {
                "pinyin": args.pinyin,
                "meanings": None
                if args.meanings is None
                else args.meanings.replace("\\n", "\n"),
                "included": args.included,
            },
        )
    if args.action == "remove-sentence":
        return "POST", _path("pending", "sentences", args.id, "remove"), {}
    return "GET", "/pending", None


CALLS: dict[str, Callable[[argparse.Namespace], Call]] = {
    "word": lambda args: ("GET", _path("words", args.word), None),
    "words": lambda args: (
        "GET",
        _query(
            "/words", status=args.status, flagged=args.flagged, backlog=args.backlog
        ),
        None,
    ),
    "sentences": lambda args: ("GET", _query("/sentences", status=args.status), None),
    "flags": lambda _args: ("GET", "/flags", None),
    "stats": lambda _args: ("GET", "/stats", None),
    "reviews": lambda args: ("GET", _query("/reviews", since=args.since), None),
    "dictionary": lambda args: (
        ("POST", "/dictionary", {}) if args.update else ("GET", "/dictionary", None)
    ),
    "import": _import,
}


class RequestError(Exception):
    def __init__(self, payload: object) -> None:
        super().__init__(payload)
        self.payload = payload


def _send(base: str, call: Call) -> object:
    method, path, body = call
    url = f"{base.rstrip('/')}/api/agent{path}"
    request = urllib.request.Request(  # noqa: S310
        url,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request) as response:  # noqa: S310
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            payload = json.load(error)
        except ValueError:
            payload = {"error": f"{url} answered {error.code} {error.reason}."}
        raise RequestError(payload) from error
    except urllib.error.URLError as error:
        message = f"Could not reach yiju at {base}: {error.reason}"
        raise RequestError({"error": message}) from error


def _serve(args: argparse.Namespace) -> None:
    from waitress import serve

    from .web import create_app

    serve(
        create_app(args.data_dir / "yiju.sqlite3", prefill=True),
        host=args.host,
        port=args.port,
        threads=4,
    )


def _backup(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    source = args.data_dir / "yiju.sqlite3"
    if not source.is_file() or args.destination.exists():
        parser.error("The source database must exist and the destination must be new.")
    with sqlite3.connect(
        f"file:{source.resolve().as_posix()}?mode=ro", uri=True
    ) as database:
        with sqlite3.connect(args.destination) as destination:
            database.backup(destination)
        destination.close()
    database.close()


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if args.command == "serve":
        _serve(args)
        return
    if args.command == "backup":
        _backup(parser, args)
        return
    if args.command == "import" and args.action == "format":
        _print({"format": pending.FORMAT})
        return
    if not args.url.startswith(("http://", "https://")):
        parser.error("--url must start with http:// or https://.")
    try:
        _print(_send(args.url, CALLS[args.command](args)))
    except RequestError as error:
        _print(error.payload)
        sys.exit(1)
    except OSError as error:
        _print({"error": str(error)})
        sys.exit(1)
