-- Knowledge stores, files, documents and the search index
-- (range 0500-0599, task-11). Design sections 13.3 and 13.4.
--
-- Task-7 creates knowledge_stores (migration 0101). This file extends it.
--
-- knowledge_items holds the current files and documents of each store.
-- Files and documents share one path namespace in a store. A file is
-- Markdown text that agents and the user write. A document is an upload;
-- content holds its extracted text, and knowledge_document_sections holds
-- its sections with page numbers.
--
-- knowledge_item_versions holds every change of an item, with its author.
-- A delete is a version with deleted = 1. Version numbers continue across
-- a delete, so that a stale base_version always fails.
--
-- search_passages is the search index of all collections: knowledge store
-- passages, history, conversations and the HA entity catalogue. search_fts
-- is its FTS5 index. Triggers keep the two in step, also for rows that a
-- foreign key cascade deletes.
--
-- search_embeddings holds one vector per content hash and embedding model.
-- The vector is a float32 BLOB for sqlite-vec. The app loads sqlite-vec at
-- run time, so no statement here needs it.

ALTER TABLE knowledge_stores ADD COLUMN size_limit INTEGER NOT NULL DEFAULT 1000000;
ALTER TABLE knowledge_stores ADD COLUMN updated_at TEXT;

CREATE TABLE knowledge_items (
    id INTEGER PRIMARY KEY,
    store_id TEXT NOT NULL REFERENCES knowledge_stores (id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('file', 'document')),
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    size INTEGER NOT NULL,
    media_type TEXT,
    pages INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by_agent_id TEXT
) STRICT;

CREATE UNIQUE INDEX knowledge_items_path
    ON knowledge_items (store_id, path COLLATE NOCASE);

CREATE TABLE knowledge_item_versions (
    store_id TEXT NOT NULL REFERENCES knowledge_stores (id) ON DELETE CASCADE,
    path TEXT NOT NULL COLLATE NOCASE,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('file', 'document')),
    change TEXT NOT NULL,
    content TEXT,
    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
    author_agent_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (store_id, path, version)
) STRICT;

CREATE TABLE knowledge_document_sections (
    item_id INTEGER NOT NULL REFERENCES knowledge_items (id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    heading TEXT,
    page INTEGER,
    text TEXT NOT NULL,
    PRIMARY KEY (item_id, ordinal)
) STRICT;

CREATE TABLE search_passages (
    id INTEGER PRIMARY KEY,
    collection TEXT NOT NULL
        CHECK (collection IN ('knowledge', 'history', 'conversation', 'entity')),
    store_id TEXT REFERENCES knowledge_stores (id) ON DELETE CASCADE,
    agent_id TEXT,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    heading TEXT,
    page INTEGER,
    ordinal INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    charge_agent_id TEXT,
    source TEXT,
    record_type TEXT,
    at TEXT,
    CHECK (collection != 'knowledge' OR store_id IS NOT NULL)
) STRICT;

CREATE INDEX search_passages_store ON search_passages (collection, store_id, source_id);
CREATE INDEX search_passages_agent ON search_passages (collection, agent_id, source_id);
CREATE INDEX search_passages_hash ON search_passages (content_hash);

CREATE VIRTUAL TABLE search_fts USING fts5 (
    text,
    title,
    content = 'search_passages',
    content_rowid = 'id',
    tokenize = 'porter unicode61'
);

CREATE TRIGGER search_passages_ai AFTER INSERT ON search_passages BEGIN
    INSERT INTO search_fts (rowid, text, title) VALUES (new.id, new.text, new.title);
END;

CREATE TRIGGER search_passages_ad AFTER DELETE ON search_passages BEGIN
    INSERT INTO search_fts (search_fts, rowid, text, title)
        VALUES ('delete', old.id, old.text, old.title);
END;

CREATE TRIGGER search_passages_au AFTER UPDATE ON search_passages BEGIN
    INSERT INTO search_fts (search_fts, rowid, text, title)
        VALUES ('delete', old.id, old.text, old.title);
    INSERT INTO search_fts (rowid, text, title) VALUES (new.id, new.text, new.title);
END;

CREATE TABLE search_embeddings (
    content_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    dims INTEGER NOT NULL,
    vector BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (content_hash, model)
) STRICT;

-- Settings of the knowledge area, for example the embedding model that the
-- user selects. Task-20 can move them to the settings store.
CREATE TABLE knowledge_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;
