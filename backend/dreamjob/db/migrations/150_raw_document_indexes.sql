-- ===========================================================================
-- Index the rest of the raw-document orphan sweep (NFR-101, NFR-102)
--
-- The weekly maintenance job anti-joins vacancy and compensation_observation on
-- raw_document_id against the raw_document table (hygiene.py).  Migration 149
-- indexed provenance, but the other two children were left unindexed, so each
-- candidate document scanned the 55k-row vacancy table (or SQLite built an
-- automatic index for it on every run).
-- ===========================================================================

CREATE INDEX IF NOT EXISTS idx_vacancy_raw_document
    ON vacancy(raw_document_id)
    WHERE raw_document_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_comp_obs_raw_document
    ON compensation_observation(raw_document_id)
    WHERE raw_document_id IS NOT NULL;
