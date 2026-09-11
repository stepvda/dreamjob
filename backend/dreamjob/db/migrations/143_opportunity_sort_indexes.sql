-- ===========================================================================
-- The remaining list sorts (NFR-101)
--
-- ``SORT_EXPRESSIONS`` offers score, sub-scores, compensation, plausibility,
-- posted date, title and company.  Each was a temporary B-tree over the whole
-- table.  The numeric ones are covered here; title and company sort on text
-- with a collation and are left to the sort, because they are chosen rarely and
-- an index on a collated text column would cost more in writes than it saves.
-- ===========================================================================

CREATE INDEX IF NOT EXISTS idx_opp_seeker_score
    ON opportunity(job_seeker_id, score DESC);
CREATE INDEX IF NOT EXISTS idx_opp_seeker_dream
    ON opportunity(job_seeker_id, score_dream_fit DESC);
CREATE INDEX IF NOT EXISTS idx_opp_seeker_profile
    ON opportunity(job_seeker_id, score_profile_fit DESC);
CREATE INDEX IF NOT EXISTS idx_opp_seeker_posted
    ON opportunity(job_seeker_id, posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_opp_seeker_comp
    ON opportunity(job_seeker_id, comp_max DESC);
CREATE INDEX IF NOT EXISTS idx_opp_seeker_plausibility
    ON opportunity(job_seeker_id, plausibility DESC);
