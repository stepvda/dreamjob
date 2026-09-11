-- ===========================================================================
-- A semantic index over the corpus (FR-261, FR-282)
--
-- Matching was keyword-only: the ranked list reads FTS5 and the dream-fit
-- comparison reads one record at a time with an eight-thousand-token model
-- call.  Neither can answer "which postings are like this one" without the
-- model, and the model cannot be run over fifty thousand rows.
--
-- The vector is stored as a JSON array of floats, so the feature carries no
-- numerical dependency and works on the SQLite the product already ships.  It
-- is populated only when an embeddings model is configured; an installation
-- without one simply has an empty table and no new behaviour.
-- ===========================================================================

CREATE TABLE embedding (
    id            TEXT PRIMARY KEY,
    entity_type   TEXT NOT NULL,          -- vacancy|company|opportunity
    entity_id     TEXT NOT NULL,
    model         TEXT NOT NULL,
    dim           INTEGER NOT NULL,
    vector        TEXT NOT NULL,          -- JSON array of floats
    content_hash  TEXT,                   -- skip re-embedding unchanged text
    created_at    TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_embedding_entity ON embedding(entity_type, entity_id, model);
CREATE INDEX idx_embedding_type ON embedding(entity_type, model);
