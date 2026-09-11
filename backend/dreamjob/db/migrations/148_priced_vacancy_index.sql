-- ===========================================================================
-- The comparison sets only ever read priced vacancies (FR-264)
--
-- ``posted_salary_corpus`` filters on ``function_family``, ``seniority``,
-- ``country``, ``company_id`` and a ``title LIKE`` width, always with
-- ``salary_min IS NOT NULL AND salary_max IS NOT NULL``.  The ordinary indexes
-- cover the equality filters but not that predicate, so the broad title width -
-- ``WHERE country = ? AND (title LIKE ? OR ...)`` - scanned all 55,057 vacancies
-- for every distinct title, a full scan per opportunity in a campaign of
-- 48,269.  Pricing a whole campaign did not finish.
--
-- Fewer than one vacancy in ten carries a range, so a partial index over just
-- those rows turns every one of these queries into a scan of a few thousand
-- frames instead of tens of thousands.
-- ===========================================================================

CREATE INDEX IF NOT EXISTS idx_vacancy_priced
    ON vacancy(company_id, function_family, seniority)
    WHERE salary_min IS NOT NULL AND salary_max IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_vacancy_priced_country
    ON vacancy(country)
    WHERE salary_min IS NOT NULL AND salary_max IS NOT NULL;
