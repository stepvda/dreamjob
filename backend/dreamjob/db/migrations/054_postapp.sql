-- ===========================================================================
-- Post-application pipeline (FR-421..FR-425, FR-444, NFR-305)
--
-- 001_initial.sql gives this area four tables - pipeline_card,
-- mock_interview_session, negotiation_brief and incoming_reply.  What it does
-- not give is the paperwork around the human decision, and NFR-305 makes that
-- paperwork the point: nothing here may act on the job seeker's behalf.
--
--  1. FR-422 drafts a reply "for the job seeker to review and send".  A draft
--     is therefore an object with a life of its own - written, approved,
--     sent - and it has to carry the threading headers that keep it in the
--     same conversation.  ``incoming_reply.draft_response`` is one text
--     column; ``reply_draft`` is the reviewable artefact, and its status
--     column is where "never auto-send" is enforced rather than promised.
--
--  2. FR-423 needs somewhere to put the slots proposed in prose, the
--     availability answer from the calendar, the slot recommended back, the
--     confirmation draft and - after approval - the identifier of the entry
--     that was created.  ``interview_appointment`` is one row per scheduling
--     conversation, and ``calendar_account`` holds the OAuth grant that made
--     it possible (encrypted, scoped, revocable - NFR-204's shape, applied to
--     the calendar rather than the mailbox).
--
--  3. FR-421 says transitions are automatic "where possible and manually
--     otherwise".  Which of the two moved a card is the difference between a
--     board the seeker trusts and one they have to re-check, so every
--     transition is recorded with its trigger in ``pipeline_card_event``.
--
--  4. FR-425 learns from outcomes and must report "the learned effects
--     transparently".  A learned effect that cannot be shown with the sample
--     it rests on is not transparent, so the analysis is stored whole -
--     effects, sample sizes, and which of them were actually applied.
-- ===========================================================================

-- --- FR-421: one card per sent application, and its history ----------------
-- Cards are derived from application_package; deriving twice must not make a
-- second card, so the package is the key.
CREATE UNIQUE INDEX idx_card_package
    ON pipeline_card(application_package_id) WHERE application_package_id IS NOT NULL;
CREATE INDEX idx_card_due ON pipeline_card(job_seeker_id, next_action_due);

ALTER TABLE pipeline_card ADD COLUMN stage_changed_at TEXT;
ALTER TABLE pipeline_card ADD COLUMN outcome_at TEXT;
ALTER TABLE pipeline_card ADD COLUMN outcome_detail TEXT;
-- FR-425 records the outcome as an event as well as a state: "replied" is
-- true of a card that later reached "offer", and the learning pass needs both.
ALTER TABLE pipeline_card ADD COLUMN reached_replied_at TEXT;
ALTER TABLE pipeline_card ADD COLUMN reached_interview_at TEXT;
ALTER TABLE pipeline_card ADD COLUMN reached_offer_at TEXT;

CREATE TABLE pipeline_card_event (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    pipeline_card_id    TEXT NOT NULL REFERENCES pipeline_card(id) ON DELETE CASCADE,
    from_stage          TEXT,
    to_stage            TEXT,
    -- FR-421: reply_detection is the automatic path, user the manual one;
    -- system covers card creation and the no-response sweep.
    trigger             TEXT NOT NULL DEFAULT 'user',  -- user|reply_detection|system
    incoming_reply_id   TEXT REFERENCES incoming_reply(id) ON DELETE SET NULL,
    note                TEXT,
    detail              TEXT,                          -- JSON
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_card_event_card ON pipeline_card_event(pipeline_card_id, created_at);

-- --- FR-422 / NFR-305: the reviewable reply draft --------------------------
CREATE TABLE reply_draft (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    incoming_reply_id   TEXT REFERENCES incoming_reply(id) ON DELETE CASCADE,
    dispatch_id         TEXT REFERENCES dispatch(id) ON DELETE SET NULL,
    pipeline_card_id    TEXT REFERENCES pipeline_card(id) ON DELETE SET NULL,
    kind                TEXT NOT NULL DEFAULT 'reply',  -- reply|follow_up|confirmation
    classification      TEXT,
    language            TEXT NOT NULL DEFAULT 'en',
    subject             TEXT,
    body                TEXT NOT NULL,
    -- Threading, so the answer lands in the same conversation (FR-422).
    thread_id           TEXT,
    in_reply_to         TEXT,
    references_header   TEXT,
    to_address          TEXT,
    attachments         TEXT,                           -- JSON list of paths
    -- NFR-305: 'draft' until a person approves; nothing sends from 'draft'.
    status              TEXT NOT NULL DEFAULT 'draft',  -- draft|edited|approved|sent|discarded
    generated_by        TEXT,                           -- llm|template
    approved_at         TEXT,
    approved_by         TEXT,
    sent_at             TEXT,
    dispatch_message_id TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX idx_reply_draft_seeker ON reply_draft(job_seeker_id, status);
CREATE INDEX idx_reply_draft_reply ON reply_draft(incoming_reply_id);

-- --- FR-423: connected calendar and the scheduling conversation ------------
CREATE TABLE calendar_account (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    provider            TEXT NOT NULL,                  -- google|microsoft
    address             TEXT NOT NULL,
    calendar_id         TEXT NOT NULL DEFAULT 'primary',
    timezone            TEXT NOT NULL DEFAULT 'Europe/Brussels',
    -- NFR-204's rule applied to the calendar grant: encrypted at rest, the
    -- granted scopes recorded, revocable by deleting the row.
    credentials_enc     BLOB,
    scopes              TEXT,
    token_expires_at    TEXT,
    is_active           INTEGER NOT NULL DEFAULT 1,
    last_error          TEXT,
    connected_at        TEXT NOT NULL,
    UNIQUE (job_seeker_id, provider, address)
);

CREATE TABLE interview_appointment (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    opportunity_id      TEXT REFERENCES opportunity(id) ON DELETE CASCADE,
    pipeline_card_id    TEXT REFERENCES pipeline_card(id) ON DELETE SET NULL,
    incoming_reply_id   TEXT REFERENCES incoming_reply(id) ON DELETE SET NULL,
    -- JSON: [{start, end, timezone, phrase, confidence, source}] (FR-423)
    proposed_slots      TEXT,
    availability        TEXT,                           -- JSON: verdict per slot
    recommended         TEXT,                           -- JSON: the slots proposed back
    calendar_checked    INTEGER NOT NULL DEFAULT 0,     -- 0 when no calendar was connected
    chosen_start        TEXT,
    chosen_end          TEXT,
    timezone            TEXT,
    location            TEXT,
    title               TEXT,
    -- NFR-305: proposed until the seeker approves; only then is an entry made.
    status              TEXT NOT NULL DEFAULT 'proposed', -- proposed|confirmed|declined|failed
    calendar_account_id TEXT REFERENCES calendar_account(id) ON DELETE SET NULL,
    calendar_event_id   TEXT,
    calendar_event_url  TEXT,
    ics_path            TEXT,                           -- always written, attachment included
    briefing_path       TEXT,                           -- FR-329 briefing attached to the entry
    briefing_attached   INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX idx_appointment_seeker ON interview_appointment(job_seeker_id, status);

-- --- FR-424: sessions are repeatable, so a session knows its round ---------
ALTER TABLE mock_interview_session ADD COLUMN language TEXT NOT NULL DEFAULT 'en';
ALTER TABLE mock_interview_session ADD COLUMN round_number INTEGER NOT NULL DEFAULT 1;
ALTER TABLE mock_interview_session ADD COLUMN focus TEXT;          -- role|behavioural|mixed
ALTER TABLE mock_interview_session ADD COLUMN question_plan TEXT;  -- JSON
ALTER TABLE mock_interview_session ADD COLUMN summary TEXT;
ALTER TABLE mock_interview_session ADD COLUMN generated_by TEXT;   -- llm|template
ALTER TABLE mock_interview_session ADD COLUMN finished_at TEXT;

-- --- FR-444: the negotiation brief and the figures behind it ---------------
ALTER TABLE negotiation_brief ADD COLUMN language TEXT NOT NULL DEFAULT 'en';
ALTER TABLE negotiation_brief ADD COLUMN stage TEXT;               -- interview|offer
ALTER TABLE negotiation_brief ADD COLUMN currency TEXT NOT NULL DEFAULT 'EUR';
ALTER TABLE negotiation_brief ADD COLUMN ask_min REAL;
ALTER TABLE negotiation_brief ADD COLUMN ask_max REAL;
ALTER TABLE negotiation_brief ADD COLUMN walk_away REAL;
-- JSON: ability to pay, personnel cost per FTE, market range, directives -
-- the four inputs FR-444 names, kept so the brief can be re-read years later.
ALTER TABLE negotiation_brief ADD COLUMN inputs TEXT;
ALTER TABLE negotiation_brief ADD COLUMN confidence REAL;
ALTER TABLE negotiation_brief ADD COLUMN updated_at TEXT;
CREATE INDEX idx_negotiation_seeker ON negotiation_brief(job_seeker_id, opportunity_id);

-- --- FR-425: what was learned, on how many applications --------------------
CREATE TABLE outcome_learning (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    sample_size         INTEGER NOT NULL DEFAULT 0,
    -- JSON: [{variable, value, n, outcome_rate, baseline, lift, interval,
    --         evidence}] - never a claim of significance the n cannot carry.
    effects             TEXT,
    -- JSON: the generation defaults derived from those effects (FR-425)
    defaults            TEXT,
    -- JSON: the scoring-weight adjustment, and which effects justified it
    weight_adjustment   TEXT,
    applied             INTEGER NOT NULL DEFAULT 0,
    applied_at          TEXT,
    notes               TEXT,
    computed_at         TEXT NOT NULL,
    UNIQUE (job_seeker_id)
);
