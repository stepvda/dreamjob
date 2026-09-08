-- ===========================================================================
-- Mail dispatch, OAuth mailbox connection and reply/bounce detection
-- (FR-325, FR-326, FR-327, NFR-204, NFR-302, NFR-702, RK-05)
--
-- Five additions to 001_initial.sql.  Each exists because the requirement
-- cannot be met with the columns already there:
--
--  1. FR-325 - send windows are expressed in the *recipient's* time zone, so a
--     message that arrives at the dispatcher outside that window is held, not
--     dropped.  ``scheduled_for`` is when the window next opens (in UTC) and
--     ``recipient_timezone`` records the zone the decision was made in, which
--     is what makes a queued send explainable afterwards.
--
--  2. FR-326 - threading.  A reply is matched on In-Reply-To / References, so
--     the headers actually sent have to be stored next to the message id, and
--     ``mail_account_id`` records which mailbox the message left from.
--
--  3. FR-327 - the follow-up is a second dispatch in the same thread.  It
--     points back at its parent, carries its own generated subject and body
--     until the job seeker approves it, and is distinguished from the original
--     by ``kind``.
--
--  4. NFR-204 - the Gmail authorisation-code flow needs somewhere to keep the
--     one-time ``state``/PKCE verifier between the redirect out and the
--     loopback redirect back; it is short-lived and must not be guessable.
--
--  5. FR-326 - polling and webhooks both need to be idempotent.
--     ``mail_poll_state`` is the IMAP/Gmail cursor per mailbox and
--     ``mail_webhook_event`` de-duplicates Resend deliveries, which are
--     re-sent until they are acknowledged.
-- ===========================================================================

-- --- FR-325 / FR-326 / FR-327: the dispatch record -------------------------
ALTER TABLE dispatch ADD COLUMN mail_account_id     TEXT;
ALTER TABLE dispatch ADD COLUMN kind                TEXT NOT NULL DEFAULT 'application';
ALTER TABLE dispatch ADD COLUMN parent_dispatch_id  TEXT;
ALTER TABLE dispatch ADD COLUMN in_reply_to         TEXT;
ALTER TABLE dispatch ADD COLUMN references_header   TEXT;
ALTER TABLE dispatch ADD COLUMN scheduled_for       TEXT;
ALTER TABLE dispatch ADD COLUMN recipient_timezone  TEXT;
ALTER TABLE dispatch ADD COLUMN follow_up_subject   TEXT;
ALTER TABLE dispatch ADD COLUMN follow_up_draft     TEXT;
-- NFR-702: the audit trail is authoritative, but the approver is repeated
-- here so the send log answers "who approved this" without a join.
ALTER TABLE dispatch ADD COLUMN approved_by         TEXT;
ALTER TABLE dispatch ADD COLUMN attempts            INTEGER NOT NULL DEFAULT 0;
ALTER TABLE dispatch ADD COLUMN last_error          TEXT;

CREATE INDEX idx_dispatch_message   ON dispatch(message_id);
CREATE INDEX idx_dispatch_recipient ON dispatch(recipient_email, sent_at);
CREATE INDEX idx_dispatch_schedule  ON dispatch(delivery_status, scheduled_for);
CREATE INDEX idx_dispatch_followup  ON dispatch(follow_up_due_at, follow_up_sent_at);
CREATE INDEX idx_dispatch_parent    ON dispatch(parent_dispatch_id);

-- --- NFR-204: the OAuth handshake ------------------------------------------
-- Rows live for minutes.  The state is the CSRF token Google echoes back on
-- the loopback redirect; the verifier is the PKCE secret that never leaves
-- this machine.
CREATE TABLE mail_oauth_state (
    state           TEXT PRIMARY KEY,
    job_seeker_id   TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    backend         TEXT NOT NULL,
    code_verifier   TEXT NOT NULL,
    redirect_uri    TEXT NOT NULL,
    scopes          TEXT,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL
);
CREATE INDEX idx_oauth_state_seeker ON mail_oauth_state(job_seeker_id);

-- --- FR-326: where the last poll stopped -----------------------------------
CREATE TABLE mail_poll_state (
    mail_account_id TEXT PRIMARY KEY REFERENCES mail_account(id) ON DELETE CASCADE,
    folder          TEXT NOT NULL DEFAULT 'INBOX',
    uid_validity    TEXT,
    last_uid        INTEGER,
    last_history_id TEXT,
    last_polled_at  TEXT,
    last_error      TEXT,
    messages_seen   INTEGER NOT NULL DEFAULT 0
);

-- --- FR-326: Resend has no IMAP, so delivery news arrives as webhooks -------
-- The provider retries until it gets a 2xx, so the delivery id is the primary
-- key and a replay is a no-op rather than a second bounce.
CREATE TABLE mail_webhook_event (
    id              TEXT PRIMARY KEY,            -- provider delivery id (svix-id)
    provider        TEXT NOT NULL,
    event_type      TEXT NOT NULL,               -- email.delivered|email.bounced|email.complained|...
    provider_message_id TEXT,
    recipient       TEXT,
    dispatch_id     TEXT REFERENCES dispatch(id) ON DELETE SET NULL,
    payload         TEXT,                        -- JSON as received
    received_at     TEXT NOT NULL,
    processed_at    TEXT
);
CREATE INDEX idx_webhook_dispatch ON mail_webhook_event(dispatch_id, received_at);
