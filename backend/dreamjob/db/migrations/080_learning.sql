-- ===========================================================================
-- Response capture and redirection advice
--
-- Extends the FR-425 outcome learning in two directions the product owner
-- asked for after the specification was baselined:
--
--   1. Replies arrive through channels the system cannot poll - a phone call,
--      a LinkedIn message, a forwarded note, or any mailbox reached through
--      Resend (which has no inbox). They are entered by hand and then treated
--      exactly like a detected reply.
--
--   2. Outcomes are analysed by *what was applied for* - function family,
--      seniority, company size, stage, sector, work arrangement - not only by
--      how the application was written. Where a segment underperforms, the
--      system proposes a concrete change of direction rather than a statistic.
-- ===========================================================================

-- A proposal to change search direction, derived from outcome segments.
-- Stored so it can be shown, dismissed, or applied to a new directive set
-- version; never applied silently (NFR-305).
CREATE TABLE redirection_advice (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    outcome             TEXT NOT NULL,          -- reply | interview | offer
    dimension           TEXT NOT NULL,          -- function_family | size_band | company_stage | ...
    from_value          TEXT,                   -- the segment doing badly
    to_value            TEXT,                   -- the segment doing better, if any
    headline            TEXT NOT NULL,
    rationale           TEXT,
    -- The numbers the advice rests on, so the user can judge it rather than
    -- trust it. Always includes n for both sides.
    evidence            TEXT,                   -- JSON: rates, intervals, sample sizes
    confidence          TEXT NOT NULL DEFAULT 'indicative',  -- indicative | suggestive
    expected_effect     REAL,                   -- percentage-point difference observed
    directive_patch     TEXT,                   -- JSON: the change to apply
    status              TEXT NOT NULL DEFAULT 'open',  -- open | applied | dismissed
    dismissed_reason    TEXT,
    applied_directive_set_id TEXT REFERENCES directive_set(id) ON DELETE SET NULL,
    computed_at         TEXT NOT NULL,
    resolved_at         TEXT
);
CREATE INDEX idx_advice_seeker ON redirection_advice(job_seeker_id, status);

-- A stored snapshot of one segment analysis run, so the advice keeps the
-- figures it was computed from even after more applications land.
CREATE TABLE outcome_segment_run (
    id              TEXT PRIMARY KEY,
    job_seeker_id   TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    outcome         TEXT NOT NULL,
    sample_size     INTEGER NOT NULL DEFAULT 0,
    resolved_size   INTEGER NOT NULL DEFAULT 0,
    baseline_rate   REAL,
    segments        TEXT NOT NULL,              -- JSON: per dimension, per value
    caveats         TEXT,                       -- JSON: what the numbers cannot support
    computed_at     TEXT NOT NULL
);
CREATE INDEX idx_segrun_seeker ON outcome_segment_run(job_seeker_id, outcome, computed_at);

-- Responses entered by hand. These become incoming_reply rows so that
-- classification (FR-422) and the pipeline board (FR-421) treat them
-- identically to detected replies; this table keeps what only applies to a
-- manual entry.
CREATE TABLE manual_response (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    incoming_reply_id   TEXT REFERENCES incoming_reply(id) ON DELETE CASCADE,
    dispatch_id         TEXT REFERENCES dispatch(id) ON DELETE SET NULL,
    opportunity_id      TEXT REFERENCES opportunity(id) ON DELETE SET NULL,
    channel             TEXT NOT NULL DEFAULT 'email',  -- email|phone|linkedin|portal|in_person|other
    received_at         TEXT,
    entered_by          TEXT,
    raw_text            TEXT,
    -- What the person entering it said, before any AI reading of it. Kept
    -- separate from the classification so a wrong classification can be
    -- corrected without losing what actually happened.
    stated_outcome      TEXT,                   -- interest|info_request|interview|rejection|referral|auto_reply|other
    notes               TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_manual_resp_seeker ON manual_response(job_seeker_id, created_at);
