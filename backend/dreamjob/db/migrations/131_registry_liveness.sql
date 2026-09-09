-- ===========================================================================
-- 131: the board registry learns which of its slugs are gone.
--
-- Migration 092 gave the registry a liveness column and a retirement rule, and
-- nothing ever wrote to it.  The consequence is measurable in this
-- installation: 208 registry boards answered HTTP 404 to a real request, all
-- 208 are still offered to every campaign, and each of the 175 campaigns
-- planned each of them again.  A dead board is not a failure - it is a fact
-- about the world - but it is a fact the registry has to be able to hold, or
-- the same 208 requests are spent every night for ever.
--
-- Two things change here.
--
-- 1. THE VOCABULARY.  ``liveness`` becomes ``state`` and ``dead`` becomes
--    ``gone``, so the registry says the same word about a board that the plan
--    item says about the attempt to read it.  A collection run that ends
--    ``gone`` and a registry row that reads ``dead`` are the same fact under
--    two names, and an operator reconciling the dashboard against the
--    registry should not have to know that.  The four outcomes a plan item can
--    end in are succeeded | blocked | gone | failed; ``gone`` is this one.
--
--    SQLite cannot drop a CHECK constraint, so the table is rebuilt.  It is
--    4,518 rows and nothing references it, so the rebuild is a copy, a drop
--    and a rename inside the migration's own transaction.
--
-- 2. THE BACKFILL.  Every 404 this installation has already paid for is
--    written back onto the row that caused it, so the evidence is not thrown
--    away a second time.  On a fresh install there are no plan items and this
--    is a no-op.
--
-- Retirement stays deliberately conservative: TWO observed 404/410 responses
-- retire a slug, not one.  One 404 is as likely to be a tenant renaming its
-- board mid-migration as a tenant that has left, and retiring on it deletes a
-- live employer from the inventory that nothing will ever look for again.  The
-- same threshold lives in
-- ``dreamjob.pipeline.board_registry.RETIRE_AFTER_FAILURES``; it is written
-- twice because a migration cannot import Python, and the unit tests assert
-- the two agree.
--
-- Requirements: FR-181 (the registry is the route to employers), FR-183 /
-- NFR-402 (every row still says which index evidenced it), FR-343 (staleness
-- per record type - a retired board is re-tested after 90 days, not never),
-- FR-186 (planning caps count boards, so a board nobody should fetch must not
-- be counted), CR-408 (forward-only migrations).
-- ===========================================================================

CREATE TABLE board_registry_v131 (
    id                   TEXT PRIMARY KEY,
    vendor               TEXT NOT NULL,
    slug                 TEXT NOT NULL,
    name                 TEXT,

    source               TEXT NOT NULL,
    first_seen           TEXT NOT NULL,

    -- unverified: nobody has asked.  Not the same as gone, and must never be
    --             treated as one - 4,515 of this installation's rows are here.
    -- live:       a request was answered.
    -- gone:       the board answered 404/410 often enough to be believed, and
    --             planning stops offering it until the revisit window opens.
    state                TEXT NOT NULL DEFAULT 'unverified',
    last_verified        TEXT,
    last_status          INTEGER,
    job_count            INTEGER,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,

    company_id           TEXT REFERENCES company(id) ON DELETE SET NULL,
    updated_at           TEXT NOT NULL,

    CHECK (state IN ('unverified', 'live', 'gone'))
);

INSERT INTO board_registry_v131
    (id, vendor, slug, name, source, first_seen, state, last_verified,
     last_status, job_count, consecutive_failures, company_id, updated_at)
SELECT id, vendor, slug, name, source, first_seen,
       CASE liveness WHEN 'dead' THEN 'gone' ELSE liveness END,
       last_verified, last_status, job_count, consecutive_failures,
       company_id, updated_at
  FROM board_registry;

DROP TABLE board_registry;
ALTER TABLE board_registry_v131 RENAME TO board_registry;

-- The identity of a board is its (vendor, slug): a duplicate row is a
-- duplicate fetch and a duplicate company (DR-101).
CREATE UNIQUE INDEX idx_board_registry_key ON board_registry(vendor, slug);
-- The planner's question: which boards of this vendor are worth fetching?
-- Answering it from the index is what keeps "skip the gone ones" free.
CREATE INDEX idx_board_registry_live ON board_registry(vendor, state, last_verified);
-- The refresh job's question: what has not been checked since <cutoff>?  Also
-- the revisit query: which retired boards are older than 90 days?
CREATE INDEX idx_board_registry_stale ON board_registry(state, last_verified);
CREATE INDEX idx_board_registry_company ON board_registry(company_id);

-- ---------------------------------------------------------------------------
-- Backfill: the 404s this installation has already observed.
--
-- ``source_plan_item.last_error`` is where a collection run recorded what the
-- board answered, and ``native_query`` names the slug it was reading.  Both
-- shapes of plan item are read: the planner writes a single ``slug``, and the
-- campaign planner writes ``board_slugs`` as a list (adapters/ats/common.py
-- says every one of them must be named, and so must every one of them be
-- credited with its own 404).
--
-- The predicate is deliberately the same one migration 130 uses to label a plan
-- item ``outcome_state = 'gone'``, minus its ``error_count > 0`` clause, which
-- 130 has already consumed by the time this runs.  It is repeated rather than
-- read from ``outcome_state`` so that this migration stands on its own: a
-- backfill that hard-depends on a sibling migration's column fails at boot if
-- that sibling is ever reshaped.  The two must agree - a board the dashboard
-- calls gone and the registry keeps offering is the bug this pair exists to
-- close.
--
-- ``records_collected = 0`` matters: a paginated board that returned vacancies
-- and then 404'd on a later page is a live board with a broken page, and
-- retiring it would delete a working employer.
--
-- One row per *observation*, counted per board.  ``last_verified`` is the
-- campaign's own clock rather than the migration's, because the question the
-- 90-day revisit asks is "how long ago did this board stop answering", and the
-- answer is not "when the migration ran".
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE _registry_404_evidence AS
WITH observed(vendor, slug, seen_at) AS (
    SELECT lower(substr(i.adapter_key, 5)),
           trim(json_extract(i.native_query, '$.slug'), ' /'),
           COALESCE(c.finished_at, c.started_at, i.created_at)
      FROM source_plan_item i
      JOIN campaign c ON c.id = i.campaign_id
     WHERE i.adapter_key LIKE 'ats.%'
       AND i.native_query IS NOT NULL
       AND i.records_collected = 0
       AND (i.last_error LIKE '%HTTP 404%' OR i.last_error LIKE '%HTTP 410%')
       AND json_extract(i.native_query, '$.slug') IS NOT NULL
    UNION ALL
    SELECT lower(substr(i.adapter_key, 5)),
           trim(j.value, ' /'),
           COALESCE(c.finished_at, c.started_at, i.created_at)
      FROM source_plan_item i
      JOIN campaign c ON c.id = i.campaign_id
      JOIN json_each(i.native_query, '$.board_slugs') j
     WHERE i.adapter_key LIKE 'ats.%'
       AND i.native_query IS NOT NULL
       AND i.records_collected = 0
       AND (i.last_error LIKE '%HTTP 404%' OR i.last_error LIKE '%HTTP 410%')
)
SELECT vendor,
       slug,
       COUNT(*)     AS failures,
       MAX(seen_at) AS seen_at
  FROM observed
 WHERE slug IS NOT NULL AND slug <> ''
 GROUP BY vendor, slug;

-- A board that has since answered is left alone: a live board that 404'd in an
-- older campaign has been contradicted by evidence newer than this, and the
-- registry believes the newer evidence.
UPDATE board_registry
   SET consecutive_failures = (
           SELECT e.failures FROM _registry_404_evidence e
            WHERE e.vendor = board_registry.vendor AND e.slug = board_registry.slug),
       last_status = 404,
       last_verified = COALESCE((
           SELECT e.seen_at FROM _registry_404_evidence e
            WHERE e.vendor = board_registry.vendor AND e.slug = board_registry.slug),
           last_verified),
       -- Two observations retire the slug; one records the strike and leaves
       -- the board plannable, so the next campaign's 404 is what retires it.
       state = CASE
           WHEN (SELECT e.failures FROM _registry_404_evidence e
                  WHERE e.vendor = board_registry.vendor
                    AND e.slug = board_registry.slug) >= 2 THEN 'gone'
           ELSE state
       END,
       updated_at = COALESCE((SELECT MAX(e.seen_at) FROM _registry_404_evidence e
                               WHERE e.vendor = board_registry.vendor
                                 AND e.slug = board_registry.slug), updated_at)
 WHERE state <> 'live'
   AND EXISTS (SELECT 1 FROM _registry_404_evidence e
                WHERE e.vendor = board_registry.vendor
                  AND e.slug = board_registry.slug);

DROP TABLE _registry_404_evidence;
