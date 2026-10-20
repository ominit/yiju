import sqlite3
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property

from flask import abort, render_template, request, url_for

from yiju.core.definitions import search_text, summary
from yiju.core.dictionary import decode, readings_pinyin
from yiju.storage.database import parameters, settings
from yiju.storage.graph import MAX_UNLEARNED
from yiju.storage.sentences import status as sentence_status
from yiju.storage.words import status, strength

from . import database

PAGE_SIZE = 50
WORD_FILTERS = {
    "all": "All",
    "learning": "Learning",
    "learned": "Learned",
    "unlocked": "New",
    "locked": "Locked",
    "backlog": "No sentence",
    "flagged": "Flagged",
    "hidden": "Paused",
}
SENTENCE_FILTERS = {
    "all": "All",
    "learning": "Learning",
    "learned": "Learned",
    "unlocked": "New",
    "eligible": "Eligible",
    "locked": "Locked",
    "hidden": "Skipped",
}
SORTS = {
    "status": "By status",
    "recent": "Recently added",
    "length": "Shortest first",
}
STATUS_ORDER = {"learning": 0, "unlocked": 1, "eligible": 1, "locked": 2, "learned": 3}
CARD_TYPES = {"recognition": "Read", "production": "Write"}


@dataclass(slots=True)
class LibraryItem:
    row: sqlite3.Row
    key: str
    text: str
    pinyin: str
    meaning: str
    status: str
    hidden: bool
    search: str
    backlog: bool = False


class Library:
    def __init__(self, connection: sqlite3.Connection, now: datetime) -> None:
        self.connection = connection
        self.now = now
        self.config = settings(connection)
        self.parameters = parameters(connection)
        self.flags = {
            row["word"]: row["note"]
            for row in connection.execute("SELECT word, note FROM flags")
        }

    @cached_property
    def cards(self) -> dict[str, list[sqlite3.Row]]:
        cards: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for card in self.connection.execute("SELECT * FROM cards ORDER BY id"):
            key = card["word"] if card["word"] is not None else str(card["sentence_id"])
            cards[key].append(card)
        return cards

    def words(self, word: str | None = None) -> list[LibraryItem]:
        rows = self.connection.execute(
            "SELECT w.*, w.rowid AS added, NOT EXISTS (SELECT 1 FROM sentence_words sw "
            "JOIN sentences s ON s.id = sw.sentence_id WHERE sw.word = w.word "
            "AND s.hidden = 0) AS backlog, "
            "NOT EXISTS (SELECT 1 FROM cards c WHERE c.word = w.word "
            "AND c.suspended = 0) AS paused "
            "FROM words w WHERE ? IS NULL OR w.word = ?",
            (word, word),
        )
        return [
            LibraryItem(
                row,
                row["word"],
                row["word"],
                readings_pinyin(decode(row["readings"])),
                summary(row["readings"]),
                status(row),
                bool(row["paused"]),
                search_text(row["readings"]),
                bool(row["backlog"]),
            )
            for row in rows
        ]

    def sentences(
        self, *, sentence_id: int | None = None, word: str | None = None
    ) -> list[LibraryItem]:
        return [
            LibraryItem(
                row,
                str(row["id"]),
                row["chinese"],
                row["pinyin"],
                row["translation"],
                sentence_status(row, self.cards[str(row["id"])], self.parameters),
                bool(row["hidden"]),
                row["pinyin"],
            )
            for row in self.connection.execute(
                "SELECT s.*, s.id AS added FROM sentences s "
                "WHERE (? IS NULL OR s.id = ?) AND (? IS NULL OR s.id IN "
                "(SELECT sentence_id FROM sentence_words WHERE word = ?)) "
                "ORDER BY s.id",
                (sentence_id, sentence_id, word, word),
            )
        ]

    def tokens(self, sentence_id: int) -> list[dict[str, str]]:
        return [
            {"text": row["word"], "status": status(row)}
            for row in self.connection.execute(
                "SELECT sw.word, w.state, w.learned FROM sentence_words sw "
                "JOIN words w ON w.word = sw.word WHERE sw.sentence_id = ? "
                "ORDER BY sw.position",
                (sentence_id,),
            )
        ]


def _sort_key(item: LibraryItem, sort: str) -> tuple[object, ...]:
    if sort == "recent":
        return (-item.row["added"],)
    if sort == "length":
        return (len(item.text), item.text)
    return (5 if item.hidden else STATUS_ORDER[item.status], len(item.text), item.text)


def _search_text(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character)
    )


def _matches(item: LibraryItem, state: str, flags: dict[str, str]) -> bool:
    if state == "flagged":
        return item.text in flags
    if state == "hidden":
        return item.hidden
    if item.hidden:
        return False
    if state == "backlog":
        return item.backlog
    return state in {"all", item.status}


def library_response(kind: str, now: datetime) -> str:
    library = Library(database(), now)
    items = library.words() if kind == "words" else library.sentences()
    filters = WORD_FILTERS if kind == "words" else SENTENCE_FILTERS
    state = request.args.get("filter", "all")
    sort = request.args.get("sort", "status")
    if state not in filters or sort not in SORTS:
        abort(400)
    query = request.args.get("q", "").strip()[:100]
    opened = request.args.get("open", "")
    normalized_query = _search_text(query)
    counts = Counter(item.status for item in items if not item.hidden)
    counts["all"] = sum(not item.hidden for item in items)
    counts["hidden"] = sum(item.hidden for item in items)
    counts["backlog"] = sum(item.backlog and not item.hidden for item in items)
    counts["flagged"] = sum(item.text in library.flags for item in items)
    selected = [
        item
        for item in items
        if _matches(item, state, library.flags)
        and (
            not query
            or normalized_query
            in _search_text(f"{item.text} {item.pinyin} {item.meaning} {item.search}")
        )
    ]
    selected.sort(key=lambda item: (item.key != opened, *_sort_key(item, sort)))
    total = len(selected)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(pages, max(1, request.args.get("page", 1, type=int)))
    selected = selected[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    details = next(
        (_details(library, kind, item) for item in selected if item.key == opened),
        None,
    )
    return render_template(
        "library.html",
        kind=kind,
        items=selected,
        filters=list(filters.items()),
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
        word_count=library.connection.execute("SELECT count(*) FROM words").fetchone()[
            0
        ],
        sentence_count=library.connection.execute(
            "SELECT count(*) FROM sentences"
        ).fetchone()[0],
    )


def _card_rows(cards: list[sqlite3.Row]) -> list[dict[str, object]]:
    return [
        {
            "label": CARD_TYPES.get(card["card_type"], card["card_type"]),
            "reviews": card["reviews"],
            "due": card["due"],
            "new": not card["reviews"],
            "suspended": bool(card["suspended"]),
        }
        for card in cards
    ]


def _audio(cards: list[sqlite3.Row]) -> str:
    return url_for("audio.pronunciation", card_id=cards[0]["id"])


def _word_reason(library: Library, item: LibraryItem) -> str:
    row = item.row
    if row["state"] == "seen":
        return ""
    sources = library.connection.execute(
        "SELECT count(*) FROM sentence_words sw JOIN sentences s "
        "ON s.id = sw.sentence_id WHERE sw.word = ? AND s.state = 'eligible' "
        "AND s.hidden = 0",
        (item.text,),
    ).fetchone()[0]
    if sources:
        return f"Unlocked by {sources} eligible sentence{'s' if sources != 1 else ''}."
    if row["manual"]:
        return "Unlocked by hand."
    if item.backlog:
        return "No sentence contains this word yet."
    return "Locked until a sentence containing it becomes eligible."


def _sentence_reason(
    library: Library, tokens: list[dict[str, str]], item: LibraryItem
) -> str:
    words = {token["text"]: token["status"] for token in tokens}
    unlearned = sum(status != "learned" for status in words.values())
    unseen = sum(status in {"locked", "unlocked"} for status in words.values())
    if item.hidden:
        return "Skipped sentences never appear in study."
    goal = library.parameters.learned_stability
    if item.status == "learned":
        return f"Both cards have at least {goal:g} days of memory strength."
    if item.status == "learning":
        return f"Learned once both cards reach {goal:g} days of memory strength."
    if item.status == "unlocked":
        return "Every word has been seen, so this sentence is in study."
    if item.status == "eligible":
        return (
            f"{unseen} word{'s' if unseen != 1 else ''} still to see. "
            "The sentence unlocks once you have seen every word."
        )
    return (
        f"{unlearned} words are not learned yet. It becomes eligible at "
        f"{MAX_UNLEARNED} or fewer."
    )


def _details(library: Library, kind: str, item: LibraryItem) -> dict[str, object]:
    cards = library.cards[item.key]
    common: dict[str, object] = {
        "cards": _card_rows(cards),
        "audio": _audio(cards),
        "parameters": library.parameters,
    }
    if kind == "sentences":
        tokens = library.tokens(int(item.key))
        return common | {
            "tokens": tokens,
            "reason": _sentence_reason(library, tokens, item),
        }
    current = strength(item.row)
    recall = current.recall(library.now)
    return common | {
        "readings": decode(item.row["readings"]),
        "traditional": ", ".join(
            dict.fromkeys(
                form
                for reading in decode(item.row["readings"])
                for form in reading.traditional
            )
        ),
        "note": library.flags.get(item.text, ""),
        "strength": {
            "stability": round(current.stability, 1),
            "days": current.success_days,
            "recall": None if recall is None else round(recall * 100),
        },
        "reason": _word_reason(library, item),
        "sentences": [
            (sentence, library.tokens(int(sentence.key)))
            for sentence in library.sentences(word=item.text)
        ],
    }


def _item(library: Library, kind: str, key: str) -> LibraryItem:
    items: list[LibraryItem] = []
    if kind == "words":
        items = library.words(key)
    elif key.isdigit():
        items = library.sentences(sentence_id=int(key))
    if not items:
        abort(404)
    return items[0]


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
