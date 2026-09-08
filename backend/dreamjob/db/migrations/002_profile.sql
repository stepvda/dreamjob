-- ---------------------------------------------------------------------------
-- PRIVATE: retained profile source documents (DR-102, FR-108)
-- ---------------------------------------------------------------------------
-- DR-102 keeps the uploaded LinkedIn export and CV so extraction can be re-run
-- when the parsers improve.  The files live in the job seeker's private upload
-- area and deliberately not in the shared raw_document table, which must carry
-- no link back to a person (FR-344).
--
-- The row exists so the file is *discoverable*: erasure walks the private
-- tables and deletes what their "_path" columns name (FR-108), which a path
-- buried in a JSON blob would survive.
CREATE TABLE profile_source (
    id              TEXT PRIMARY KEY,
    job_seeker_id   TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,               -- linkedin_pdf | cv
    filename        TEXT NOT NULL,               -- as uploaded
    file_path       TEXT NOT NULL,               -- retained original on disk
    sha256          TEXT NOT NULL,
    byte_size       INTEGER NOT NULL,
    retained_at     TEXT NOT NULL,
    UNIQUE (job_seeker_id, kind)
);
CREATE INDEX idx_profile_source_seeker ON profile_source(job_seeker_id);
