-- ===========================================================================
-- The per-employer agency tag, product side (FR-143, FR-281, FR-282, FR-330,
-- FR-383, NFR-402, RK-08)
--
-- docs/Interim_Agencies_Proposal.md measures that 17-20% of real corpus rows -
-- 33-37% of the named Belgian EURES slice, 80-92% of Actiris - are posted by
-- an interim, staffing or selection agency for an employer the advert does not
-- name, and that in 83% of those rows the end client cannot be recovered at
-- all.  Everything the product does well is computed about the employer: the
-- profile, five years of accounts, ability to pay, values match, the letter
-- about why you want to work *there*.  Against an agency row all of it is
-- authoritative and wrong.
--
-- The verdict itself is migration 110's (``company_employer_kind``), its
-- signals 111's and the registry rung 112's.  This slice consumes them, and
-- owns exactly one table: the journal of the background resolution pass.
--
-- **Why a journal, when 111 already records one row per (company, rung).**
-- That table answers "what was tried for this company".  It cannot answer the
-- question the coverage screen actually asks, which is "when did we last sweep
-- the corpus, how far did that sweep get, and did it stop early".  Without
-- that, a coverage figure of 62% is read as "38% are direct employers" rather
-- than "38% have never been looked at" - which is precisely the inference this
-- whole design exists to stop anyone making by accident.  The columns are the
-- fields of ``EmployerResolutionReport`` verbatim, so a finished pass is
-- written down without a translation layer.
--
-- RK-08 / FR-344: nothing here stores a person.  ``job_seeker_id`` records who
-- *asked* for a pass, never who a verdict is about; the verdict rows carry no
-- seeker at all, because a company does not stop being an agency between job
-- seekers.  Deleting the seeker sets it NULL rather than deleting the journal:
-- the sweep happened, and the knowledge base it wrote is shared.
-- ===========================================================================

CREATE TABLE employer_resolve_run (
    id              TEXT PRIMARY KEY,
    -- The FR-185 background job this pass ran as, when it ran as one; NULL for
    -- a synchronous pass or a dry run.
    job_run_id      TEXT REFERENCES job_run(id) ON DELETE SET NULL,
    job_seeker_id   TEXT REFERENCES job_seeker(id) ON DELETE SET NULL,

    requested       INTEGER NOT NULL DEFAULT 0,   -- companies the pass took on
    visited         INTEGER NOT NULL DEFAULT 0,   -- companies the ladder walked
    resolved        INTEGER NOT NULL DEFAULT 0,   -- a decided verdict was written
    -- An honest refusal, written down with its reason.  Counted separately
    -- from ``failed`` on purpose: a cannot_tell is an answer the product shows
    -- and offers a next rung for; a failure is a request that broke.
    cannot_tell     INTEGER NOT NULL DEFAULT 0,
    failed          INTEGER NOT NULL DEFAULT 0,
    reused          INTEGER NOT NULL DEFAULT 0,   -- already fresh, no request spent
    -- NFR-205: verdicts whose ``anomalies`` are non-empty - which is where the
    -- website rung records an attempted prompt injection - are queued for
    -- review whatever they concluded.  The count travels with the pass so the
    -- operator sees it without hunting for it.
    needs_review    INTEGER NOT NULL DEFAULT 0,
    -- How many vacancy rows the decided verdicts cover.  Employers and rows
    -- are different denominators and only the second says how much of a ranked
    -- list is still unaccounted for: eleven employers hold 515 of the corpus's
    -- vacancies.
    vacancies_covered INTEGER NOT NULL DEFAULT 0,

    by_kind         TEXT,                          -- JSON {agency, employer, cannot_tell}
    by_rung         TEXT,                          -- JSON {kb, registry, eures, website, manual}
    by_reason       TEXT,                          -- JSON, cannot_tell only

    status          TEXT NOT NULL DEFAULT 'running',   -- running | done | failed | cancelled
    reason          TEXT,                              -- why it stopped, when it stopped early
    started_at      TEXT NOT NULL,
    finished_at     TEXT,

    CHECK (status IN ('running', 'done', 'failed', 'cancelled')),
    CHECK (requested >= 0 AND visited >= 0 AND resolved >= 0),
    -- A finished pass has an end; a running one does not claim to.
    CHECK ((status = 'running') = (finished_at IS NULL))
);

-- "When did we last sweep, and what came back" is one row, newest first.
CREATE INDEX idx_employer_resolve_run_started ON employer_resolve_run(started_at DESC);
-- The job screen joins the other way: this pass, for this job.
CREATE INDEX idx_employer_resolve_run_job ON employer_resolve_run(job_run_id);
