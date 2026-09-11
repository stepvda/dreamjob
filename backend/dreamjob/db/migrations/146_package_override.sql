-- ===========================================================================
-- A consistency override the send paths can actually see (FR-322, FR-324)
--
-- Approval treated a failed FR-322 consistency check as overridable: the job
-- seeker could approve it with a recorded reason.  Both send paths then refused
-- the package outright on ``consistency_status = 'fail'``, with no override at
-- all.  The recorded reason was written only into the audit trail, so an
-- approval built on it bought a package that could never be sent - an override
-- the product offered and could not honour (E2E_1500, section 8.7).
--
-- One nullable column carries the human decision from approval to dispatch.
-- It is set only when the approver supplied a reason, and cleared whenever the
-- package is regenerated, exactly as approval itself is - a fresh CV is a fresh
-- check, and the old override does not survive it.
-- ===========================================================================

ALTER TABLE application_package ADD COLUMN consistency_override TEXT;
