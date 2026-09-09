-- ===========================================================================
-- Discovery: the board a company is read through is an identity (DR-101)
--
-- The planner now materialises targets before it translates anything
-- (FR-162, docs/Data_Gathering_Plan.md section 5.2 N1): one plan item per
-- (ATS vendor, board slug) from the board registry, from the knowledge base
-- and from the companies the job seeker named.  That makes the pair a
-- first-class identity of the company behind it - for most registry boards it
-- is the *only* identity available, because a Greenhouse or Ashby payload
-- carries no legal identifier, no VAT number and often no company name at all.
--
-- DR-101 ranks identities legal_id > VAT > domain > normalised name.  A board
-- slug slots in beside the domain: it is issued by the vendor, it is unique
-- within that vendor, and two rows carrying the same pair are the same
-- employer recorded twice.  Without the constraint below, every campaign that
-- reads the same board again creates another company row and the 7,500-company
-- target becomes a count of duplicates.
-- ===========================================================================

-- 1. Vendors are catalogue keys and are written lower case ("greenhouse",
--    "lever").  Rows captured before that was settled are normalised here so
--    the uniqueness below can actually see a duplicate.  Slugs keep their case:
--    Workday tenant slugs are case-sensitive.
UPDATE company
   SET ats_vendor = LOWER(TRIM(ats_vendor))
 WHERE ats_vendor IS NOT NULL
   AND ats_vendor <> LOWER(TRIM(ats_vendor));

UPDATE company
   SET ats_slug = TRIM(ats_slug)
 WHERE ats_slug IS NOT NULL
   AND ats_slug <> TRIM(ats_slug);

-- Empty strings are not identities; store the absence of a board as NULL so
-- the partial index below never has to reason about "" as a slug.
UPDATE company
   SET ats_vendor = NULL
 WHERE ats_vendor IS NOT NULL AND TRIM(ats_vendor) = '';

UPDATE company
   SET ats_slug = NULL
 WHERE ats_slug IS NOT NULL AND TRIM(ats_slug) = '';

-- 2. Existing duplicates.  Merging two company rows is a knowledge-base
--    operation (FR-184) and cannot be done safely here: a dozen tables point
--    at company.id.  What this migration can do is leave the board on the row
--    that recorded it first and clear it from the later duplicates, so the
--    constraint can be created.  Nothing of substance is lost - a slug is a
--    route, and the next campaign re-discovers it from the registry - while
--    the identity stops being ambiguous.
UPDATE company
   SET ats_vendor = NULL, ats_slug = NULL
 WHERE id IN (
        SELECT later.id
          FROM company AS later
         WHERE later.ats_vendor IS NOT NULL
           AND later.ats_slug IS NOT NULL
           AND EXISTS (
                SELECT 1
                  FROM company AS first_seen
                 WHERE first_seen.ats_vendor = later.ats_vendor
                   AND first_seen.ats_slug = later.ats_slug
                   AND (first_seen.collected_at < later.collected_at
                        OR (first_seen.collected_at = later.collected_at
                            AND first_seen.id < later.id))
               )
       );

-- 3. The identity itself.  Partial, so the millions of companies that have no
--    board (every register import) are not forced to share one NULL slot.
CREATE UNIQUE INDEX uq_company_ats_board
    ON company(ats_vendor, ats_slug)
 WHERE ats_vendor IS NOT NULL AND ats_slug IS NOT NULL;

-- 4. Reading the known boards back is a planning-time query on every campaign
--    ("which companies already have a board I can go straight to?"), and it
--    asks for the freshest first.  Without this it is a full scan of company
--    plus a sort; with it, discovery reads the boards it needs off the index.
CREATE INDEX idx_company_board_freshness
    ON company(COALESCE(refreshed_at, collected_at) DESC)
 WHERE ats_vendor IS NOT NULL AND ats_slug IS NOT NULL;
