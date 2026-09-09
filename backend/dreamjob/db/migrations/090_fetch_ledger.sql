-- ---------------------------------------------------------------------------
-- 090: the fetch ledger, and the http_cache columns a conditional GET needs.
--
-- Plan reference: docs/Data_Gathering_Plan.md section 4 (L1/L3) and section 5.2
-- item N4.  Requirements: FR-182 (one response cache with a TTL so repeat
-- campaigns do not re-fetch unchanged pages), FR-183/DR-102 (a cached body is
-- still a stored raw document and must keep its provenance link), FR-342
-- (knowledge-base reuse), NFR-103 (a campaign fits in four hours).
--
-- Why a ledger at all.  Reuse is decided today per *adapter*: after any run
-- writes a hundred fresh ats.greenhouse rows, every Greenhouse board is skipped
-- for a week, including the 3,399 boards nobody has ever read.  A ledger row
-- per (adapter, target, url) makes the question answerable for one board:
-- "when did we last read *this* target, what did the server call it, and how
-- much did it give us?"  That is the difference between a four-hour second
-- campaign and a four-minute one.
-- ---------------------------------------------------------------------------

-- The key is natural, like http_cache's and robots_cache's: there is exactly
-- one row per (adapter, target, url), and re-reading a target updates it in
-- place rather than growing history nobody reads.  A paginated target keeps one
-- row per page; last_fetch() answers for the target as a whole.
CREATE TABLE fetch_ledger (
    adapter_key     TEXT NOT NULL,           -- e.g. 'ats.greenhouse'
    target_key      TEXT NOT NULL,           -- board slug, employer, query id
    url             TEXT NOT NULL,           -- the exact URL that was read
    etag            TEXT,                    -- validators, for the next If-None-Match
    last_modified   TEXT,
    fetched_at      TEXT NOT NULL,
    http_status     INTEGER,                 -- 200, 304, 404 ... whatever it ended as
    record_count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (adapter_key, target_key, url)
);

-- "Is this target fresh?" is a per-target max(fetched_at) lookup, and the
-- planner asks it once per plan item - thousands of times per campaign.
CREATE INDEX idx_fetch_ledger_target ON fetch_ledger(adapter_key, target_key, fetched_at DESC);
-- "Which targets of this adapter are still inside the staleness window?" is one
-- scan per adapter when a campaign is planned.
CREATE INDEX idx_fetch_ledger_fresh ON fetch_ledger(adapter_key, fetched_at);

-- ---------------------------------------------------------------------------
-- http_cache: carry the provenance a cache hit used to lose, and let a hit be
-- served without re-hashing the body.
--
-- ``content_hash`` and ``raw_document_id`` are what FR-183 needs on a cache
-- hit; resolving them by re-reading and re-hashing the file worked but cost a
-- SHA-256 over every cached body (3.6 MB for a large Greenhouse board) plus a
-- raw_document lookup on every hit.  Both are nullable because a negative cache
-- entry - a 404 or a 410 we promise not to re-probe for a week - has no body,
-- no hash and, deliberately, no stored document.
-- ---------------------------------------------------------------------------

ALTER TABLE http_cache ADD COLUMN content_hash TEXT;
ALTER TABLE http_cache ADD COLUMN raw_document_id TEXT;
