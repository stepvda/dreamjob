-- ===========================================================================
-- The ATS board registry (N3, docs/Data_Gathering_Plan.md section 5.2)
--
-- The registry is the route to 7,500 companies.  Three public URL indexes
-- name ~15,900 boards across nine ATS vendors in about 65 requests, which is
-- the only affordable way to turn "which employers exist" into fetchable plan
-- items: guessing slugs hits 16%, and probing company websites with
-- detect_ats costs ~30 requests per board found (section 2.8).
--
-- Two artefacts, with different jobs:
--
--   * backend/dreamjob/pipeline/data/board_registry.json - the seed, written
--     by scripts/import_board_registry.py and committed, so a fresh install
--     has targets before anybody runs the importer.  It is discovery data:
--     (vendor, slug, name, first_seen, last_verified, source).
--
--   * this table - the same rows plus the *liveness state* that only this
--     installation can know: whether the board answered, when, with what
--     status, and how many postings it carried.
--
-- Why liveness needs a table at all.  It decays, measurably: a slug seen in a
-- 2026 Common Crawl is 87.5% live, a slug seen only in the Wayback index is
-- 27.5% live (section 2.3).  So the registry has its own staleness class -
-- C8 adds "board_registry": 30 to the FR-343 policy - and re-verification is
-- one request per slug in the nightly refresh job, never per campaign: at
-- 0.5 req/s a per-campaign re-check of 14,600 boards would cost 8 hours and
-- buy nothing that yesterday's check did not already know.
--
-- Requirements: FR-343 (staleness per record type), FR-183 / NFR-402 (every
-- row says which index evidenced it), FR-182 (nothing here is fetched by the
-- importer; see the script header for why Common Crawl is read outside the
-- egress layer), FR-186 (planning caps count boards, so they must be
-- countable).
-- ===========================================================================

CREATE TABLE board_registry (
    id                   TEXT PRIMARY KEY,
    -- Matches ATSAdapter.vendor: greenhouse | lever | ashby | recruitee |
    -- personio | workable | teamtailor | workday.
    vendor               TEXT NOT NULL,
    -- Exactly what company.ats_slug holds, so a row is a plan item without
    -- translation.  Workday's slug is '<host>/<site>'.
    slug                 TEXT NOT NULL,
    -- The employer's name when a source gave one.  Greenhouse and Ashby
    -- payloads carry no organisation name; fetching it costs one request per
    -- board and belongs in the 90-day registry refresh, not a campaign.
    name                 TEXT,

    -- Provenance (FR-183).  One or more of commoncrawl | wayback | hackernews
    -- joined with '+'.  It is not decoration: it predicts liveness, which is
    -- what decides whether a board is worth a request.
    source               TEXT NOT NULL,
    first_seen           TEXT NOT NULL,

    -- Liveness state, owned by the nightly refresh job.  NULL last_verified
    -- means "never fetched", which is not the same as "dead" and must not be
    -- treated as one.
    liveness             TEXT NOT NULL DEFAULT 'unverified',  -- unverified|live|dead
    last_verified        TEXT,
    last_status          INTEGER,                 -- HTTP status of the last check
    job_count            INTEGER,                 -- postings seen at that check
    -- A board that 404s once may be a blip; three in a row is a dead board.
    -- Kept as a count so the job can retire a board on evidence rather than
    -- on a single bad response (section 3 step 8).
    consecutive_failures INTEGER NOT NULL DEFAULT 0,

    -- Filled once the board has been read and the employer identified
    -- (DR-101 keys on (ats_vendor, ats_slug)).
    company_id           TEXT REFERENCES company(id) ON DELETE SET NULL,
    updated_at           TEXT NOT NULL,

    CHECK (liveness IN ('unverified', 'live', 'dead'))
);

-- The identity of a board is its (vendor, slug): the same slug on two vendors
-- is two different companies, and the same company on two vendors is two
-- boards.  UNIQUE because a duplicate row is a duplicate fetch and a
-- duplicate company (DR-101).
CREATE UNIQUE INDEX idx_board_registry_key ON board_registry(vendor, slug);

-- The planner's question: which boards of this vendor are worth fetching?
CREATE INDEX idx_board_registry_live ON board_registry(vendor, liveness, last_verified);

-- The nightly job's question: what has not been verified for 30 days?
CREATE INDEX idx_board_registry_stale ON board_registry(last_verified, liveness);

CREATE INDEX idx_board_registry_company ON board_registry(company_id);
