-- ===========================================================================
-- The Apply Browser: selection, contact reachability and the list query
-- (FR-301, FR-303, FR-304, FR-305, FR-321, FR-324, NFR-302, NFR-502, RK-08)
--
-- Four additions, each because a requirement cannot be met by the existing
-- schema:
--
--  1. FR-324 - the Apply Browser's selection has to survive a reload, and
--     ``opportunity.selected`` cannot carry it on its own.  That flag is
--     rewritten by the ranked list (FR-284) and cleared when a campaign is
--     re-synthesised, so a selection made in the Apply Browser would silently
--     evaporate on the next scoring pass.  ``apply_selection`` is the
--     browser's own record and it also carries the per-row workflow state the
--     screen shows, which ``opportunity`` has nowhere to put.
--
--  2. FR-301 / RK-08 - "a company with no discoverable contact is recorded as
--     unreachable".  There is no row anywhere that can say that today: the
--     absence of a ``contact`` row is indistinguishable from "nobody has
--     looked yet", and the interface has to be able to tell those apart or it
--     invites the job seeker to run discovery again on 700 companies that
--     have already been searched.  ``apply_contact_resolution`` is one row per
--     company - reachable or unreachable, by which FR-303 method, with which
--     FR-304 verdict, and why not when not.
--
--  3. FR-305 - "rate-limit and cache; do not probe the same domain
--     repeatedly".  ``email_domain_state`` caches the *MX* answer per domain
--     and ``http_cache`` caches individual *pages*, but neither records the
--     question the Apply Browser asks: "is this domain the one this company
--     actually uses, and have I already found that out?".  A rejected domain
--     with no cache entry is re-derived and re-fetched on every pass.
--     ``apply_domain_probe`` remembers the verdict, so a domain is confirmed
--     or rejected once.
--
--  4. NFR-502 - the browser's list query is one join across opportunity,
--     company, contact, application_package and dispatch.  The indexes at the
--     bottom are the ones that join needs; without them the correlated
--     sub-selects that find the live package and the best contact scan those
--     tables once per row on screen.
--
-- RK-08: nothing here stores anything about a person.  The reachability row
-- points at a ``contact`` id and repeats the address that is already on it;
-- the discovered address itself continues to live in ``contact``, behind the
-- FR-306 minimisation gate.
-- ===========================================================================

-- --- FR-324: the Apply Browser's own selection -----------------------------
CREATE TABLE apply_selection (
    id              TEXT PRIMARY KEY,
    job_seeker_id   TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    opportunity_id  TEXT NOT NULL REFERENCES opportunity(id) ON DELETE CASCADE,
    -- selected|generating|ready|approved|sent|skipped|failed
    status          TEXT NOT NULL DEFAULT 'selected',
    note            TEXT,
    selected_at     TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (job_seeker_id, opportunity_id)
);
CREATE INDEX idx_apply_selection_seeker ON apply_selection(job_seeker_id, status);

-- --- FR-301/FR-303/FR-304: is this company reachable, and how --------------
-- One row per company, whatever the answer.  ``status = 'unreachable'`` is a
-- finding, not a gap: it means the whole FR-301 ladder was walked and nothing
-- survived FR-304, and the Apply Browser shows the company as such rather
-- than offering an address nobody verified.
CREATE TABLE apply_contact_resolution (
    company_id      TEXT PRIMARY KEY REFERENCES company(id) ON DELETE CASCADE,
    status          TEXT NOT NULL,           -- reachable|unreachable
    contact_id      TEXT REFERENCES contact(id) ON DELETE SET NULL,
    email           TEXT,
    domain          TEXT,
    -- Where the domain came from, because an address is only as trustworthy
    -- as the domain it sits on: company|vacancy_text|derived_confirmed.
    domain_source   TEXT,
    -- FR-303 verbatim: website|press|pattern_inference|lookup_service|vacancy.
    method          TEXT,
    -- FR-304 verbatim: valid|risky|unknown  ('invalid' is never stored here).
    validation      TEXT,
    is_generic      INTEGER NOT NULL DEFAULT 0,
    reason          TEXT,                    -- why unreachable, in words
    vacancy_count   INTEGER NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 1,
    first_seen_at   TEXT NOT NULL,
    resolved_at     TEXT NOT NULL
);
CREATE INDEX idx_apply_resolution_status ON apply_contact_resolution(status, resolved_at);
CREATE INDEX idx_apply_resolution_method ON apply_contact_resolution(method);

-- --- FR-305: one verdict per domain, so no domain is probed twice ----------
CREATE TABLE apply_domain_probe (
    domain          TEXT PRIMARY KEY,
    -- confirmed  the site is reachable and names the company
    -- rejected   the site is reachable and is somebody else's
    -- no_mx      the domain cannot receive mail, so no address on it can work
    -- unreachable  no answer; retried after the cache window, not immediately
    outcome         TEXT NOT NULL,
    company_id      TEXT,                    -- the company the probe was for
    evidence        TEXT,                    -- what made the verdict, in words
    probe_count     INTEGER NOT NULL DEFAULT 1,
    first_probed_at TEXT NOT NULL,
    last_probed_at  TEXT NOT NULL
);
CREATE INDEX idx_apply_probe_outcome ON apply_domain_probe(outcome, last_probed_at);

-- --- NFR-502: the indexes the browser's one round trip needs ---------------
-- The list query filters the seeker's selection, then finds the live package,
-- the best usable contact and the last dispatch for each row on screen.
CREATE INDEX idx_opp_apply_selected
    ON opportunity(job_seeker_id, selected, campaign_id);
CREATE INDEX idx_pkg_opportunity
    ON application_package(opportunity_id, job_seeker_id, status, created_at DESC);
CREATE INDEX idx_dispatch_package_sent
    ON dispatch(application_package_id, sent_at DESC);
-- ``usable_contact`` reads ``contact``; the browser wants the most confident
-- address at a company, and the ladder wants the addresses already on a domain.
CREATE INDEX idx_contact_company_confidence
    ON contact(company_id, confidence DESC);

-- The contacts-at-scale pass walks vacancies newest first, grouped by company.
CREATE INDEX idx_vacancy_company_posted ON vacancy(company_id, posted_at DESC);
CREATE INDEX idx_vacancy_posted ON vacancy(posted_at DESC);
