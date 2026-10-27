CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS words (
    word TEXT PRIMARY KEY,
    readings TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('cedict', 'manual')),
    state TEXT NOT NULL DEFAULT 'locked'
        CHECK (state IN ('locked', 'unlocked', 'seen')),
    manual INTEGER NOT NULL DEFAULT 0,
    memory TEXT,
    success_days INTEGER NOT NULL DEFAULT 0,
    last_success TEXT,
    learned INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS words_state ON words(state);

CREATE TABLE IF NOT EXISTS sentences (
    id INTEGER PRIMARY KEY,
    chinese TEXT NOT NULL UNIQUE,
    pinyin TEXT NOT NULL,
    translation TEXT NOT NULL,
    rules TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'locked'
        CHECK (state IN ('locked', 'eligible', 'unlocked')),
    hidden INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sentences_state ON sentences(state);

CREATE TABLE IF NOT EXISTS sentence_words (
    sentence_id INTEGER NOT NULL REFERENCES sentences(id),
    position INTEGER NOT NULL,
    word TEXT NOT NULL REFERENCES words(word),
    PRIMARY KEY (sentence_id, position)
);
CREATE INDEX IF NOT EXISTS sentence_words_word ON sentence_words(word);

CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY,
    word TEXT REFERENCES words(word),
    sentence_id INTEGER REFERENCES sentences(id),
    card_type TEXT NOT NULL,
    scheduling TEXT NOT NULL,
    due TEXT NOT NULL,
    reviews INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    suspended INTEGER NOT NULL DEFAULT 0,
    CHECK ((word IS NULL) != (sentence_id IS NULL)),
    UNIQUE (word, card_type),
    UNIQUE (sentence_id, card_type)
);
CREATE INDEX IF NOT EXISTS cards_due ON cards(due);
CREATE INDEX IF NOT EXISTS cards_sentence ON cards(sentence_id);

CREATE TABLE IF NOT EXISTS flags (
    word TEXT PRIMARY KEY REFERENCES words(word),
    note TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL,
    updated TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_sentences (
    id INTEGER PRIMARY KEY,
    chinese TEXT NOT NULL UNIQUE,
    words TEXT NOT NULL,
    pinyin TEXT NOT NULL,
    translation TEXT NOT NULL,
    rules TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS pending_words (
    word TEXT PRIMARY KEY,
    readings TEXT,
    requested INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS names (
    id INTEGER PRIMARY KEY,
    chinese TEXT NOT NULL UNIQUE,
    pinyin TEXT NOT NULL,
    english TEXT NOT NULL,
    gender TEXT NOT NULL CHECK (gender IN ('male', 'female')),
    created TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pending_names (
    id INTEGER PRIMARY KEY,
    chinese TEXT NOT NULL UNIQUE,
    pinyin TEXT NOT NULL,
    english TEXT NOT NULL,
    gender TEXT NOT NULL CHECK (gender IN ('male', 'female'))
);

CREATE TABLE IF NOT EXISTS parameter_sets (
    id INTEGER PRIMARY KEY,
    parameters TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    card_id INTEGER NOT NULL REFERENCES cards(id),
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 4),
    created TEXT NOT NULL,
    card_before TEXT NOT NULL,
    parameter_set INTEGER NOT NULL REFERENCES parameter_sets(id),
    undone INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS reviews_created ON reviews(created);
CREATE INDEX IF NOT EXISTS reviews_card ON reviews(card_id, id);

CREATE TABLE IF NOT EXISTS review_words (
    review_id INTEGER NOT NULL REFERENCES reviews(id),
    word TEXT NOT NULL REFERENCES words(word),
    role TEXT NOT NULL CHECK (role IN ('card', 'sentence', 'marked')),
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 4),
    weight REAL NOT NULL,
    recall_before REAL,
    state_before TEXT NOT NULL,
    memory_before TEXT,
    memory_after TEXT NOT NULL,
    success_days_before INTEGER NOT NULL,
    last_success_before TEXT,
    learned_before INTEGER NOT NULL,
    learned_after INTEGER NOT NULL,
    PRIMARY KEY (review_id, word)
);
CREATE INDEX IF NOT EXISTS review_words_word ON review_words(word);

CREATE TABLE IF NOT EXISTS review_changes (
    review_id INTEGER NOT NULL REFERENCES reviews(id),
    kind TEXT NOT NULL CHECK (kind IN ('word', 'sentence')),
    key TEXT NOT NULL,
    state_before TEXT NOT NULL,
    PRIMARY KEY (review_id, kind, key)
);
