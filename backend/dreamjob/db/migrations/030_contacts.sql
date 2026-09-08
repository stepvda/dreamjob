-- ===========================================================================
-- Hiring-contact discovery, e-mail validation and introduction paths
-- (FR-301..FR-306, FR-461, NFR-302, NFR-303, CR-402, RK-08)
--
-- Four additions to 001_initial.sql.  Each exists because a requirement cannot
-- be met by convention alone:
--
--  1. NFR-302 - "honour objections by blocking the contact permanently".
--     ``contact.objected`` cannot carry that on its own: NFR-303 deletes the
--     contact row when the campaign's retention deadline passes, and the next
--     campaign would re-collect the same address from the same public page and
--     mail it again.  ``contact_objection`` outlives the contact row, and the
--     triggers below re-apply the block on every insert or e-mail change, so
--     the rule holds for every job seeker and not only in the interface.
--
--  2. FR-303 - the domain e-mail pattern inferred from observed addresses is a
--     property of the domain, not of a campaign, so it belongs in the SHARED
--     knowledge base with its own freshness stamp (FR-343, FR-344).
--
--  3. FR-305 - rate limiting and catch-all detection are per *domain* while
--     ``email_validation_cache`` is per address.  ``email_domain_state`` holds
--     the MX answer, the catch-all verdict and the probe budget that keeps the
--     system from hammering one mail server.
--
--  4. FR-302 / FR-461 - the job seeker's own network is private data about
--     third parties (FR-344 forbids putting it in ``contact``), and it is the
--     input the route ranking reads.  ``network_member`` carries the same
--     campaign scope and retention deadline as browser-collected contacts
--     (NFR-303).
--
-- RK-08: every table here stores professional identifiers only - no personal
-- addresses, no free-text notes about the person, no interaction history
-- beyond the dates the ranking needs.
-- ===========================================================================

-- --- NFR-302: the permanent block ------------------------------------------
-- Keyed on the address because honouring an objection means recognising the
-- address again later; nothing else about the person is kept.
CREATE TABLE contact_objection (
    email           TEXT PRIMARY KEY,            -- lowercased professional address
    domain          TEXT,
    linkedin_url    TEXT,                        -- blocks a person with no known address
    reason          TEXT,
    source          TEXT NOT NULL DEFAULT 'manual',  -- manual|reply|unsubscribe|bounce
    objected_at     TEXT NOT NULL
);
CREATE INDEX idx_objection_domain ON contact_objection(domain);

-- The query every outreach path must read (NFR-302, FR-304).  Enforcing the
-- block in the schema rather than in each caller is the point of the view:
-- "invalid" addresses are never used (FR-304) and an objection is permanent.
CREATE VIEW usable_contact AS
SELECT c.*
FROM contact c
WHERE c.objected = 0
  AND COALESCE(c.email_validation, 'unknown') <> 'invalid'
  AND (
        c.email IS NULL
        OR lower(c.email) NOT IN (SELECT email FROM contact_objection)
      )
  AND (
        c.linkedin_url IS NULL
        OR c.linkedin_url NOT IN (
            SELECT linkedin_url FROM contact_objection WHERE linkedin_url IS NOT NULL
        )
      );

-- A re-collected address is blocked again the moment it is written.
CREATE TRIGGER contact_objection_on_insert
AFTER INSERT ON contact
WHEN NEW.email IS NOT NULL AND NEW.objected = 0
     AND EXISTS (SELECT 1 FROM contact_objection o WHERE o.email = lower(NEW.email))
BEGIN
    UPDATE contact
       SET objected = 1,
           objected_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')
     WHERE id = NEW.id;
END;

CREATE TRIGGER contact_objection_on_email_change
AFTER UPDATE OF email ON contact
WHEN NEW.email IS NOT NULL AND NEW.objected = 0
     AND EXISTS (SELECT 1 FROM contact_objection o WHERE o.email = lower(NEW.email))
BEGIN
    UPDATE contact
       SET objected = 1,
           objected_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')
     WHERE id = NEW.id;
END;

-- Recording an objection blocks every copy of that address already stored.
CREATE TRIGGER contact_objection_applies_to_existing
AFTER INSERT ON contact_objection
BEGIN
    UPDATE contact
       SET objected = 1,
           objected_at = NEW.objected_at
     WHERE objected = 0 AND email IS NOT NULL AND lower(email) = NEW.email;
END;

-- --- FR-303: inferred address pattern per domain (SHARED) ------------------
CREATE TABLE email_pattern (
    domain          TEXT PRIMARY KEY,
    pattern         TEXT NOT NULL,               -- first.last|f.last|first|firstlast|...
    confidence      REAL NOT NULL DEFAULT 0.5,
    sample_count    INTEGER NOT NULL DEFAULT 0,  -- named addresses the inference saw
    supporting      INTEGER NOT NULL DEFAULT 0,  -- of those, how many fit `pattern`
    alternatives    TEXT,                        -- JSON [{pattern, support}]
    local_parts     TEXT,                        -- JSON, the local parts observed
    source          TEXT,                        -- website|press|manual|lookup_service
    inferred_at     TEXT NOT NULL
);

-- --- FR-304/FR-305: per-domain MX, catch-all verdict and probe budget ------
CREATE TABLE email_domain_state (
    domain              TEXT PRIMARY KEY,
    has_mx              INTEGER,                 -- NULL until looked up
    mx_hosts            TEXT,                    -- JSON, best-preference first
    mx_checked_at       TEXT,
    is_disposable       INTEGER NOT NULL DEFAULT 0,
    catch_all           INTEGER,                 -- NULL = not determined
    catch_all_checked_at TEXT,
    smtp_policy         TEXT,                    -- accepts|rejects_unknown|blocked|unknown
    probe_window_start  TEXT,                    -- start of the current probe budget window
    probe_count         INTEGER NOT NULL DEFAULT 0,
    last_probe_at       TEXT,
    updated_at          TEXT NOT NULL
);

-- --- FR-302/FR-461: the job seeker's own network (PRIVATE) -----------------
CREATE TABLE network_member (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    full_name           TEXT NOT NULL,
    headline            TEXT,
    role_title          TEXT,
    company_name        TEXT,
    company_id          TEXT REFERENCES company(id) ON DELETE SET NULL,
    linkedin_url        TEXT,
    degree              INTEGER NOT NULL DEFAULT 1,   -- 1 = own connection, 2 = via a mutual
    mutual_name         TEXT,                          -- FR-302: the named mutual contact
    mutual_linkedin     TEXT,
    schools             TEXT,                          -- JSON, for alumni matching
    employers           TEXT,                          -- JSON, for former-colleague matching
    communities         TEXT,                          -- JSON, shared groups and communities
    connected_at        TEXT,
    last_interaction_at TEXT,
    source              TEXT,                          -- linkedin_export|browser|manual
    access_method       TEXT NOT NULL DEFAULT 'manual',
    -- NFR-303: browser-collected people are campaign-scoped and expire.
    owning_campaign_id  TEXT REFERENCES campaign(id) ON DELETE SET NULL,
    retention_until     TEXT,
    collected_at        TEXT NOT NULL,
    UNIQUE (job_seeker_id, linkedin_url)
);
CREATE INDEX idx_network_seeker ON network_member(job_seeker_id, company_id);
CREATE INDEX idx_network_company_name ON network_member(job_seeker_id, company_name);
CREATE INDEX idx_network_retention ON network_member(retention_until);

-- --- FR-461: what the route ranking and the intermediary message need ------
ALTER TABLE introduction_path ADD COLUMN network_member_id TEXT;
ALTER TABLE introduction_path ADD COLUMN relevance REAL;
ALTER TABLE introduction_path ADD COLUMN rationale TEXT;
ALTER TABLE introduction_path ADD COLUMN message_subject TEXT;
ALTER TABLE introduction_path ADD COLUMN message_generated_at TEXT;
ALTER TABLE introduction_path ADD COLUMN updated_at TEXT;

-- NFR-303: the retention sweeper scans on this column.
CREATE INDEX idx_contact_retention ON contact(retention_until);
CREATE INDEX idx_contact_objected ON contact(objected);
