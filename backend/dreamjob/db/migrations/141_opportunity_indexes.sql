-- ===========================================================================
-- Opportunity read indexes (NFR-101)
--
-- The ranked list is the screen a job seeker actually works in, and it was
-- sorting the whole table in a temporary B-tree on every page: 52,913 rows,
-- ~12 seconds for the first 500.  Two indexes remove the sort.
--
-- ``idx_opp_seeker_created`` serves the unordered walk (synthesis, scoring,
-- the backfill), which reads in ``created_at`` order and used to be the only
-- query with no index at all.
--
-- ``idx_opp_seeker_rank`` serves the default ranked order - manual positions
-- first, then pins, then score.  SQLite can walk it for
-- ``ORDER BY manual_rank, pinned DESC, score DESC`` and stop after the page.
-- ===========================================================================

CREATE INDEX IF NOT EXISTS idx_opp_seeker_created
    ON opportunity(job_seeker_id, created_at);

CREATE INDEX IF NOT EXISTS idx_opp_seeker_rank
    ON opportunity(job_seeker_id, manual_rank, pinned DESC, score DESC);

-- The vacancy link is read once per row for the source URL, and the company
-- join once per row for the badge.  Both are already indexed on their own
-- primary keys; this one covers the per-company walk the facets and the
-- knowledge-base reuse assessment do.
CREATE INDEX IF NOT EXISTS idx_opp_seeker_company
    ON opportunity(job_seeker_id, company_id);
