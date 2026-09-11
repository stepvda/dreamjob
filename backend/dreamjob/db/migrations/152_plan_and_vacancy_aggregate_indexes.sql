-- ===========================================================================
-- Two indexes the two slow list paths need (NFR-101, NFR-102)
--
-- 1. ``list_plan_items`` reads every ``source_plan_item`` of one campaign in
--    ``created_at, adapter_key`` order.  ``idx_plan_campaign`` is
--    ``(campaign_id, status)``, so the planner fetched the campaign's rows and
--    then sorted them in a temporary B-tree on every plan read.  Indexing the
--    same columns in the ordered order lets the planner walk the rows already
--    sorted and drop the temp B-tree.
--
-- 2. The all-companies sweep used two correlated subqueries per company for
--    ``vacancy_count`` and ``latest_vacancy_at``.  The rewrite folds both into
--    one grouped read of ``vacancy``.  That read evaluates
--    ``MAX(COALESCE(posted_at, collected_at))``, which reads either column and
--    is covered by no existing ``vacancy`` index, so the grouped read fell back
--    to a full scan of the wide ``vacancy`` table (~6 s on the live corpus).
--    This covering index keeps the aggregate index-only.
--
-- ``IF NOT EXISTS`` because a forward migration must be safe to re-run if a
-- process died between the DDL and the ``schema_migration`` row.
-- ===========================================================================

CREATE INDEX IF NOT EXISTS idx_plan_campaign_created
    ON source_plan_item(campaign_id, created_at, adapter_key);

CREATE INDEX IF NOT EXISTS idx_vacancy_company_agg
    ON vacancy(company_id, posted_at, collected_at);
