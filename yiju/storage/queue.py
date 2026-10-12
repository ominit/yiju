import json
import sqlite3
from datetime import UTC, datetime, timedelta
from random import SystemRandom
from typing import cast

from yiju.core.definitions import recall_prompt
from yiju.core.selection import analyze
from yiju.core.text import contains_han

from .database import settings, vocabulary_words
from .learning import known_sentences, known_words

CARD_QUERY = (
    "SELECT c.*, CASE WHEN c.sentence_id IS NULL THEN "
    "(SELECT group_concat(DISTINCT e2.pinyin) FROM entries e2 "
    "WHERE e2.word = c.word) ELSE e.pinyin END AS word_pinyin, "
    "CASE WHEN c.sentence_id IS NULL THEN "
    "(SELECT group_concat(e2.definition, char(10) || char(10)) FROM entries e2 "
    "WHERE e2.word = c.word) ELSE e.definition END AS definition, "
    "CASE WHEN c.sentence_id IS NULL THEN "
    "(SELECT group_concat(e2.definition_json, char(10)) FROM entries e2 "
    "WHERE e2.word = c.word) ELSE e.definition_json END AS definition_json, "
    "s.chinese, s.pinyin AS sentence_pinyin, s.translation, s.tokens, "
    "s.version AS sentence_version, "
    "EXISTS(SELECT 1 FROM cards sibling WHERE sibling.sentence_id = c.sentence_id "
    "AND sibling.reviews > 0) AS sentence_started "
    "FROM cards c JOIN entries e ON e.id = c.entry_id "
    "LEFT JOIN sentences s ON s.id = c.sentence_id "
)
ELIGIBLE_FILTER = (
    "WHERE c.suspended = 0 AND c.due <= ? "
    "AND NOT EXISTS (SELECT 1 FROM reviews r "
    "JOIN cards sibling ON sibling.id = r.card_id "
    "WHERE r.undone = 0 AND r.created >= ? AND r.created < ? "
    "AND r.card_id != c.id AND "
    "((c.sentence_id IS NULL AND sibling.sentence_id IS NULL "
    "AND sibling.word = c.word) OR "
    "(c.sentence_id IS NOT NULL AND sibling.sentence_id = c.sentence_id))) "
)


def card_family(card: sqlite3.Row) -> str:
    return (
        f"sentence:{card['sentence_id']}"
        if card["sentence_id"] is not None
        else f"word:{card['word']}"
    )


def _new_card_order(cards: list[sqlite3.Row]) -> list[sqlite3.Row]:
    random = SystemRandom()
    random.shuffle(cards)
    directions = [
        [card for card in cards if card["direction"] == direction]
        for direction in ("recognition", "production")
    ]
    random.shuffle(directions)
    ordered = []
    siblings = []
    seen: set[str] = set()
    while any(directions):
        for direction in directions:
            while direction and card_family(direction[-1]) in seen:
                siblings.append(direction.pop())
            if direction:
                card = direction.pop()
                ordered.append(card)
                seen.add(card_family(card))
    return ordered + siblings


def _family_cards(
    cards: list[sqlite3.Row], preferred_card_id: str | None
) -> list[sqlite3.Row]:
    if preferred_card_id is not None:
        cards.sort(key=lambda row: row["id"] != preferred_card_id)
    seen: set[str] = set()
    selected = []
    for card in cards:
        family = card_family(card)
        if family not in seen:
            selected.append(card)
            seen.add(family)
    return selected


def study_queue(
    connection: sqlite3.Connection,
    now: datetime,
    *,
    card_id: str | None = None,
    preferred_card_id: str | None = None,
) -> list[sqlite3.Row]:
    now = now.astimezone(UTC)
    day_start = now.date().isoformat()
    day_end = (now.date() + timedelta(days=1)).isoformat()
    config = settings(connection)
    known = known_words(connection, now)
    started = connection.execute(
        "SELECT count(*) FROM reviews WHERE undone = 0 "
        "AND created >= ? AND created < ? "
        "AND json_extract(card_before, '$.reviews') = 0",
        (day_start, day_end),
    ).fetchone()[0]
    remaining = max(0, int(config["new_per_day"]) - started)
    due = cast(
        "list[sqlite3.Row]",
        connection.execute(
            CARD_QUERY
            + ELIGIBLE_FILTER
            + ("AND c.id = ? " if card_id is not None else "")
            + "ORDER BY (c.reviews = 0), c.due, c.id",
            (now.isoformat(), day_start, day_end, card_id)
            if card_id is not None
            else (now.isoformat(), day_start, day_end),
        ).fetchall(),
    )
    vocabulary = vocabulary_words(connection)
    due = [
        row
        for row in due
        if (
            row["sentence_id"] is None
            or all(token in vocabulary for token in json.loads(row["tokens"]))
        )
        and (
            row["direction"] != "production"
            or (
                bool(row["translation"].strip())
                and not contains_han(row["translation"])
                if row["sentence_id"] is not None
                else bool(recall_prompt(row["definition_json"]))
            )
        )
    ]
    reviews = [row for row in due if row["reviews"] > 0]
    new_sentences: list[tuple[float, sqlite3.Row]] = []
    new_words = []
    for row in due:
        if row["reviews"]:
            continue
        if row["sentence_id"] is None:
            if row["word"] not in known or row["direction"] != "recognition":
                new_words.append(row)
        else:
            analysis = analyze(
                tuple(json.loads(row["tokens"])),
                known,
                row["word"],
                int(config["max_unknown"]),
                include_zero=bool(config["include_zero"])
                or bool(row["sentence_started"]),
                vocabulary=vocabulary,
            )
            if analysis.eligible:
                new_sentences.append((analysis.score, row))
    sentence_scores = {row["id"]: score for score, row in new_sentences}
    sentences = _new_card_order([row for _, row in new_sentences])
    sentences.sort(key=lambda row: -sentence_scores[row["id"]])
    selected = _family_cards(
        reviews + sentences + _new_card_order(new_words), preferred_card_id
    )
    return [row for row in selected if row["reviews"]] + [
        row for row in selected if not row["reviews"]
    ][:remaining]


def overview(connection: sqlite3.Connection, now: datetime) -> dict[str, int]:
    queue = study_queue(connection, now)
    return {
        "words": connection.execute(
            "SELECT count(DISTINCT word) FROM entries"
        ).fetchone()[0],
        "known": len(known_words(connection, now)),
        "due": sum(row["reviews"] > 0 for row in queue),
        "new": sum(row["reviews"] == 0 for row in queue),
        "sentences": connection.execute(
            "SELECT count(*) FROM sentences WHERE status = 'accepted'"
        ).fetchone()[0],
        "learned_sentences": len(known_sentences(connection, now)),
        "today": connection.execute(
            "SELECT count(*) FROM reviews WHERE undone = 0 AND created >= ?",
            (now.date().isoformat(),),
        ).fetchone()[0],
    }
