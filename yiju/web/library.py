import json
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property

from flask import abort, render_template, request, url_for

from yiju.core.definitions import combine_definitions
from yiju.core.progress import LearningProgress, learning_progress
from yiju.core.selection import Analysis, analyze
from yiju.storage.database import settings, vocabulary_words
from yiju.storage.progress import sentence_success_days

from . import database

PAGE_SIZE = 50
SORTS = {
    "learning": "Learning first",
    "progress": "Most learned",
    "recent": "Recently added",
    "length": "Shortest first",
}
DIRECTIONS = {
    "recognition": "Read",
    "production": "Write",
}


@dataclass(slots=True)
class LibraryItem:
    row: sqlite3.Row
    text: str
    pinyin: str
    progress: LearningProgress
    cards: list[sqlite3.Row]
    reviews: int
    hidden: bool
    analysis: Analysis | None = None
    meaning: str = ""
    ready: bool = True
    state: str = field(init=False)

    def __post_init__(self) -> None:
        self.state = self.progress.state


class Library:
    def __init__(self, connection: sqlite3.Connection, now: datetime) -> None:
        self.connection = connection
        self.now = now
        self.config = settings(connection)
        self.flags = {
            row["word"]: row["note"]
            for row in connection.execute("SELECT word, note FROM word_flags")
        }
        self.knowledge = {
            row["word"]: learning_progress(
                row["scheduling"], row["success_days"], self.config, now
            )
            for row in connection.execute("SELECT * FROM knowledge")
        }
        self.known = frozenset(
            word
            for word, progress in self.knowledge.items()
            if progress.state == "learned"
        )

    @cached_property
    def vocabulary(self) -> frozenset[str]:
        return vocabulary_words(self.connection)

    @cached_property
    def word_cards(self) -> dict[str, list[sqlite3.Row]]:
        cards: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for card in self.connection.execute(
            "SELECT * FROM cards WHERE sentence_id IS NULL ORDER BY direction DESC"
        ):
            cards[card["word"]].append(card)
        return cards

    @cached_property
    def sentence_cards(self) -> dict[str, list[sqlite3.Row]]:
        cards: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for card in self.connection.execute(
            "SELECT * FROM cards WHERE sentence_id IS NOT NULL ORDER BY direction DESC"
        ):
            cards[card["sentence_id"]].append(card)
        return cards

    def words(self, word: str | None = None) -> list[LibraryItem]:
        reviews = {
            row["word"]: row["n"]
            for row in self.connection.execute(
                "SELECT w.word, count(*) AS n FROM review_words w "
                "JOIN reviews r ON r.id = w.review_id WHERE r.undone = 0 "
                "GROUP BY w.word"
            )
        }
        rows = self.connection.execute(
            "WITH definitions AS (SELECT word, group_concat(definition_json, "
            "char(10)) AS definitions_json, "
            "group_concat(DISTINCT pinyin) AS all_pinyin, "
            "group_concat((SELECT group_concat(json_extract(value, '$.meaning'), ' ') "
            "FROM json_each(entries.definition_json, '$.senses'))) AS search_text "
            "FROM entries GROUP BY word) "
            "SELECT e.*, e.rowid AS import_order, d.definitions_json, d.all_pinyin, "
            "d.search_text, c.id AS card_id, c.version, c.suspended "
            "FROM cards c JOIN entries e ON e.id = c.entry_id "
            "JOIN definitions d ON d.word = e.word WHERE c.sentence_id IS NULL AND "
            "c.direction = 'recognition' AND (? IS NULL OR e.word = ?)",
            (word, word),
        )
        return [
            LibraryItem(
                row,
                row["word"],
                row["all_pinyin"],
                self.knowledge.get(row["word"], LearningProgress()),
                self.word_cards[row["word"]],
                reviews.get(row["word"], 0),
                bool(row["suspended"]),
            )
            for row in rows
        ]

    def sentences(
        self, where: str = "", parameters: tuple[str, ...] = ()
    ) -> list[LibraryItem]:
        days = sentence_success_days(self.connection)
        items = []
        for row in self.connection.execute(
            "SELECT s.*, s.rowid AS import_order, e.word, e.raw_definition "
            "FROM sentences s JOIN entries e ON e.id = s.entry_id " + where,
            parameters,
        ):
            cards = self.sentence_cards[row["id"]]
            reviewed = [card for card in cards if card["reviews"]]
            progress = max(
                (
                    learning_progress(
                        card["scheduling"],
                        days.get(row["id"], 0),
                        self.config,
                        self.now,
                    )
                    for card in reviewed
                ),
                key=lambda value: value.percent,
                default=LearningProgress(),
            )
            analysis = analyze(
                tuple(json.loads(row["tokens"])),
                self.known,
                row["word"],
                int(self.config["max_unknown"]),
                include_zero=bool(self.config["include_zero"]) or bool(reviewed),
                vocabulary=self.vocabulary,
            )
            items.append(
                LibraryItem(
                    row,
                    row["chinese"],
                    row["pinyin"],
                    progress,
                    cards,
                    sum(card["reviews"] for card in cards),
                    row["status"] == "rejected",
                    analysis=analysis,
                    meaning=row["translation"],
                    ready=not analysis.missing
                    and (bool(reviewed) or analysis.eligible),
                )
            )
        return items


def _sort_key(item: LibraryItem, sort: str) -> tuple[object, ...]:
    if sort == "recent":
        return (-item.row["import_order"],)
    if sort == "progress":
        return (-item.progress.percent, item.text)
    if sort == "length":
        return (len(item.text), item.text)
    rank = {"learning": 0, "new": 1, "learned": 3}[item.state]
    return (
        5 if item.hidden else rank if item.ready else 4,
        item.progress.percent,
        len(item.text),
        item.text,
    )


def _search_text(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character)
    )


def _summary(definitions_json: str, limit: int = 3) -> str:
    meanings = dict.fromkeys(
        sense.meaning
        for sense in combine_definitions(definitions_json).senses
        if sense.meaning
    )
    return "; ".join(list(meanings)[:limit])


def item_key(kind: str, item: LibraryItem) -> str:
    return item.text if kind == "words" else str(item.row["id"])


def library_response(kind: str, now: datetime) -> str:
    library = Library(database(), now)
    items = library.words() if kind == "words" else library.sentences()
    filters = [("all", "All"), ("learning", "Learning"), ("learned", "Learned")]
    filters.append(("new", "New"))
    if kind == "sentences":
        filters.append(("ready", "Ready"))
    else:
        filters.append(("flagged", "Flagged"))
    filters.append(("hidden", "Paused" if kind == "words" else "Skipped"))
    state = request.args.get("filter", "all")
    sort = request.args.get("sort", "learning")
    if state not in dict(filters) or sort not in SORTS:
        abort(400)
    query = request.args.get("q", "").strip()[:100]
    opened = request.args.get("open", "")
    normalized_query = _search_text(query)
    counts = Counter(item.state for item in items if not item.hidden)
    counts["all"] = sum(not item.hidden for item in items)
    counts["hidden"] = sum(item.hidden for item in items)
    counts["ready"] = sum(item.ready and not item.hidden for item in items)
    counts["flagged"] = sum(item.text in library.flags for item in items)
    selected = [
        item
        for item in items
        if (
            item.text in library.flags
            if state == "flagged"
            else item.hidden
            if state == "hidden"
            else not item.hidden
            and (
                state == "all"
                or (state == "ready" and item.ready)
                or item.state == state
            )
        )
        and (
            not query
            or normalized_query
            in _search_text(
                " ".join(
                    (
                        item.text,
                        item.pinyin,
                        item.meaning,
                        (item.row["search_text"] or "") if kind == "words" else "",
                    )
                )
            )
        )
    ]
    selected.sort(
        key=lambda item: (item_key(kind, item) != opened, *_sort_key(item, sort))
    )
    total = len(selected)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(pages, max(1, request.args.get("page", 1, type=int)))
    selected = selected[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    if kind == "words":
        for item in selected:
            item.meaning = _summary(item.row["definitions_json"])
    details = next(
        (
            _details(library, kind, item)
            for item in selected
            if item_key(kind, item) == opened
        ),
        None,
    )
    return render_template(
        "library.html",
        kind=kind,
        items=selected,
        filters=filters,
        counts=counts,
        state=state,
        sort=sort,
        sorts=SORTS,
        q=query,
        page=page,
        pages=pages,
        total=total,
        flags=library.flags,
        opened=opened,
        details=details,
        word_count=len(library.word_cards),
        sentence_count=library.connection.execute(
            "SELECT count(*) FROM sentences"
        ).fetchone()[0],
    )


def _card_rows(cards: list[sqlite3.Row]) -> list[dict[str, object]]:
    order = list(DIRECTIONS)
    return [
        {
            "label": DIRECTIONS[card["direction"]],
            "reviews": card["reviews"],
            "due": card["due"],
            "new": not card["reviews"],
            "suspended": bool(card["suspended"]),
        }
        for card in sorted(cards, key=lambda card: order.index(card["direction"]))
        if card["direction"] in DIRECTIONS
    ]


def _audio(cards: list[sqlite3.Row]) -> str:
    card = next(card for card in cards if card["direction"] == "recognition")
    return url_for("audio.pronunciation", card_id=card["id"])


def _details(library: Library, kind: str, item: LibraryItem) -> dict[str, object]:
    common = {
        "cards": _card_rows(item.cards),
        "config": library.config,
        "audio": _audio(item.cards),
    }
    if kind == "sentences":
        return common | {"vocabulary": library.vocabulary}
    entries = library.connection.execute(
        "SELECT * FROM entries WHERE word = ? ORDER BY rowid", (item.text,)
    ).fetchall()
    return common | {
        "traditional": ", ".join(
            dict.fromkeys(
                row["traditional"]
                for row in entries
                if row["traditional"] and row["traditional"] != item.text
            )
        ),
        "definition": combine_definitions(item.row["definitions_json"]),
        "note": library.flags.get(item.text, ""),
        "sentences": library.sentences(
            "WHERE e.word = ? ORDER BY s.rowid", (item.text,)
        ),
    }


def _item(library: Library, kind: str, key: str) -> LibraryItem:
    items = (
        library.words(key)
        if kind == "words"
        else library.sentences("WHERE s.id = ?", (key,))
    )
    if not items:
        abort(404)
    item = items[0]
    if kind == "words":
        item.meaning = _summary(item.row["definitions_json"])
    return item


def item_response(kind: str, key: str, now: datetime) -> str:
    library = Library(database(), now)
    item = _item(library, kind, key)
    return render_template(
        "library_item.html",
        kind=kind,
        item=item,
        flags=library.flags,
        details=_details(library, kind, item),
    )


def item_location(kind: str, key: str, now: datetime) -> str:
    item = _item(Library(database(), now), kind, key)
    return url_for(
        "pages.library" if kind == "words" else "pages.sentences",
        q=item.text[:100],
        open=key,
    )
