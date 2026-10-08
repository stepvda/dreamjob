-- ===========================================================================
-- Applying for an internship (Apply Browser)
--
-- One row per job seeker: whether the applications generated from the Apply
-- screen ask for an internship, and on what terms.  The generator appends the
-- terms to the application e-mail as a fixed sentence
-- (documents/internship.py), so what the recipient reads is exactly what the
-- job seeker chose here.
--
--   internship        0/1 - off means the e-mail is an ordinary application
--   duration_months   1..24, NULL when the job seeker did not say
--   start_month       'YYYY-MM', NULL when the job seeker did not say
--   pay               paid | unpaid | either
-- ===========================================================================

CREATE TABLE apply_preference (
    job_seeker_id    TEXT PRIMARY KEY REFERENCES job_seeker(id) ON DELETE CASCADE,
    internship       INTEGER NOT NULL DEFAULT 0,
    duration_months  INTEGER CHECK (duration_months IS NULL OR duration_months BETWEEN 1 AND 24),
    start_month      TEXT,
    pay              TEXT NOT NULL DEFAULT 'either' CHECK (pay IN ('paid', 'unpaid', 'either')),
    updated_at       TEXT NOT NULL
);
