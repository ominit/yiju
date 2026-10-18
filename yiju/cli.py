import argparse
import os
import sqlite3
from pathlib import Path

from waitress import serve

from .web import create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="yiju")
    subcommands = parser.add_subparsers(dest="command", required=True)
    server = subcommands.add_parser("serve")
    server.add_argument("--host", default=os.environ.get("YIJU_HOST", "0.0.0.0"))
    server.add_argument(
        "--port", type=int, default=int(os.environ.get("YIJU_PORT", "8000"))
    )
    server.add_argument(
        "--data-dir", type=Path, default=Path(os.environ.get("YIJU_DATA_DIR", "data"))
    )
    backup = subcommands.add_parser("backup")
    backup.add_argument("destination", type=Path)
    backup.add_argument(
        "--data-dir", type=Path, default=Path(os.environ.get("YIJU_DATA_DIR", "data"))
    )
    args = parser.parse_args()
    if args.command == "serve":
        serve(
            create_app(args.data_dir / "yiju.sqlite3", prefill=True),
            host=args.host,
            port=args.port,
            threads=4,
        )
    else:
        source = args.data_dir / "yiju.sqlite3"
        if not source.is_file() or args.destination.exists():
            parser.error(
                "The source database must exist and the destination must be new."
            )
        with sqlite3.connect(
            f"file:{source.resolve().as_posix()}?mode=ro", uri=True
        ) as database:
            with sqlite3.connect(args.destination) as destination:
                database.backup(destination)
            destination.close()
        database.close()
