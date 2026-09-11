-- ===========================================================================
-- The ranked list's own sort (NFR-101)
--
-- ``ORDER BY (manual_rank IS NULL), manual_rank, pinned DESC, score DESC``
-- could not use ``idx_opp_seeker_rank`` because its leading term is an
-- *expression*, not a column, so every page still built a temporary B-tree over
-- the whole table.  SQLite supports expression indexes, and an index on the
-- exact expression the query orders by lets it walk the index and stop after
-- the page.
--
-- The earlier index is dropped rather than kept: it can never be chosen while
-- the expression leads the ORDER BY, so it only costs writes.
-- ===========================================================================

DROP INDEX IF EXISTS idx_opp_seeker_rank;

CREATE INDEX IF NOT EXISTS idx_opp_seeker_rank
    ON opportunity(
        job_seeker_id,
        (manual_rank IS NULL),
        manual_rank,
        pinned DESC,
        score DESC
    );
