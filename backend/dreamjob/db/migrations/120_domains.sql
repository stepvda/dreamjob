-- ===========================================================================
-- Company domains, and the candidates the identity gate refused (FR-301,
-- FR-303, FR-304, FR-305, NFR-402, CR-405)
--
-- The FR-301 contact ladder cannot spell an address before it knows a domain,
-- and 867 of the 1,430 companies in the corpus have none - 1,700 vacancies
-- with nothing to apply to, which is the whole gap between the coverage the
-- Apply Browser shows and the coverage the brief asks for.
-- ``dreamjob.pipeline.company_domains`` walks a ladder for those companies and
-- this migration is where the ladder writes down what it concluded.
--
-- **Two tables, because two different questions are asked of this data.**
--
-- ``company_domain_resolution`` answers "what happened to this company" - one
-- row per company, the domain and the rung that produced it, or the reason
-- there is none.  It is the twin of ``apply_contact_resolution`` one step
-- earlier in the ladder: a company that cannot be given a domain honestly is
-- a *finding*, and recording it is what stops the next pass re-deriving the
-- same eight dead spellings and what lets a screen say "the ladder was walked
-- and nothing could be confirmed" rather than "not looked at yet".
--
-- ``company_domain_candidate`` answers "what did the gate refuse, and why" -
-- one row per (company, candidate domain).  A pass that resolves 300 domains
-- by rejecting 4,000 candidates is a different thing from one that resolves
-- 300 by trying 320, and only the second is worth worrying about.  Three
-- namesake domains (brightplus.com, adequat.com, think-about-it.com) had to be
-- deleted from this database after an earlier gate let them through, so the
-- refusals are the evidence that the gate is doing its work and they are kept
-- where they can be counted in SQL rather than buried in a log line.
--
-- **Why not ``apply_domain_probe``.**  That table is keyed on the domain alone
-- and holds one verdict per domain, which is right for "does this domain have
-- a mail exchanger" and wrong for "does this page belong to *this* company":
-- two companies can spell to the same domain and the answer differs.  Nothing
-- here replaces it - the MX answer and the fetch are still cached there
-- (FR-305) - these rows record the company-specific judgement it cannot hold.
--
-- RK-08 / FR-344: no person, no job seeker.  A company's domain does not
-- change between job seekers, so this is shared knowledge-base fact.
-- ===========================================================================

CREATE TABLE company_domain_resolution (
    company_id      TEXT PRIMARY KEY REFERENCES company(id) ON DELETE CASCADE,
    company_name    TEXT NOT NULL DEFAULT '',
    -- resolved | unresolved
    status          TEXT NOT NULL,
    domain          TEXT,
    -- Which rung answered: held_contact | vacancy_text | vacancy_link |
    -- ats_board | register | derived_confirmed, or NULL when none did.
    source          TEXT,
    -- What said so, in the words the rung used: the address that carried the
    -- domain, the register's own "Web Address" line, or the identity rule the
    -- page satisfied.  NFR-402: a resolution without evidence is not one.
    evidence        TEXT,
    -- The market the spelling and the country rule were run against, and
    -- where that market came from (company | vacancy | board | none), so a
    -- verdict reached on an inferred market can be told from one reached on
    -- a recorded one.
    market          TEXT,
    market_source   TEXT,
    candidates      INTEGER NOT NULL DEFAULT 0,
    rejected        INTEGER NOT NULL DEFAULT 0,
    reason          TEXT,
    -- The identity gate that judged it, so a later hardening can find every
    -- domain an older gate let through (see company_domain_revocation).
    gate_version    TEXT,
    vacancy_count   INTEGER NOT NULL DEFAULT 0,
    resolved_at     TEXT NOT NULL
);

CREATE INDEX idx_domain_resolution_status ON company_domain_resolution(status, resolved_at);
CREATE INDEX idx_domain_resolution_source ON company_domain_resolution(source);

CREATE TABLE company_domain_candidate (
    company_id  TEXT NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    domain      TEXT NOT NULL,
    -- Where the candidate came from, same vocabulary as the resolution row.
    source      TEXT NOT NULL,
    -- accepted | confirmed | review | rejected | no_mx | unreachable | revoked
    outcome     TEXT NOT NULL,
    reason      TEXT,
    decided_at  TEXT NOT NULL,
    PRIMARY KEY (company_id, domain)
);

CREATE INDEX idx_domain_candidate_outcome ON company_domain_candidate(outcome);
CREATE INDEX idx_domain_candidate_domain ON company_domain_candidate(domain);
