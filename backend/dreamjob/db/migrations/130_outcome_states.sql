-- ===========================================================================
-- What a plan item ended in, and why (FR-182, FR-185, FR-186, FR-343, IR-101)
--
-- ``source_plan_item`` could say two things about a unit of work: how many
-- records it collected, and how many errors it had.  Everything that was not a
-- record was therefore an error, and one campaign reported 538 of them:
--
--     308  ATS boards answering 404 - dead slugs in a harvested registry whose
--          liveness was *measured* at 87.5% for fresh Common Crawl entries and
--          27.5% for Wayback-only ones (see 092_board_registry.sql).  A board
--          that has closed is a fact about the world, not a failure of ours.
--     192  robots.txt refusals - the product declining a source on purpose
--          (FR-182, CR-402).  "We declined 192 sources on principle" is
--          something the operator wants to know and can defend.
--      51  HTTP 403 bot walls - the same decision, spelled in HTTP.
--     104  plan items naming adapters that exist only as test fixtures.
--   4,664  items that never started because the FR-186 page budget ran out.
--
-- None of those is a failure, and burying a real failure among five hundred of
-- them is how the next real failure gets missed.  This migration gives the row
-- somewhere to put the distinction.
--
-- **Why not just stop counting them.**  The bug this replaces was the opposite
-- one: an item that fetched nothing, was blocked, or crashed was recorded as
-- ``done`` with 0 records and 0 errors, which hid a total retrieval failure for
-- hours while the dashboard said everything had succeeded.  So nothing here is
-- made quiet.  ``error_count`` is kept exactly where it is and keeps its
-- meaning - it is only narrowed to the things that actually went wrong - and
-- every other outcome gets a counter of its own instead of being dropped.
--
-- **The columns.**
--
--   outcome_state   the one word the item ended in:
--                     succeeded  records were written
--                     blocked    we declined, correctly (robots, 403, 451)
--                     gone       the target is not there any more (404/410)
--                     failed     5xx, transport, rate limit, crash - the one
--                                the operator must see
--                     skipped    there was nothing to do
--                     capped     the FR-186 budget ended the run first
--                   plus the finer answers the earlier audit fix won and must
--                   not lose: no_matches (the source said it holds nothing),
--                   extracted_nothing, normalised_nothing, rejected, no_work.
--                   ``succeeded-with-nothing`` therefore stays distinguishable
--                   from ``fetched nothing``, which is the whole reason those
--                   states exist.
--   outcome_reason  the evidence: which URL, which status, which rule
--                   (NFR-402 - a verdict without evidence is not one).
--   blocked_count   requests declined on principle, counted per request.
--   gone_count      requests to a target that no longer exists.
--   failed_count    requests that actually failed, plus one for a page that
--                   failed without issuing one.  It is the same events as
--                   ``error_count`` at request granularity: three 500s on one
--                   page are three failures and one failed page.
--
-- ``status`` gains two terminal values alongside done|failed|skipped:
-- ``blocked`` and ``gone``.  Both are terminal for the reason ``done`` is -
-- asking again this week cannot change the answer - so the collection worker,
-- which runs planned|running|failed, leaves them alone.
--
-- RK-08 / FR-344: a plan item already belongs to one campaign and one job
-- seeker; nothing here widens that.
-- ===========================================================================

ALTER TABLE source_plan_item ADD COLUMN outcome_state  TEXT;
ALTER TABLE source_plan_item ADD COLUMN outcome_reason TEXT;
ALTER TABLE source_plan_item ADD COLUMN blocked_count  INTEGER NOT NULL DEFAULT 0;
ALTER TABLE source_plan_item ADD COLUMN gone_count     INTEGER NOT NULL DEFAULT 0;
ALTER TABLE source_plan_item ADD COLUMN failed_count   INTEGER NOT NULL DEFAULT 0;

-- The dashboard's question: how did this campaign's plan end?  Answering it by
-- scanning 50,000 plan items per page load is what a partial index over the
-- campaign is for.
CREATE INDEX idx_plan_outcome ON source_plan_item(campaign_id, outcome_state);

-- ---------------------------------------------------------------------------
-- Backfill: relabel what the rows already prove (FR-185)
--
-- Every row settled by the previous code wrote its evidence into
-- ``last_error`` in a fixed vocabulary, so the rows that are demonstrably not
-- failures can be relabelled now instead of only after the next campaign - the
-- 538 are on the operator's screen today.
--
-- Three rules, and each is applied only where the evidence is unambiguous:
--
--   * "robots.txt" anywhere in the message - the product's own words for its
--     own decision (FR-182).  Nothing else writes that string.
--   * "HTTP 403" - a bot wall.
--   * "HTTP 404"/"HTTP 410" on an ``ats.*`` adapter - a board the registry
--     offered and that is not there any more.  The adapter prefix is required:
--     a 404 on a search endpoint means the endpoint moved, which is a breakage
--     and stays a failure.
--
-- A row is relabelled only when it did not also collect records and is not
-- also carrying another kind of error, and the count is *moved* out of
-- ``error_count`` rather than deleted: the four counters still add up to every
-- answer the item ever had.  Anything the rules do not match keeps
-- ``error_count`` exactly as it is - an unrecognised message is a failure
-- until something proves otherwise, which is the safe direction.
-- ---------------------------------------------------------------------------

UPDATE source_plan_item
   SET outcome_state  = 'blocked',
       outcome_reason = last_error,
       blocked_count  = MAX(error_count, 1),
       error_count    = 0,
       status         = 'blocked'
 WHERE error_count > 0
   AND records_collected = 0
   AND (last_error LIKE '%robots.txt%' OR last_error LIKE '%HTTP 403%');

UPDATE source_plan_item
   SET outcome_state  = 'gone',
       outcome_reason = last_error,
       gone_count     = MAX(error_count, 1),
       error_count    = 0,
       status         = 'gone'
 WHERE error_count > 0
   AND records_collected = 0
   AND adapter_key LIKE 'ats.%'
   AND (last_error LIKE '%HTTP 404%' OR last_error LIKE '%HTTP 410%');

-- The FR-186 budget: never an error to begin with, and now countable as what
-- it is rather than as 4,664 rows of prose in ``last_error``.
UPDATE source_plan_item
   SET outcome_state  = 'capped',
       outcome_reason = last_error
 WHERE outcome_state IS NULL
   AND status = 'planned'
   AND (last_error LIKE 'not started:%' OR last_error LIKE 'stopped by cap:%');

-- Everything else keeps the label its status already implies, so no row is
-- left unclassified and the dashboard never has to guess.
UPDATE source_plan_item
   SET outcome_state = CASE status
                           WHEN 'done'    THEN 'succeeded'
                           WHEN 'skipped' THEN 'skipped'
                           WHEN 'failed'  THEN 'failed'
                       END,
       failed_count  = CASE WHEN status = 'failed' THEN error_count ELSE failed_count END
 WHERE outcome_state IS NULL
   AND status IN ('done', 'skipped', 'failed');
