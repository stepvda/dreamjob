-- ===========================================================================
-- Provenance by plan item (NFR-101, FR-166)
--
-- ``campaign_company_ids`` joins provenance to source_plan_item on
-- ``p.source_plan_item_id = s.id`` and filters by the campaign.  SQLite drives
-- from source_plan_item (it has ``idx_plan_campaign``), then needs the
-- provenance rows belonging to each plan item - and there was no index on that
-- column at all.  Every plan item therefore scanned all 174,407 provenance
-- rows: 3,020,000 VM steps, 5.9 seconds, measured in logs/database.log and
-- reproduced directly.
--
-- The index carries ``entity_type`` and ``entity_id`` as well, so the query's
-- ``entity_type = 'company'`` filter and its ``entity_id`` projection are both
-- served from the index and the table is never touched.
-- ===========================================================================

CREATE INDEX IF NOT EXISTS idx_prov_plan_item
    ON provenance(source_plan_item_id, entity_type, entity_id);

-- The second half of the same UNION walks provenance by (entity_type, entity_id)
-- and then to source_plan_item by primary key.  ``idx_prov_entity`` already
-- covers that leading pair; adding the plan item lets that leg likewise finish
-- in the index.
CREATE INDEX IF NOT EXISTS idx_prov_entity_plan
    ON provenance(entity_type, entity_id, source_plan_item_id);
