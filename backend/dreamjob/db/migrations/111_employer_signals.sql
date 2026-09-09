-- ===========================================================================
-- 111: what the employer-kind ladder tried, and what the free signals suspect
-- (FR-341, FR-342, FR-344, NFR-402, RK-08)
--
-- Migration 110 holds the *verdict* - one row per company saying agency,
-- employer or cannot_tell, with the evidence that established it.  This
-- migration holds the two things the ladder in
-- ``pipeline/employer_resolver.py`` needs in order to produce that verdict
-- honestly, neither of which belongs on the verdict row:
--
--  1. ``employer_resolution_signal`` - the rung-0b snapshot.  The free
--     signals (name lexicon, client phrases, offering codes, the detector's
--     band) prove nothing on their own: docs/Agency_Research_Design.md
--     section 2 gives rung 0b "Proves: Nothing.  Sets priority and a
--     ``suspected`` hint".  They are still worth storing, for two reasons the
--     verdict row cannot serve.  They order the queue, so the 56-vacancy
--     agency is resolved before the one-vacancy employer and the campaign's
--     first pass covers the rows that matter (docs/Interim_Agencies_Proposal.md
--     section 1: 210 vacancies sit at 34 reachable agencies).  And when every
--     rung below them hands down, they are the *only* thing the product can
--     honestly show next to "employer type not verified": "the adverts look
--     like an agency's, but we could not establish it" is information; a
--     verdict would be a guess.  A row here is therefore never a verdict and
--     the resolver never reads ``hint`` as one.
--
--  2. ``employer_resolution_attempt`` - one row per (company, rung) the
--     ladder actually walked.  Three jobs:
--
--     * NFR-402.  "Every verdict carries its evidence" includes the rungs
--       that did *not* answer: a job seeker who disagrees with
--       ``cannot_tell`` is owed "the register was searched and is silent; the
--       site could not be read: it needs a browser", not silence.  The
--       verdict's own ``evidence`` JSON carries the same trail in the shape
--       the screen renders; this table is the ladder's copy of it, queryable
--       by rung for the coverage panel (section 7.5 of the research note).
--     * Retry by reason, not by clock (research note section 4).  A
--       ``bot_wall`` is worth another look in 90 days; an
--       ``off_domain_redirect`` or a ``registry_ambiguous`` never is, because
--       the answer will not change without a human.  ``retry_after NULL``
--       with a reason is the ladder saying "not again automatically", and it
--       is what stops a monthly refresh from re-spending 32 minutes of KBO
--       requests on the same silent names.
--     * Resumability (NFR-401).  A pass interrupted mid-corpus resumes at the
--       next company, and a rung that already answered for a company is not
--       re-walked inside the same window.
--
-- RK-08 / FR-344: neither table holds anything about a person, and neither
-- holds a ``job_seeker_id``.  A company does not stop being an agency because
-- a different job seeker is looking at it, so this is knowledge-base state
-- (FR-341), shared and established once, never campaign state.  The one
-- per-seeker artefact in this design - a job seeker's "this is wrong"
-- correction - is deliberately somewhere else (research note section 8).
-- ===========================================================================

-- --- rung 0b: the free signals, as priority and as a hint ------------------
-- One row per company, rewritten in place.  ``priority`` is the only number
-- the queue reads; ``hint`` is what the signals suspect and is shown, if at
-- all, as a suspicion.  ``signals`` is the JSON list of what fired, with its
-- measured share, so the hint can be explained in words rather than asserted.
CREATE TABLE employer_resolution_signal (
    company_id      TEXT PRIMARY KEY REFERENCES company(id) ON DELETE CASCADE,
    vacancy_count   INTEGER NOT NULL DEFAULT 0,
    -- suspected_agency | suspected_direct | none.  Never 'agency'/'employer':
    -- the vocabulary is deliberately different from the verdict's so that no
    -- caller can mistake a hint for a kind.
    hint            TEXT NOT NULL DEFAULT 'none',
    -- Queue order only.  Higher is resolved sooner.  Vacancy count dominates
    -- it, because that is the number of rows a wrong answer damages.
    priority        REAL NOT NULL DEFAULT 0,
    -- The detector's band and score when it has run (certain|probable|
    -- possible|unknown|direct_likely).  Read for priority and for the
    -- "what the postings suggest" sentence; never as the verdict, because a
    -- score is not a quote (NFR-402).
    tier            TEXT,
    score           REAL,
    -- JSON list, the same evidence shape the verdict carries:
    -- [{signal, supports, detail, url, quote, measured, points}].
    signals         TEXT NOT NULL DEFAULT '[]',
    computed_at     TEXT NOT NULL,

    -- The vocabularies are closed here for the same reason they are closed on
    -- the verdict: a hint spelled 'agency' would be a verdict by accident.
    CHECK (hint IN ('suspected_agency', 'suspected_direct', 'none')),
    CHECK (tier IS NULL OR tier IN
        ('certain', 'probable', 'possible', 'unknown', 'direct_likely')),
    CHECK (priority >= 0)
);
-- The queue: the employers that matter most, first.
CREATE INDEX idx_employer_signal_priority
    ON employer_resolution_signal(priority DESC, vacancy_count DESC);

-- --- the ladder's own trail ------------------------------------------------
-- Natural key, like fetch_ledger's: exactly one row per (company, rung), and
-- walking a rung again updates it rather than growing history nobody reads.
-- ``attempts`` counts how often it was walked, which is what lets the
-- interface say "searched three times, still nothing" (the phrasing
-- apply_contact_resolution already uses) instead of presenting every re-run
-- as the first.
CREATE TABLE employer_resolution_attempt (
    company_id      TEXT NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    -- The rung vocabulary is migration 110's, verbatim - kb | signals |
    -- registry | eures | website | manual - so that a trail row and the
    -- verdict row it explains cannot drift apart.  ``position`` is where the
    -- rung sits in the ladder, which is what "cheapest first" means and what
    -- the coverage screen orders by.
    rung            TEXT NOT NULL,
    position        INTEGER NOT NULL,
    method          TEXT NOT NULL,             -- signals|registry_nace|eures_sector|website_llm|correction
    -- decided | handed_down | unavailable | failed.  'unavailable' is the
    -- honest answer when a rung is not wired into this build (no registry key,
    -- the website rung not shipped): it is not a failure and it is not
    -- evidence of anything about the company.
    outcome         TEXT NOT NULL,
    kind            TEXT,                      -- what this rung concluded, if anything
    confidence      REAL,
    reason          TEXT,                      -- why it handed down, in the reason vocabulary
    detail          TEXT,                      -- JSON: whatever the rung wants the trail to keep
    duration_ms     INTEGER,
    attempts        INTEGER NOT NULL DEFAULT 1,
    attempted_at    TEXT NOT NULL,
    -- NULL means "not automatically": either this rung answered, or its
    -- reason is one a clock cannot fix (research note section 4).
    retry_after     TEXT,

    PRIMARY KEY (company_id, rung),
    CHECK (rung IN ('kb', 'signals', 'registry', 'eures', 'website', 'manual')),
    CHECK (outcome IN ('decided', 'handed_down', 'unavailable', 'failed')),
    CHECK (kind IS NULL OR kind IN ('agency', 'employer', 'cannot_tell')),
    CHECK (attempts >= 1)
);
-- "Which companies are due another attempt?" is the refresh job's one scan.
CREATE INDEX idx_employer_attempt_retry
    ON employer_resolution_attempt(retry_after) WHERE retry_after IS NOT NULL;
-- "How far did the ladder get, by rung?" is the coverage panel's group-by.
CREATE INDEX idx_employer_attempt_rung
    ON employer_resolution_attempt(rung, outcome);

-- A note on what is deliberately NOT here.  There is no ``verdict`` column and
-- no second copy of ``kind``: this table says what was *tried*, migration
-- 110's ``company_employer_kind`` says what is *true*, and two tables that
-- both claim to hold the verdict is how a product ends up showing two
-- different badges for one company.  The ``kind`` column above is the rung's
-- own answer at the moment it answered - trail, not truth - and the resolver
-- reads it only to explain a disagreement between rungs.
