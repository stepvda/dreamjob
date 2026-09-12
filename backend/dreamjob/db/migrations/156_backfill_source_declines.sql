-- ===========================================================================
-- Backfill the declines the campaigns already proved (FR-182, IR-101)
--
-- Migration 154 creates the memory, but the evidence it exists for is already
-- in ``source_plan_item``: one autopilot run recorded five Indeed and ten
-- Jobat requests as ``blocked``, and a catalogue row like ``board.stepstone``
-- has said "prohibited" since before either run.  Without this backfill the
-- next plan would select those sources once more, charge the same refused
-- requests once more, and only then decline them - which is exactly the rerun
-- loop the table exists to stop.
--
-- Two sources of evidence, both conservative:
--
-- 1. **Terms the catalogue already states.**  A row whose ``tos_status`` is
--    ``prohibited`` and that no administrator has acknowledged is declined
--    immediately - IR-101 needs no request threshold - and its reason is the
--    stronger statement, so it is recorded first.
-- 2. **Blocked plan items.**  An adapter is declined only when the refusals
--    satisfy the same threshold the forward policy applies (migration 154;
--    three refused requests, or refusals recorded in two distinct campaigns),
--    *and* the adapter has never written a record on any plan item.  A
--    per-tenant ATS vendor with one dead board and a hundred live ones is
--    working, and the "never succeeded" guard is what keeps it out.
--
-- The reason code is derived from the recorded refusals: robots.txt if that is
-- all the evidence, otherwise the HTTP spelling, defaulting to 403.
-- ===========================================================================

-- IR-101: a prohibited row nobody has acknowledged is already a decline.
INSERT INTO adapter_decline (
    adapter_key, reason, detail, evidence_url,
    request_count, refused_count, run_count, last_campaign_id,
    first_seen_at, last_seen_at, declined_at, source,
    acknowledged_at, acknowledged_by, acknowledged_note, updated_at
)
SELECT
    c.adapter_key,
    'terms',
    COALESCE(c.display_name, c.adapter_key) ||
        ': terms of service prohibit automated access (IR-101)',
    NULL,
    0, 0, 0, NULL,
    c.updated_at, c.updated_at, c.updated_at, 'auto',
    NULL, NULL, NULL, c.updated_at
FROM source_catalogue c
WHERE c.tos_status = 'prohibited'
  AND c.acknowledged_at IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM adapter_decline d WHERE d.adapter_key = c.adapter_key
  );

INSERT INTO adapter_decline (
    adapter_key, reason, detail, evidence_url,
    request_count, refused_count, run_count, last_campaign_id,
    first_seen_at, last_seen_at, declined_at, source,
    acknowledged_at, acknowledged_by, acknowledged_note, updated_at
)
SELECT
    p.adapter_key,
    CASE
        WHEN p.robots_items = p.items THEN 'robots'
        WHEN p.http_451_items > 0 THEN 'http_451'
        WHEN p.http_401_items > 0 THEN 'http_401'
        ELSE 'http_403'
    END,
    'derived at migration from ' || p.refused || ' refused request(s) in ' ||
        p.runs || ' campaign(s) recorded before declines existed (FR-182)',
    NULL,
    p.refused,
    p.refused,
    p.runs,
    NULL,
    p.first_seen,
    p.last_seen,
    p.last_seen,
    'auto',
    NULL, NULL, NULL,
    p.last_seen
FROM (
    SELECT
        s.adapter_key,
        COUNT(*) AS items,
        COUNT(DISTINCT s.campaign_id) AS runs,
        SUM(COALESCE(s.blocked_count, 0)) AS refused,
        SUM(CASE WHEN s.last_error LIKE '%robots.txt%' THEN 1 ELSE 0 END) AS robots_items,
        SUM(CASE WHEN s.last_error LIKE '%HTTP 451%' THEN 1 ELSE 0 END) AS http_451_items,
        SUM(CASE WHEN s.last_error LIKE '%HTTP 401%' THEN 1 ELSE 0 END) AS http_401_items,
        MIN(COALESCE(s.activity_at, s.created_at)) AS first_seen,
        MAX(COALESCE(s.activity_at, s.created_at)) AS last_seen
    FROM source_plan_item s
    WHERE s.status = 'blocked'
      AND COALESCE(s.records_collected, 0) = 0
      AND COALESCE(s.error_count, 0) = 0
      AND COALESCE(s.blocked_count, 0) > 0
      AND s.adapter_key NOT IN (
          SELECT adapter_key FROM source_plan_item
          WHERE status = 'done' AND COALESCE(records_collected, 0) > 0
      )
    GROUP BY s.adapter_key
    HAVING SUM(COALESCE(s.blocked_count, 0)) >= 3
        OR COUNT(DISTINCT s.campaign_id) >= 2
) p
WHERE NOT EXISTS (
    SELECT 1 FROM adapter_decline d WHERE d.adapter_key = p.adapter_key
);
