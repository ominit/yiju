import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from .core import dictionary
from .core.scheduling import now_utc
from .storage import pending
from .storage.database import ConflictError, connect, initialize
from .storage.export import (
    SENTENCE_STATUSES,
    WORD_STATUSES,
    flagged_words,
    list_sentences,
    list_words,
    review_log,
    word_info,
)
from .storage.queue import overview

AGENT_HELP = """\
Commands other than serve and backup print JSON and are meant for scripts and
agents. Imports never change the library directly: `yiju import add` puts the
items on the check screen in the web app, next to anything else waiting there,
and prints exactly what approving would add. Agents can revise waiting items
with the edit commands, but only the user can approve them.
"""


def _data_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--data-dir", type=Path, default=Path(os.environ.get("YIJU_DATA_DIR", "data"))
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
        _data_dir(commands.choices[name])

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
    for action in actions.choices.values():
        _data_dir(action)


def _inclusion(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--include", dest="included", action="store_true", default=None)
    group.add_argument("--exclude", dest="included", action="store_false")


def _print(value: object) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _pending(connection: sqlite3.Connection) -> dict[str, Any]:
    result = pending.preview(connection).to_json()
    result["check_screen"] = "/pending"
    return result


def _import(connection: sqlite3.Connection, args: argparse.Namespace) -> object:
    if args.action == "format":
        return {"format": pending.FORMAT}
    if args.action == "add":
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
        pending.stage(connection, text)
        return _pending(connection)
    if args.action == "show":
        return _pending(connection)
    if args.action == "edit-word" and args.word not in {
        word.word for word in pending.preview(connection).words
    }:
        message = f"{args.word} is not a new word waiting for a check."
        raise LookupError(message)
    with pending.editing(connection):
        if args.action == "edit-sentence":
            pending.edit_sentence(
                connection,
                args.id,
                words=args.words,
                pinyin=args.pinyin,
                translation=args.translation,
                included=args.included,
            )
        elif args.action == "edit-word":
            pending.edit_word(
                connection,
                args.word,
                pinyin=args.pinyin,
                meanings=None
                if args.meanings is None
                else args.meanings.replace("\\n", "\n"),
                included=args.included,
            )
        else:
            pending.remove_sentence(connection, args.id)
    return _pending(connection)


def _word(connection: sqlite3.Connection, args: argparse.Namespace) -> object:
    info = word_info(connection, args.word, now_utc())
    if info is None:
        message = f"{args.word} is not in the library."
        raise LookupError(message)
    return info


def _dictionary(_connection: sqlite3.Connection, args: argparse.Namespace) -> object:
    if args.update:
        published = dictionary.update()
    else:
        dictionary.ensure()
        published = dictionary.published()
    return {"path": str(dictionary.SOURCE.path), "published": published}


QUERIES: dict[str, Callable[[sqlite3.Connection, argparse.Namespace], object]] = {
    "word": _word,
    "words": lambda connection, args: list_words(
        connection,
        word_status=args.status,
        flagged=args.flagged,
        backlog=args.backlog,
    ),
    "sentences": lambda connection, args: list_sentences(connection, args.status),
    "flags": lambda connection, _args: flagged_words(connection),
    "stats": lambda connection, _args: overview(connection, now_utc()),
    "reviews": lambda connection, args: review_log(connection, args.since),
    "import": _import,
    "dictionary": _dictionary,
}


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
    path = args.data_dir / "yiju.sqlite3"
    initialize(path)
    dictionary.use(args.data_dir / dictionary.FILENAME)
    with closing(connect(path)) as connection:
        try:
            _print(QUERIES[args.command](connection, args))
        except pending.FormatError as error:
            _print(
                {
                    "error": "The text is not in the import format.",
                    "lines": error.errors,
                }
            )
            sys.exit(1)
        except (LookupError, ConflictError, ValueError, OSError) as error:
            _print({"error": str(error)})
            sys.exit(1)
