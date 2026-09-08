-- ===========================================================================
-- Continuous monitoring: watchlist rechecks, notifications, digests
-- (FR-401, FR-402, FR-403)
--
-- Three additions to 001_initial.sql, each because the requirement cannot be
-- met with the columns already there:
--
--  1. FR-401 names four channels to recheck - careers pages, ATS endpoints,
--     news and hiring signals, new filings - and says matching vacancies are
--     added to the ranked list automatically.  ``watchlist_entry`` could say
--     only *when* it was last looked at.  It now says which channels are in
--     force, which campaign's ranked list a new vacancy joins, what the last
--     pass found and why a pass failed, so a silent watch can be told apart
--     from a quiet company.
--
--  2. A weekly recheck that re-announces the same vacancy every week is worse
--     than no recheck.  ``notification.dedup_key`` makes "new vacancy X for
--     seeker Y" a fact that can only be announced once.
--
--  3. FR-403 is one digest per seeker per week.  Re-running the generator for
--     a period that already has a digest must refresh that digest rather than
--     stack a second one, so the period is a key.
-- ===========================================================================

-- --- FR-401: what a watch checks, and where its findings land --------------
-- JSON array of channels; NULL means the default set (careers, ats, news,
-- signals, filings).  Keeping it per entry lets a seeker watch a company for
-- news alone without pulling its board every week.
ALTER TABLE watchlist_entry ADD COLUMN check_sources TEXT;
-- The ranked list a newly found vacancy is synthesised into (FR-401).  NULL
-- means notify only: nothing is added to a ranked list without a target.
ALTER TABLE watchlist_entry ADD COLUMN campaign_id TEXT REFERENCES campaign(id) ON DELETE SET NULL;
ALTER TABLE watchlist_entry ADD COLUMN reason TEXT;            -- why it is watched
ALTER TABLE watchlist_entry ADD COLUMN next_check_at TEXT;     -- due date, so the scheduler can index
ALTER TABLE watchlist_entry ADD COLUMN last_result TEXT;       -- JSON: counts per channel
ALTER TABLE watchlist_entry ADD COLUMN last_error TEXT;
ALTER TABLE watchlist_entry ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0;

CREATE INDEX idx_watchlist_due ON watchlist_entry(active, next_check_at);

-- --- FR-401/FR-403: announce each finding once -----------------------------
ALTER TABLE notification ADD COLUMN dedup_key TEXT;
ALTER TABLE notification ADD COLUMN severity TEXT NOT NULL DEFAULT 'info';  -- info|action|urgent
ALTER TABLE notification ADD COLUMN dismissed_at TEXT;
CREATE UNIQUE INDEX idx_notif_dedup
    ON notification(job_seeker_id, kind, dedup_key) WHERE dedup_key IS NOT NULL;

-- --- FR-403: one digest per seeker per period ------------------------------
ALTER TABLE digest ADD COLUMN recommended_action_detail TEXT;  -- JSON: why this action
ALTER TABLE digest ADD COLUMN email_error TEXT;
CREATE UNIQUE INDEX idx_digest_period ON digest(job_seeker_id, period_start, period_end);
