-- ---------------------------------------------------------------------------
-- 093: hygiene indexes for collection at campaign scale.
--
-- Plan reference: docs/Data_Gathering_Plan.md section 5.2 item N7 and section
-- 2.4 ("Writes").  Requirements: FR-166 (every record is linked to the plan
-- item and adapter that produced it), FR-183/DR-102 (the raw-document store),
-- FR-184 (de-duplication), NFR-103 (a campaign fits in four hours).
--
-- Campaign A writes ~97,000 vacancy rows.  Three lookups on that path have no
-- index behind them today, and each of them is executed once per written row:
--
--  1. the unresolved-company candidate scan in
--     ``knowledge.vacancy_candidates`` filters ``company_name_raw`` and
--     ``collected_at`` and had to scan the whole table: 18 ms per new vacancy
--     at 100k rows, which is ~30 minutes of pure scanning per 100k rows;
--  2. the FR-166 provenance trail is read back per entity type and adapter for
--     the coverage dashboard and for the "what did this campaign do" audit,
--     which scanned every provenance row (one or two per collected record);
--  3. company rows are now keyed on (ats_vendor, ats_slug) when a board writes
--     a vacancy - the identity that makes ``max_companies`` measure something -
--     and that lookup runs once per board, ~6,900 times in Campaign A.
--
-- The fourth index serves the 30-day orphan sweep of ``raw_document``, which
-- selects by ``fetched_at``.
--
-- Indexes only: no table is altered and no row is touched, so this migration
-- is safe to apply to a populated database.
-- ---------------------------------------------------------------------------

-- 1. The unresolved-company candidate scan (FR-184, knowledge.vacancy_candidates).
CREATE INDEX IF NOT EXISTS idx_vacancy_company_name
    ON vacancy(company_name_raw, collected_at);

-- 2. The provenance trail, read per entity type / period / adapter (FR-166).
CREATE INDEX IF NOT EXISTS idx_prov_type_created
    ON provenance(entity_type, created_at, adapter_key);

-- 3. The board identity a collected vacancy is attached to (DR-101, FR-162).
--    Partial, because only companies that actually run a public board carry it.
CREATE INDEX IF NOT EXISTS idx_company_ats_board
    ON company(ats_vendor, ats_slug)
    WHERE ats_vendor IS NOT NULL AND ats_slug IS NOT NULL;

-- 4. The 30-day orphan sweep of the raw-document store (FR-183, DR-102).
CREATE INDEX IF NOT EXISTS idx_rawdoc_fetched
    ON raw_document(fetched_at);
