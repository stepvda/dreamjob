-- ===========================================================================
-- Who actually employs: the per-employer agency tag
-- (FR-341, FR-342, FR-343, FR-344, NFR-402, NFR-205, NFR-702, RK-08, CR-408)
--
-- docs/Interim_Agencies_Proposal.md and docs/Agency_Research_Design.md.
--
-- Interim, staffing and selection agencies post real vacancies on behalf of an
-- employer they do not name.  17-20% of the real corpus, 33-37% of the named
-- Belgian EURES slice and 80-92% of Actiris rows are such postings, and every
-- company-centred stage of this product - profiling, five years of accounts,
-- ability to pay, speculative openings, values match, the letter about why you
-- want to work *there* - is today computed against the agency and reads as
-- authoritative while being about the wrong organisation.
--
-- Four decisions are frozen into this schema.
--
--  1. **A tag on the employer, never a filter at collection.**
--     ``pipeline/discovery.py`` excludes NACE section N "because that is where
--     the staffing agencies sit"; EURES publishes NACE Rev 2.1, in which
--     staffing (division 78) is section **O** and N is professional, scientific
--     and technical activities.  Swapping the letter would not help: a vacancy
--     carries every section on its employer's list, so NOEL FRANKLIN's rows
--     arrive under C, F, N *and* O, and excluding O would drop whole legitimate
--     sectors (rental, cleaning, facility management) while still collecting
--     agencies.  The mechanism has to be one row per company - this table -
--     read by scoring, documents and the gates.
--
--  2. **``cannot_tell`` is a first-class state, with a reason and a retry.**
--     Neither default is acceptable: "assume employer" hides the sixty-vacancy
--     agency whose site could not be read (100G BV - JS-only site, NACE 62.x,
--     every vacancy at somebody else's plant); "assume agency" is the GitLab
--     failure with a different excuse (225 postings, 213 titles - every
--     diversity measure calls it an agency).  So ``kind`` has three values, the
--     reason is stored, and ``retry_after`` says when - or whether - the
--     cheapest next rung is worth spending.
--
--  3. **No verdict without evidence (NFR-402).**  A job seeker told that their
--     prospective employer is an agency must be able to see the sentence that
--     says so, on which page, established when and by which rung, and to
--     disagree with it.  ``evidence`` is therefore NOT NULL and a *decided*
--     verdict may not carry an empty one - the CHECK below is the reason a
--     hallucinated verdict cannot be stored at all.
--
--  4. **A shared fact, and a private correction.**  A company does not stop
--     being an agency between job seekers, so the verdict is knowledge base
--     (FR-341) and carries no job-seeker id (FR-344).  A seeker's correction is
--     a different kind of thing and lives in its own table: it takes effect for
--     that seeker immediately, and reaches everybody else only by promotion -
--     an operator's review, or two seekers correcting the same company the same
--     way.  One person's mistake - or one person's grudge against a former
--     employer - must not silently relabel a good company for everyone, with no
--     evidence anyone else can check; the machine's verdict at least carries a
--     quote (Agency_Research_Design.md section 8).
--
-- RK-08 / FR-306: nothing here describes a person.  The correction row points
-- at the job seeker who made it, because it is *their* override until it is
-- promoted, and promotion drops that link (FR-344).  ``note`` is prose about a
-- company; no name, address or e-mail of any individual belongs in it.
--
-- NFR-205: ``evidence``, ``summary``, ``anomalies`` and ``audiences`` hold text
-- that came from an untrusted web page.  They are stored as data, rendered as
-- data, and never as instructions.  ``anomalies`` is where an injection attempt
-- is recorded; a non-empty ``anomalies`` queues the row for review whatever the
-- verdict says.
-- ===========================================================================

-- --- The shared verdict: one row per company -------------------------------
CREATE TABLE company_employer_kind (
    company_id        TEXT PRIMARY KEY REFERENCES company(id) ON DELETE CASCADE,

    -- The verdict itself.  ``cannot_tell`` is an answer, not a gap: it means
    -- the rungs that have run could not establish who employs, and ``reason``
    -- says which of the ways that happens this was.
    kind              TEXT NOT NULL,     -- agency | employer | cannot_tell

    -- What the product does with it (Interim_Agencies_Proposal.md section 4):
    -- direct     no badge, everything runs as today
    -- agency     badge, company sub-score None, no profile/financial/speculative
    -- board      the same, for a job board (ICTJOB) that appears as an employer
    -- unverified "employer type not verified" - shown, never acted on
    employer_role     TEXT NOT NULL DEFAULT 'unverified',

    -- temp_agency | recruitment_selection | job_board | payrolling |
    -- consultancy_or_outsourcing | product | services | public_body | unknown
    service_model     TEXT NOT NULL DEFAULT 'unknown',

    -- Confidence in ``kind``.  0.85 is the bar above which no further rung is
    -- spent on this company (Agency_Research_Design.md section 2).  For a
    -- ``cannot_tell`` row it is deliberately low and nothing scores on it.
    confidence        REAL NOT NULL,

    -- Which rung established it, cheapest first: kb (a verdict already on
    -- record), signals (the detector over this employer's own postings),
    -- registry (a KBO/CH/KvK activity list), eures (the section-O employer
    -- probe), website (the classifier over the company's own pages), manual
    -- (a promoted human correction).
    rung              TEXT NOT NULL,
    -- The technique inside the rung, for the coverage screen and for provenance:
    -- signals | registry_nace | eures_sector | website_llm | correction | import
    method            TEXT NOT NULL,

    -- The additive score and its band, when the rung was the signal detector
    -- (section 2.3).  NULL for a registry or website verdict, which does not
    -- score: certain | probable | possible | unknown | direct_likely.
    tier              TEXT,
    score             REAL,
    postings          INTEGER,           -- n the aggregates were computed over

    -- ``cannot_tell`` only.  The vocabulary is closed on purpose: the product
    -- offers a *different* next step per reason ("add the website", "check in a
    -- browser", "which of these two companies is it?"), so a reason nobody has
    -- written behaviour for must not appear without a migration.
    reason            TEXT,

    -- NFR-402.  JSON list, each item {signal, supports, detail, url, quote,
    -- measured, points, established_at}: the rung, the page, a verbatim quote
    -- verified against that page's text, and when it was established.
    evidence          TEXT NOT NULL DEFAULT '[]',
    -- How the page or register row was tied to this name, so a namesake cannot
    -- be laundered into a verdict: {gate, title_match, jsonld_name,
    -- vat_on_site, country_ok}.  brightplus.com is a Finnish coatings maker.
    identity_evidence TEXT,
    audiences         TEXT,              -- JSON {sells_to_employers, sells_to_candidates}
    summary           TEXT,              -- one sentence, for the badge tooltip
    anomalies         TEXT,              -- JSON list; non-empty rows are reviewed

    -- Which prompt and model produced a website verdict (FR-364, NFR-402).
    prompt_template   TEXT,
    prompt_version    TEXT,
    model             TEXT,
    llm_call_id       TEXT,

    -- FR-343 freshness.  ``established_at`` is when this verdict was first
    -- reached, ``refreshed_at`` when it was last re-confirmed without changing,
    -- ``expires_at`` when it becomes stale (365 days for a registry verdict,
    -- 180 for a website one; NULL never expires, which is what a promoted human
    -- correction is).
    established_at    TEXT NOT NULL,
    refreshed_at      TEXT,
    expires_at        TEXT,
    -- When a ``cannot_tell`` is worth trying again.  NULL means "not on a
    -- clock": either the answer will not change without a human
    -- (namesake_collision, registry_ambiguous) or it is event-driven
    -- (no_domain becomes due the moment a domain is derived or entered).
    retry_after       TEXT,
    attempts          INTEGER NOT NULL DEFAULT 1,

    CHECK (kind IN ('agency', 'employer', 'cannot_tell')),
    CHECK (employer_role IN ('direct', 'agency', 'board', 'unverified')),
    CHECK (service_model IN (
        'temp_agency', 'recruitment_selection', 'job_board', 'payrolling',
        'consultancy_or_outsourcing', 'product', 'services', 'public_body', 'unknown')),
    CHECK (rung IN ('kb', 'signals', 'registry', 'eures', 'website', 'manual')),
    CHECK (tier IS NULL OR tier IN
        ('certain', 'probable', 'possible', 'unknown', 'direct_likely')),
    CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CHECK (attempts >= 1),
    -- A reason belongs to a non-answer, and a non-answer needs one: "employer
    -- type not verified" is only useful to a job seeker with the because.
    CHECK ((kind = 'cannot_tell') = (reason IS NOT NULL)),
    CHECK (reason IS NULL OR reason IN (
        'no_domain', 'unreachable', 'bot_wall', 'js_rendered', 'parked', 'robots',
        'off_domain_redirect', 'namesake_collision', 'registry_ambiguous',
        'ambiguous_self_description', 'no_verifiable_evidence', 'llm_failed',
        -- One state, two spellings: docs/Agency_Research_Design.md section 3.1
        -- writes the JS-only case as ``js_rendered_or_empty``.  Accepted so a
        -- write in that spelling is stored rather than crashing a corpus pass;
        -- the repository folds it onto ``js_rendered`` on the way in and the
        -- coverage screen folds it on the way out, so it is never a second
        -- bucket meaning the same thing.
        'js_rendered_or_empty')),
    -- NFR-402, and the rule that makes a hallucinated verdict unstorable: a
    -- decided verdict without evidence cannot be written down.
    CHECK (kind = 'cannot_tell' OR (evidence IS NOT NULL AND trim(evidence) NOT IN ('', '[]')))
);

-- The list and the gates ask "which companies are agencies", the coverage
-- screen asks "how many of each, at what confidence".
CREATE INDEX idx_employer_kind_kind ON company_employer_kind(kind, confidence);
-- ``financial.campaign_companies``, ``speculative.candidate_companies`` and the
-- competitor pass all exclude agency/board rows; that is this index.
CREATE INDEX idx_employer_kind_role ON company_employer_kind(employer_role);
-- FR-343: what has gone stale, and which non-answers are due for another rung.
CREATE INDEX idx_employer_kind_expiry ON company_employer_kind(expires_at);
CREATE INDEX idx_employer_kind_retry
    ON company_employer_kind(retry_after) WHERE kind = 'cannot_tell';
-- Section 7.5: "30% unverified" has to be readable as "mostly no domain"
-- rather than "the classifier is unsure".
CREATE INDEX idx_employer_kind_reason
    ON company_employer_kind(reason) WHERE kind = 'cannot_tell';

-- NOTE on the opportunity list (NFR-502, FR-282): no index is needed for the
-- badge.  ``company_id`` is this table's primary key, so the join the ranked
-- list and the Apply Browser add is a rowid lookup per row already on screen -
-- which is what lets the badge cost no extra query.

-- --- The role never contradicts the verdict --------------------------------
-- Four slices write this table and two of them build their row from the shape
-- in ``Agency_Research_Design.md`` section 6, which has no ``employer_role``:
-- their insert would take the column default and a registered temp agency
-- would sit in the table as an agency that the gates - which read
-- ``employer_role`` - do not exclude, and that the list does not badge.  The
-- role is not derivable in general (an ``unknown`` band is a ``cannot_tell``
-- the product still treats as direct, and that distinction is most of the
-- corpus), so it stays a stored column; these two triggers only repair the
-- cases where it plainly contradicts the verdict beside it.
CREATE TRIGGER employer_kind_role_follows_insert
AFTER INSERT ON company_employer_kind
WHEN NEW.kind <> 'cannot_tell' AND NEW.employer_role = 'unverified'
BEGIN
    UPDATE company_employer_kind
       SET employer_role = CASE
               WHEN NEW.kind = 'agency' AND NEW.service_model = 'job_board' THEN 'board'
               WHEN NEW.kind = 'agency' THEN 'agency'
               ELSE 'direct'
           END
     WHERE company_id = NEW.company_id;
END;

-- The same on the way through a replacement, plus the other direction: a row
-- that becomes a non-answer must stop being badged as an agency.  A
-- ``cannot_tell`` already sitting at ``direct`` is left alone - that is the
-- ``unknown`` band, which is deliberately not badged.
CREATE TRIGGER employer_kind_role_follows_update
AFTER UPDATE OF kind ON company_employer_kind
WHEN (NEW.kind <> 'cannot_tell' AND NEW.employer_role = 'unverified')
  OR (NEW.kind = 'cannot_tell' AND NEW.employer_role IN ('agency', 'board'))
BEGIN
    UPDATE company_employer_kind
       SET employer_role = CASE
               WHEN NEW.kind = 'cannot_tell' THEN 'unverified'
               WHEN NEW.kind = 'agency' AND NEW.service_model = 'job_board' THEN 'board'
               WHEN NEW.kind = 'agency' THEN 'agency'
               ELSE 'direct'
           END
     WHERE company_id = NEW.company_id;
END;

-- --- The human correction: private by default, shared by promotion ---------
CREATE TABLE employer_kind_correction (
    id            TEXT PRIMARY KEY,
    company_id    TEXT NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    -- The seeker whose correction this is - and NULL on the promoted, shared
    -- copy, because a shared record must not point back at the person whose
    -- campaign produced it (FR-344).
    job_seeker_id TEXT REFERENCES job_seeker(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,                    -- agency | employer
    -- Required, like a rejection reason (FR-285): a correction with no stated
    -- because cannot be reviewed, cannot be promoted, and is not evidence.
    note          TEXT NOT NULL,
    evidence_url  TEXT,                             -- optional rung-4 evidence item
    scope         TEXT NOT NULL DEFAULT 'private',  -- private | shared
    promoted_by   TEXT,                             -- operator id, or 'consensus'
    promoted_at   TEXT,
    created_at    TEXT NOT NULL,

    CHECK (kind IN ('agency', 'employer')),
    CHECK (scope IN ('private', 'shared')),
    CHECK (length(trim(note)) > 0),
    -- The two halves of the sharing rule, in the schema: a private correction
    -- belongs to exactly one seeker, and a shared one belongs to nobody.
    CHECK ((scope = 'private' AND job_seeker_id IS NOT NULL)
        OR (scope = 'shared'  AND job_seeker_id IS NULL)),
    -- One correction per seeker per company; the index this creates is also
    -- what the list query's correlated look-up uses.
    UNIQUE (company_id, job_seeker_id)
);
-- At most one shared correction per company (SQLite treats NULLs in a UNIQUE
-- as distinct, so the table constraint above does not cover the promoted row).
CREATE UNIQUE INDEX idx_employer_kind_correction_shared
    ON employer_kind_correction(company_id) WHERE job_seeker_id IS NULL;
-- The operator's review queue: private corrections waiting for promotion.
CREATE INDEX idx_employer_kind_correction_scope
    ON employer_kind_correction(scope, created_at);
