-- ===========================================================================
-- A source that is systematically unusable, remembered across campaigns
-- (FR-182, FR-185, IR-101)
--
-- A campaign collected nothing because every vacancy source answered 403:
-- Indeed refused all 5 requests, Jobat all 10.  Collection recorded each plan
-- item ``blocked`` - correctly, and the run itself was fine - but the fact
-- that the *adapter* is unusable did not survive the campaign.  The next plan
-- selected the same five sources, every run re-issued the same refused
-- requests, logged the same errors and grew the same ``error_count``.  The
-- operator had no list of sources the product had declined, and no way to say
-- "try this one again".
--
-- This table is that memory.
--
-- One row per adapter.  Observations accumulate across campaigns - requests
-- issued, refusals seen, how many distinct campaigns saw them - and
-- ``declined_at`` is stamped the moment the pattern is judged persistent (see
-- ``pipeline/declines.py`` for the policy, because the threshold is a
-- product decision and belongs in code a person can read, not in SQL).
--
-- The lifecycle:
--
--   * below the threshold, the row is an observation only.  Nothing excludes
--     the adapter and collection charges it as before; the counts are what a
--     later threshold decision rests on.
--   * ``declined_at`` set and ``acknowledged_at`` NULL - the adapter is
--     *declined*.  Planning rejects it with the reasons recorded here and
--     collection skips its plan items without charging a page or an error.
--   * an administrator acknowledges it (``acknowledged_at``): the counters
--     reset and ``declined_at`` is cleared, so a source that is refused again
--     has to re-earn the decline rather than inherit it.  The acknowledgement
--     is a named, timestamped act in ``audit_event`` (NFR-702).
--
-- IR-101 is a property of the reason.  A decline whose reason is ``terms`` or
-- ``unacknowledged`` may only be cleared by an administrator explicitly
-- acknowledging the terms; nothing in the product clears it automatically.
-- A ``robots``, ``http_403``, ``http_451`` or ``http_401`` decline is cleared
-- the same way, because asking again cannot change a refusal either.
--
-- **Why an adapter and not a URL.**  A 404 is a fact about one target and the
-- board registry already remembers it (092_board_registry.sql).  A 403 bot
-- wall, a robots.txt rule and a credential that is refused everywhere are
-- facts about the *source*, and re-deriving them per URL is what charged the
-- same five pages to the same five sources every campaign.
--
-- **Evidence, not prose.**  ``reason`` is a stable code
-- (``robots`` | ``http_403`` | ``http_451`` | ``http_401`` | ``terms`` |
-- ``unacknowledged``) so a screen can group by it, and ``detail`` carries the
-- sentence with the URL the decision rests on (NFR-402).
-- ===========================================================================

CREATE TABLE adapter_decline (
    adapter_key        TEXT PRIMARY KEY,
    -- The stable code for why the source is unusable.
    reason             TEXT NOT NULL,
    -- The evidence sentence: which URL answered what, or which rule.
    detail             TEXT,
    evidence_url       TEXT,
    -- Every request the adapter issued in the runs that evidenced the decline.
    request_count      INTEGER NOT NULL DEFAULT 0,
    -- ... of those, the ones that were refusals of the decline kind (403/451,
    -- robots.txt, 401).
    refused_count      INTEGER NOT NULL DEFAULT 0,
    -- Distinct campaigns that produced a refusal.  Per item as well as per
    -- run: two plan items for one adapter in one campaign are one run.
    run_count          INTEGER NOT NULL DEFAULT 0,
    last_campaign_id   TEXT,
    first_seen_at      TEXT NOT NULL,
    last_seen_at       TEXT NOT NULL,
    -- NULL until the pattern is judged persistent; then the moment it was.
    declined_at        TEXT,
    -- auto | admin.  ``auto`` today; the column keeps a future manual decline
    -- distinguishable without another migration.
    source             TEXT NOT NULL DEFAULT 'auto',
    acknowledged_at    TEXT,
    acknowledged_by    TEXT,
    acknowledged_note  TEXT,
    updated_at         TEXT NOT NULL
);

-- The planner's question: which adapters are declined right now?
CREATE INDEX idx_adapter_decline_active ON adapter_decline(declined_at, acknowledged_at);
