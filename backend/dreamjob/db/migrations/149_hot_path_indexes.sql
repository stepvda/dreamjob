-- ===========================================================================
-- Indexes the hot paths actually need (NFR-101, NFR-102)
--
-- Four queries ran without a usable index:
--   * the raw-document orphan sweep anti-joins on provenance.raw_document_id
--     across ~225k rows, once per raw_document candidate - a scan inside a scan;
--   * erasure and the admin listings delete/select llm_call and audit_event by
--     job_seeker_id, which neither table indexed;
--   * the keyset walk pages opportunities by id within one seeker, and the only
--     ordered index on id is the primary key, which is not seekable by seeker.
-- ===========================================================================

CREATE INDEX IF NOT EXISTS idx_prov_raw_document
    ON provenance(raw_document_id);

CREATE INDEX IF NOT EXISTS idx_llm_call_seeker
    ON llm_call(job_seeker_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_seeker
    ON audit_event(job_seeker_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_opp_seeker_id
    ON opportunity(job_seeker_id, id);
