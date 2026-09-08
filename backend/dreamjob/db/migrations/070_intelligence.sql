-- ===========================================================================
-- Dream-job intelligence, event radar and campaign export
-- (FR-381, FR-382, FR-443, FR-462, FR-463, NFR-301)
--
-- Five additions to 001_initial.sql.  Each exists because the requirement
-- cannot be met with the columns already there:
--
--  1. FR-381 links every gap to the opportunities where it is decisive, and
--     those opportunities belong to one campaign.  ``gap_analysis`` could not
--     say which campaign it was computed against, so a second campaign would
--     silently overwrite the first one's reading of the market.
--
--  2. FR-382 proposes stepping-stone paths only when no opportunity clears a
--     configurable threshold.  ``stepping_stone_path`` now records the basis -
--     the threshold in force, the best fit actually found, the gaps the path
--     closes - so a path can be re-read months later and judged.
--
--  3. FR-462 asks for conferences, meetups and webinars matched to a location
--     and a travel tolerance, with target-company attendees where public.
--     ``event`` held a name, a time and a place; it now also holds what kind
--     of event it is, whether attending means travelling at all, the topics it
--     covers and the attendees a public page named.
--
--  4. FR-462 also asks for events to be addable to the calendar.  Which events
--     one job seeker wants to attend is private data (FR-101, FR-344), so it
--     lives in its own private table rather than on the shared ``event`` row.
--
--  5. FR-443's LinkedIn suggestions are "editable text for the job seeker to
--     apply manually": the generated draft and the seeker's edit of it both
--     have to survive a page reload, and FR-463's export package has to be
--     downloadable after the request that built it has ended.
--
-- Every private table here carries ``job_seeker_id`` with ON DELETE CASCADE,
-- so an erasure (FR-108, NFR-301) takes these rows with the account.
-- ===========================================================================

-- --- FR-381: the gap analysis is per campaign ------------------------------
ALTER TABLE gap_analysis ADD COLUMN campaign_id TEXT REFERENCES campaign(id) ON DELETE CASCADE;
ALTER TABLE gap_analysis ADD COLUMN summary TEXT;
-- deterministic | llm+deterministic: NFR-104 degradation is visible in the row.
ALTER TABLE gap_analysis ADD COLUMN generated_by TEXT;
ALTER TABLE gap_analysis ADD COLUMN updated_at TEXT;
CREATE INDEX idx_gap_seeker ON gap_analysis(job_seeker_id, campaign_id);

-- --- FR-382: why this path was proposed ------------------------------------
-- JSON: {threshold, best_dream_fit, opportunities_considered, gaps_addressed}
ALTER TABLE stepping_stone_path ADD COLUMN basis TEXT;
ALTER TABLE stepping_stone_path ADD COLUMN generated_by TEXT;
ALTER TABLE stepping_stone_path ADD COLUMN horizon_months INTEGER;
CREATE INDEX idx_stone_seeker ON stepping_stone_path(job_seeker_id, campaign_id);

-- --- FR-462: what the radar needs an event to say --------------------------
ALTER TABLE event ADD COLUMN kind TEXT;              -- conference|meetup|webinar|other
ALTER TABLE event ADD COLUMN format TEXT;            -- in_person|online|hybrid
ALTER TABLE event ADD COLUMN description TEXT;
ALTER TABLE event ADD COLUMN topics TEXT;            -- JSON array of subject labels
-- JSON: [{name, role, company_name, company_id, kind: speaker|attendee|organiser,
--         source}] - only people a public page names (NFR-302: professional
-- context only).
ALTER TABLE event ADD COLUMN attendees TEXT;
ALTER TABLE event ADD COLUMN language TEXT;
ALTER TABLE event ADD COLUMN registration_url TEXT;
ALTER TABLE event ADD COLUMN access_method TEXT NOT NULL DEFAULT 'http';   -- FR-207
ALTER TABLE event ADD COLUMN confidence REAL NOT NULL DEFAULT 0.5;
ALTER TABLE event ADD COLUMN refreshed_at TEXT;
CREATE INDEX idx_event_kind ON event(kind, starts_at);

-- --- FR-462: the job seeker's own interest in an event (PRIVATE) -----------
CREATE TABLE event_interest (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    event_id            TEXT NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    campaign_id         TEXT REFERENCES campaign(id) ON DELETE SET NULL,
    status              TEXT NOT NULL DEFAULT 'interested',  -- interested|going|dismissed
    note                TEXT,
    -- NFR-305: the calendar entry is only made when the seeker asks for it.
    ics_path            TEXT,
    calendar_account_id TEXT,
    calendar_event_id   TEXT,
    calendar_event_url  TEXT,
    last_error          TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (job_seeker_id, event_id)
);
CREATE INDEX idx_event_interest_seeker ON event_interest(job_seeker_id, status);

-- --- FR-443: LinkedIn suggestions, as editable text (PRIVATE) --------------
CREATE TABLE linkedin_advice (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    persona_id          TEXT REFERENCES persona(id) ON DELETE SET NULL,
    campaign_id         TEXT REFERENCES campaign(id) ON DELETE CASCADE,
    dream_job_model_id  TEXT REFERENCES dream_job_model(id) ON DELETE SET NULL,
    language            TEXT NOT NULL DEFAULT 'en',
    headlines           TEXT,                    -- JSON array of alternatives
    about               TEXT,                    -- draft About section
    skills              TEXT,                    -- JSON: skills to list, ranked by demand
    keywords            TEXT,                    -- JSON: [{term, postings, share}]
    featured            TEXT,                    -- JSON: content to feature
    notes               TEXT,                    -- JSON: caveats, what was left out
    corpus_summary      TEXT,                    -- JSON: what the advice was grounded in
    -- FR-385: while discretion mode is on, applying these edits would signal a
    -- job search, so the advice is stored but flagged as not-to-apply-now.
    discretion_mode     INTEGER NOT NULL DEFAULT 0,
    generated_by        TEXT,                    -- llm | deterministic
    -- FR-443: the system never writes to LinkedIn; this is the seeker's own
    -- edit of the draft, kept so the screen can be reopened.
    edited_text         TEXT,
    edited_at           TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX idx_linkedin_advice_seeker ON linkedin_advice(job_seeker_id, created_at);

-- --- FR-463: the export package (PRIVATE) ----------------------------------
CREATE TABLE campaign_export (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    campaign_id         TEXT NOT NULL REFERENCES campaign(id) ON DELETE CASCADE,
    language            TEXT NOT NULL DEFAULT 'en',
    zip_path            TEXT,
    pdf_path            TEXT,
    json_path           TEXT,
    byte_size           INTEGER,
    -- JSON: counts per section, the do-not-disclose paths applied (FR-106),
    -- the isolation assertion FR-463 is accepted on, and the file list.
    manifest            TEXT,
    status              TEXT NOT NULL DEFAULT 'ready',   -- ready|failed
    last_error          TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_campaign_export_seeker ON campaign_export(job_seeker_id, campaign_id);
