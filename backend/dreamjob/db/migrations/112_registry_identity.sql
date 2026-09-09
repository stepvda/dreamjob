-- ===========================================================================
-- The registry identity of a company, and the namesake domains it exposes
-- (DR-101, FR-181, FR-184, FR-241, FR-341, NFR-402, CR-405)
--
-- Two tables and one correction, all of them about the same failure: a name
-- match is the weakest DR-101 key, and both places this installation uses one
-- have been anchoring facts on the wrong organisation.
--
--  1. ``company_registry_identity`` - which legal entity a company name was
--     resolved to, by which rule, and *whether the register refused*.  Today
--     the resolution is thrown away: ``KBOAdapter.search_by_name`` returns a
--     number and the caller writes it onto the company, so "the register does
--     not know this company", "two registered companies share this name" and
--     "nobody has looked yet" are indistinguishable afterwards - and the
--     phonetic search's wrong picks (six of thirty measured names: SMALS to a
--     gravel company, TOURING NV to "A TOURING COMPANY BV" at similarity
--     1.00) left no trace to review.  A refusal is a finding and is recorded
--     as one, with the candidate rows attached, so the employer-kind rung can
--     answer ``cannot_tell(registry_ambiguous)`` and show a person the two
--     rows rather than pick one (docs/Agency_Research_Design.md section 2.1).
--
--     The enterprise number is cached forever - a company keeps it for life -
--     and ``expires_at`` covers only the activity codes read from the page,
--     which are occasionally added to.
--
--  2. ``company_domain_revocation`` - what the hardened identity gate cleared,
--     and why.  A derived domain becomes a company's e-mail domain, its
--     "official website" in a briefing and, once the employer-kind rung
--     reads it, the evidence for what kind of organisation it is.  When the
--     domain belongs to a namesake, every one of those is confidently wrong,
--     and clearing it silently would leave the next pass to re-derive it.
--     The revocation row is the memory of the decision (NFR-402).
--
-- The correction at the bottom clears the three namesakes that are in the
-- database now, each verified by hand on 2026-09-09:
--
--   Bright Plus NV      -> brightplus.com     a Finnish coatings maker
--   Adéquat Belgium NV  -> adequat.com        a Canadian translation bureau
--   think about IT GmbH -> think-about-it.com redirects to a Hyundai 404
--
-- All three passed ``company_named_on_page`` because the page does name the
-- words in the company's name - "Home - brightplus.com" names "bright plus".
-- The address spelled on each of them (info@brightplus.com, admin@adequat.com,
-- jobs@think-about-it.com) is a real mailbox at an unrelated organisation, so
-- the contact rows go with the domain (FR-306, RK-08): keeping a stranger's
-- address on file against a company it does not work for is the failure
-- CR-405 exists to prevent, and it is also a privacy defect.
--
-- The rest of the 264 derived domains are re-verified by
-- ``employer_registry_rung.reverify_derived_domains``, which fetches each
-- page and writes its own revocation rows: SQL cannot check a redirect.
-- ===========================================================================

-- --- 1. which legal entity this company is ---------------------------------
CREATE TABLE company_registry_identity (
    company_id          TEXT PRIMARY KEY REFERENCES company(id) ON DELETE CASCADE,
    registry            TEXT NOT NULL,        -- kbo_bce|companies_house|kvk
    -- matched     the gate resolved the name to exactly one registered entity
    -- ambiguous   the register holds more than one entity of this name
    -- no_match    the register does not hold an entity of this name
    -- unavailable the register did not answer; nothing is concluded
    decision            TEXT NOT NULL,
    legal_id            TEXT,                 -- enterprise/company number, when matched
    registered_name     TEXT,                 -- the name as the register spells it
    municipality        TEXT,                 -- seat, the tie-breaker for namesakes
    postcode            TEXT,
    match_rule          TEXT,                 -- exact_name|extended_name
    -- Was the seat compared with the places this company's vacancies are in?
    -- A one-word name matched without it is the class the phonetic search
    -- gets wrong, so the verdict built on it is capped and corroborated.
    municipality_checked INTEGER NOT NULL DEFAULT 0,
    queried_name        TEXT NOT NULL,        -- what was actually asked for
    candidates          TEXT,                 -- JSON: the rows considered (NFR-402)
    reason              TEXT,                 -- why, in words
    source_url          TEXT,
    established_at      TEXT NOT NULL,
    expires_at          TEXT,                 -- the activity codes, not the number
    attempts            INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_registry_identity_decision ON company_registry_identity(decision);
CREATE INDEX idx_registry_identity_legal_id ON company_registry_identity(legal_id);
CREATE INDEX idx_registry_identity_expiry ON company_registry_identity(expires_at);

-- --- 2. what the identity gate cleared, and why ----------------------------
CREATE TABLE company_domain_revocation (
    id              TEXT PRIMARY KEY,
    company_id      TEXT REFERENCES company(id) ON DELETE SET NULL,
    company_name    TEXT NOT NULL,            -- kept: the row outlives the company row
    domain          TEXT NOT NULL,
    previous_source TEXT,                     -- company|vacancy_text|derived_confirmed
    -- namesake|off_domain_redirect|country_mismatch|weak_identity|parked|
    -- js_rendered_or_empty|unreachable
    reason          TEXT NOT NULL,
    evidence        TEXT,                     -- what the gate saw, in words
    gate_version    TEXT NOT NULL DEFAULT 'v2',
    contacts_removed INTEGER NOT NULL DEFAULT 0,
    cleared_at      TEXT NOT NULL
);
CREATE INDEX idx_domain_revocation_domain ON company_domain_revocation(domain);
CREATE INDEX idx_domain_revocation_reason ON company_domain_revocation(reason, cleared_at);

-- --- 3. the three namesakes that are in the database now --------------------
-- Recorded first: the UPDATE below removes the evidence this SELECT reads.
INSERT INTO company_domain_revocation (
    id, company_id, company_name, domain, previous_source, reason, evidence,
    gate_version, contacts_removed, cleared_at
)
SELECT
    lower(hex(randomblob(16))),
    c.id,
    c.name,
    r.domain,
    r.domain_source,
    'namesake',
    CASE r.domain
        WHEN 'brightplus.com' THEN
            'brightplus.com is BrightBio, a Finnish coatings maker; the page title '
            || '"Home - brightplus.com" names the company only by repeating the domain, '
            || 'and the site shows no Belgian address, telephone prefix or language'
        WHEN 'adequat.com' THEN
            'adequat.com is "Adéquat, services linguistiques inc.", a Canadian translation '
            || 'bureau; the schema.org name is not "Adéquat" and the address block is Canadian'
        ELSE
            'think-about-it.com redirects off-domain to a Hyundai error page; the final '
            || 'registrable domain is not the one the verdict was attached to'
    END,
    'v2',
    (SELECT COUNT(*) FROM contact ct
      WHERE ct.company_id = c.id AND lower(ct.email) LIKE '%@' || r.domain),
    strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')
FROM apply_contact_resolution r
JOIN company c ON c.id = r.company_id
WHERE r.domain IN ('brightplus.com', 'adequat.com', 'think-about-it.com');

-- The address was spelled on somebody else's domain: it goes with it.
DELETE FROM contact
 WHERE lower(email) LIKE '%@brightplus.com'
    OR lower(email) LIKE '%@adequat.com'
    OR lower(email) LIKE '%@think-about-it.com';

UPDATE company
   SET domain = NULL
 WHERE lower(domain) IN ('brightplus.com', 'adequat.com', 'think-about-it.com');

-- FR-301: the company becomes *unreachable with a reason*, which is a finding
-- the Apply Browser shows, and not an invitation to derive the same domain
-- again on the next pass.
UPDATE apply_contact_resolution
   SET status        = 'unreachable',
       contact_id    = NULL,
       email         = NULL,
       domain        = NULL,
       domain_source = NULL,
       method        = NULL,
       validation    = NULL,
       is_generic    = 0,
       reason        = 'the confirmed domain belonged to a namesake and was cleared by the '
                       || 'v2 identity gate; see company_domain_revocation',
       resolved_at   = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')
 WHERE domain IN ('brightplus.com', 'adequat.com', 'think-about-it.com');

-- FR-305: a rejected verdict, so the ladder does not re-confirm the domain
-- the moment the probe cache expires.
UPDATE apply_domain_probe
   SET outcome        = 'rejected',
       evidence       = 'cleared by the v2 identity gate: the site belongs to a different '
                        || 'organisation of a similar name (see company_domain_revocation)',
       last_probed_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')
 WHERE domain IN ('brightplus.com', 'adequat.com', 'think-about-it.com');
