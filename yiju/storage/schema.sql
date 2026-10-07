CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    digest TEXT NOT NULL UNIQUE,
    created TEXT NOT NULL,
    summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
    id TEXT PRIMARY KEY,
    word TEXT NOT NULL,
    traditional TEXT,
    pinyin TEXT NOT NULL,
    definition TEXT NOT NULL,
    raw_definition TEXT NOT NULL,
    references_json TEXT NOT NULL,
    import_id INTEGER NOT NULL REFERENCES imports(id),
    definition_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS entries_word ON entries(word);
CREATE TABLE IF NOT EXISTS sentences (
    id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES entries(id),
    chinese TEXT NOT NULL,
    pinyin TEXT NOT NULL,
    translation TEXT NOT NULL,
    tokens TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'accepted' CHECK(status IN ('accepted', 'rejected')),
    version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cards (
    id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES entries(id),
    sentence_id TEXT REFERENCES sentences(id),
    word TEXT NOT NULL,
    scheduling TEXT NOT NULL,
    due TEXT NOT NULL,
    reviews INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    suspended INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS cards_due ON cards(due);
CREATE TABLE IF NOT EXISTS knowledge (
    word TEXT PRIMARY KEY,
    scheduling TEXT NOT NULL,
    success_days INTEGER NOT NULL DEFAULT 0,
    last_success TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    card_id TEXT NOT NULL REFERENCES cards(id),
    word TEXT NOT NULL,
    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 4),
    created TEXT NOT NULL,
    card_before TEXT NOT NULL,
    knowledge_before TEXT,
    undone INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS reviews_created ON reviews(created);
CREATE INDEX IF NOT EXISTS reviews_card ON reviews(card_id, id);
CREATE TABLE IF NOT EXISTS review_words (
    review_id INTEGER NOT NULL REFERENCES reviews(id),
    word TEXT NOT NULL,
    knowledge_before TEXT,
    PRIMARY KEY(review_id, word)
);
