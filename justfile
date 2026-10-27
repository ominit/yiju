help:
    @just --list

sync:
    uv sync --locked

format:
    nix fmt

lint:
    ruff check .
    ty check

test:
    uv run --locked python -m pytest

build:
    uv build --no-sources

[positional-arguments]
run *args:
    uv run --locked yiju serve "$@"

[positional-arguments]
backup destination:
    uv run --locked yiju backup "$1"

check: format lint test
