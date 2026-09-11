-- ===========================================================================
-- Registry seed: the complete company universe, before it is worth enriching
--
-- The knowledge base grows from collection: a board is scraped, a company is
-- discovered as a side effect, and it carries whatever that scrape happened to
-- say - no sector code, often no country. That is backwards for the question a
-- job seeker actually asks ("which companies in my field, near me, exist at
-- all?"), and it can never answer the one the product exists for: which
-- companies have *not* advertised. A crawl only ever sees companies that
-- already did.
--
-- The Belgian company register (KBO/BCE) is the complete, official, lawful
-- answer, published as a monthly open-data dump. This table is that dump,
-- staged: one row per registered entity with its NACE activity codes, legal
-- form, status and municipality. It is deliberately separate from ``company`` -
-- two million registered entities, mostly dormant shells and one-person
-- businesses, must never become two million knowledge-base rows. A seed is
-- promoted into ``company`` only when a campaign selects it, which is what
-- ``materialised_company_id`` records.
--
-- Shared, and it carries no job seeker (FR-344).
-- ===========================================================================

CREATE TABLE company_seed (
    id                      TEXT PRIMARY KEY,
    source                  TEXT NOT NULL DEFAULT 'kbo_bulk',
    entity_number           TEXT NOT NULL,          -- KBO/BCE enterprise number
    name                    TEXT NOT NULL,
    normalised_name         TEXT NOT NULL,
    status                  TEXT,                   -- active | stopped
    entity_type             TEXT,                   -- company | sole_trader | ...
    legal_form              TEXT,
    nace_codes              TEXT,                   -- JSON array, primary first
    nace_primary            TEXT,                   -- the 2-digit division of the first code
    municipality            TEXT,
    postcode                TEXT,
    country                 TEXT NOT NULL DEFAULT 'BE',
    start_date              TEXT,
    collected_at            TEXT NOT NULL,
    -- Set once the seed has been promoted into a ``company`` row, so a second
    -- campaign reuses the same company rather than creating a twin.
    materialised_company_id TEXT,
    UNIQUE (source, entity_number)
);

-- The selection the planner makes: sector first, because it is the most
-- selective and the whole point of staging.
CREATE INDEX IF NOT EXISTS idx_seed_nace ON company_seed(nace_primary, country);
CREATE INDEX IF NOT EXISTS idx_seed_geo ON company_seed(country, postcode);
CREATE INDEX IF NOT EXISTS idx_seed_name ON company_seed(normalised_name);
CREATE INDEX IF NOT EXISTS idx_seed_unpromoted ON company_seed(materialised_company_id);
