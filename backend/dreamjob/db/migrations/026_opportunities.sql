-- ===========================================================================
-- Opportunity synthesis, compensation and ranking (FR-261..265, FR-281..285)
--
-- Two additions to 001_initial.sql, both needed to satisfy the requirements
-- literally rather than by inference:
--
--  1. FR-261 lists the fields an opportunity record must carry: required and
--     desirable skills, location, work arrangement, contract type, posting
--     date, application channel and source.  ``opportunity`` had none of them;
--     they could be read back through the ``vacancy`` join, but a speculative
--     opening (FR-262) has no vacancy row, and the shared ``vacancy`` row is
--     re-collected and merged over time (FR-342).  The opportunity is the job
--     seeker's private, stable snapshot, so the normalised values live here.
--
--  2. FR-264 asks for a compensation estimate built from national salary
--     surveys and Glassdoor/Levels-style data.  Those observations are market
--     facts, not vacancies, and belong in the SHARED knowledge base with the
--     usual freshness metadata (FR-343) and no link back to a job seeker
--     (FR-344).
-- ===========================================================================

-- --- FR-261: the normalised opportunity record -----------------------------
ALTER TABLE opportunity ADD COLUMN required_skills    TEXT;   -- JSON array
ALTER TABLE opportunity ADD COLUMN desirable_skills   TEXT;   -- JSON array
ALTER TABLE opportunity ADD COLUMN location           TEXT;
ALTER TABLE opportunity ADD COLUMN country            TEXT;
ALTER TABLE opportunity ADD COLUMN latitude           REAL;
ALTER TABLE opportunity ADD COLUMN longitude          REAL;
ALTER TABLE opportunity ADD COLUMN work_arrangement   TEXT;   -- onsite|hybrid|remote
ALTER TABLE opportunity ADD COLUMN remote_days        INTEGER;
ALTER TABLE opportunity ADD COLUMN contract_type      TEXT;   -- permanent|fixed_term|freelance|interim
ALTER TABLE opportunity ADD COLUMN fte_percentage     INTEGER;
ALTER TABLE opportunity ADD COLUMN posted_at          TEXT;
ALTER TABLE opportunity ADD COLUMN application_channel TEXT;  -- email|ats_form|url|speculative
ALTER TABLE opportunity ADD COLUMN application_target TEXT;
ALTER TABLE opportunity ADD COLUMN source_url         TEXT;
ALTER TABLE opportunity ADD COLUMN source_adapter     TEXT;
-- FR-264: whether comp_min/comp_max were stated by the employer or estimated.
ALTER TABLE opportunity ADD COLUMN comp_is_stated     INTEGER NOT NULL DEFAULT 0;
-- FR-281: when the last recalculation ran, so the list can show staleness.
ALTER TABLE opportunity ADD COLUMN scored_at          TEXT;

-- Synthesis is re-runnable: one opportunity per vacancy per campaign, and one
-- speculative opening per role title per company per campaign (FR-262).
CREATE UNIQUE INDEX idx_opp_campaign_vacancy
    ON opportunity(campaign_id, vacancy_id) WHERE vacancy_id IS NOT NULL;
CREATE UNIQUE INDEX idx_opp_campaign_speculative
    ON opportunity(campaign_id, company_id, lower(title)) WHERE kind = 'speculative';

-- FR-284: manual order is the first sort key of the ranked list.
CREATE INDEX idx_opp_manual_rank ON opportunity(job_seeker_id, campaign_id, manual_rank);

-- --- FR-264: SHARED market compensation observations -----------------------
CREATE TABLE compensation_observation (
    id                  TEXT PRIMARY KEY,
    -- glassdoor|levels|salary_survey|posted_vacancies|manual
    source              TEXT NOT NULL,
    source_name         TEXT,                    -- e.g. "Hudson salary compass 2025"
    source_url          TEXT,
    company_id          TEXT REFERENCES company(id) ON DELETE CASCADE,
    role_title          TEXT NOT NULL,
    normalised_title    TEXT,
    function_family     TEXT,
    seniority           TEXT,
    country             TEXT,
    region              TEXT,
    currency            TEXT NOT NULL DEFAULT 'EUR',
    period              TEXT NOT NULL DEFAULT 'annual',  -- annual|monthly|daily
    amount_min          REAL,
    amount_p25          REAL,
    amount_median       REAL,
    amount_p75          REAL,
    amount_max          REAL,
    includes_variable   INTEGER NOT NULL DEFAULT 0,
    sample_size         INTEGER,
    as_of               TEXT,                    -- the period the figures describe
    raw_document_id     TEXT REFERENCES raw_document(id) ON DELETE SET NULL,
    collected_at        TEXT NOT NULL,
    access_method       TEXT NOT NULL DEFAULT 'http',
    confidence          REAL NOT NULL DEFAULT 0.5
);
CREATE INDEX idx_comp_obs_lookup
    ON compensation_observation(function_family, seniority, country);
CREATE INDEX idx_comp_obs_company ON compensation_observation(company_id);

-- --- FR-265: SHARED employer-review signals, advisory input only -----------
CREATE TABLE employer_review_summary (
    id                  TEXT PRIMARY KEY,
    company_id          TEXT NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    source              TEXT NOT NULL,           -- glassdoor|indeed|kununu|...
    source_url          TEXT,
    rating              REAL,                    -- normalised to 0..5
    rating_scale_max    REAL NOT NULL DEFAULT 5,
    review_count        INTEGER,
    themes              TEXT,                    -- JSON: [{theme, polarity, mentions}]
    collected_at        TEXT NOT NULL,
    access_method       TEXT NOT NULL DEFAULT 'http',
    confidence          REAL NOT NULL DEFAULT 0.4,
    UNIQUE (company_id, source)
);

-- FR-282: the per-component reasons behind each sub-score, so the ranked list
-- can explain a number without recomputing it.
ALTER TABLE opportunity ADD COLUMN score_detail TEXT;   -- JSON
