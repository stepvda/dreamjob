"""Accounts, sessions, MFA and consent (NFR-202, NFR-201, FR-101, CR-410, CR-401, FR-126).

This module holds the authentication *policy*; the SQL lives in
``db.repositories.seekers`` and the primitives in ``security.crypto``.

NFR-202 in practice:
  * passwords are Argon2id hashes with a strength rule applied before hashing;
  * a TOTP second factor (pyotp) can be enrolled, and once activated a login
    without a valid code is refused;
  * only the HMAC of a session token is stored, and every session is bound to
    the client that created it, which ``api.deps.current_seeker`` re-checks on
    each request.

NFR-201: the TOTP secret is sealed with a key derived for this job seeker, so
it is unreadable without the master key and dies with the account (FR-108).

CR-410: profile data may not leave the machine for DeepSeek before the job
seeker has consented.  Other slices call :func:`has_consent` or
:func:`require_consent` before building a prompt out of profile data.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyotp

from dreamjob.config import get_settings
from dreamjob.db.connection import utcnow
from dreamjob.db.repositories import seekers as repo
from dreamjob.security import crypto
from dreamjob.security.audit import record_audit

log = logging.getLogger(__name__)

SESSION_TTL_HOURS = 12
TOTP_ISSUER = "Dream Job"
MIN_PASSWORD_LENGTH = 9

# The three decisions the job seeker is asked to make, with the text that is
# recorded alongside the answer so a later reader knows what was agreed to.
CONSENT_KINDS: dict[str, str] = {
    # CR-410: transfer of profile data to a provider outside the EU.
    "llm_transfer": (
        "Profile, campaign and vacancy text is sent to the DeepSeek API, which processes it "
        "outside the European Union, to extract, score and generate content. Fields marked "
        "'do not disclose' (FR-106) and special categories of personal data (FR-127) are "
        "removed before any prompt is sent."
    ),
    # CR-401: LinkedIn's user agreement prohibits automated access.
    "linkedin_automation": (
        "LinkedIn's user agreement prohibits automated access and scraping, including through "
        "a logged-in session. Using the browser automation against LinkedIn may lead to "
        "restriction or loss of the account. The risk is the account holder's."
    ),
    # FR-126: online enrichment is optional.
    "enrichment": (
        "Publicly available pages about the job seeker are searched and read to enrich the "
        "composite profile. Without this consent the composite profile is built from "
        "user-supplied data only."
    ),
}

_COMMON_PASSWORDS = {
    "password", "passw0rd", "123456789012", "qwertyuiop12", "letmein12345",
    "dreamjob1234", "administrator", "welcome12345",
}


def _verify_totp(secret: str, code: str | None) -> bool:
    """Accept a code from the previous or next window; phone clocks drift."""
    if not code:
        return False
    return bool(pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1))


class AuthError(RuntimeError):
    """Base class for authentication failures."""


class InvalidCredentials(AuthError):
    pass


class MFARequired(AuthError):
    """A valid TOTP code is needed to finish this login (NFR-202)."""


class WeakPassword(AuthError):
    pass


class MFAUnavailable(AuthError):
    """MFA needs the master key; encryption at rest is not negotiable (NFR-201)."""


class ConsentRequired(RuntimeError):
    """Raised when an action needs a consent the job seeker has not given."""

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(
            f"Consent {kind!r} has not been given. "
            f"{CONSENT_KINDS.get(kind, '')} Record it via POST /api/auth/consent."
        )


# ---------------------------------------------------------------------------
# Passwords (NFR-202)
# ---------------------------------------------------------------------------


def validate_password_strength(password: str) -> None:
    """Reject passwords that would make the MFA requirement the only defence."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if password.lower() in _COMMON_PASSWORDS:
        raise WeakPassword("Password is too common")
    classes = sum(
        bool(re.search(pattern, password))
        for pattern in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]")
    )
    if classes < 3:
        raise WeakPassword(
            "Password must combine at least three of: lower case, upper case, digits, symbols"
        )


# ---------------------------------------------------------------------------
# Registration and login (FR-101, NFR-202)
# ---------------------------------------------------------------------------


def register(
    email: str, display_name: str, password: str, *, locale: str = "en"
) -> dict:
    """Create an account and its isolated private space (FR-101).

    The first account on a fresh installation becomes the administrator;
    without that, a new deployment would have no one able to reach the
    administration screens (FR-362..364).
    """
    validate_password_strength(password)
    first_account = repo.count_seekers() == 0
    seeker_id = repo.create_seeker(
        email,
        display_name,
        password_hash=crypto.hash_password(password),
        is_admin=first_account,
        locale=locale,
    )
    record_audit(
        "job_seeker.registered",
        "job_seeker",
        seeker_id,
        seeker_id=seeker_id,
        detail={"email": email, "is_admin": first_account},
    )
    return repo.get_seeker(seeker_id) or {}


def authenticate(email: str, password: str, totp_code: str | None = None) -> dict:
    """Verify credentials and the second factor.  Returns the job_seeker row."""
    row = repo.get_seeker_by_email(email)
    if row is None or not row["password_hash"]:
        # Spend comparable time on unknown accounts so the response does not
        # tell an attacker which addresses are registered.
        crypto.hash_password(password)
        raise InvalidCredentials("Unknown e-mail address or password")
    if not crypto.verify_password(password, row["password_hash"]):
        raise InvalidCredentials("Unknown e-mail address or password")

    envelope = _totp_envelope(row)
    if envelope and envelope.get("active"):
        if not totp_code:
            raise MFARequired("A one-time code from your authenticator app is required")
        if not _verify_totp(envelope["secret"], totp_code):
            raise InvalidCredentials("Invalid one-time code")
    return row


def change_password(seeker_id: str, current_password: str, new_password: str) -> None:
    row = repo.get_seeker(seeker_id)
    if row is None or not row["password_hash"]:
        raise InvalidCredentials("No password set for this account")
    if not crypto.verify_password(current_password, row["password_hash"]):
        raise InvalidCredentials("Current password is incorrect")
    validate_password_strength(new_password)
    repo.set_password_hash(seeker_id, crypto.hash_password(new_password))
    # A password change invalidates every existing session (NFR-202).
    repo.delete_sessions_for(seeker_id)
    record_audit("job_seeker.password_changed", "job_seeker", seeker_id, seeker_id=seeker_id)


# ---------------------------------------------------------------------------
# Sessions (NFR-202)
# ---------------------------------------------------------------------------


def start_session(seeker_id: str, user_agent: str, ip: str) -> tuple[str, str]:
    """Create a client-bound session.  Returns ``(token, expires_at)``."""
    token, token_hash = crypto.new_session_token()
    expires_at = (datetime.now(UTC) + timedelta(hours=SESSION_TTL_HOURS)).isoformat(
        timespec="seconds"
    )
    repo.create_session(seeker_id, token_hash, crypto.client_binding(user_agent, ip), expires_at)
    repo.purge_expired_sessions()
    record_audit("session.started", "job_seeker", seeker_id, seeker_id=seeker_id)
    return token, expires_at


def end_session(token: str, seeker_id: str | None = None) -> bool:
    deleted = repo.delete_session(crypto.hash_token(token))
    if deleted:
        record_audit("session.ended", "job_seeker", seeker_id, seeker_id=seeker_id)
    return bool(deleted)


def end_all_sessions(seeker_id: str) -> int:
    return repo.delete_sessions_for(seeker_id)


# ---------------------------------------------------------------------------
# TOTP multi-factor authentication (NFR-202, NFR-201)
# ---------------------------------------------------------------------------


def _totp_envelope(seeker_row: dict) -> dict | None:
    blob = seeker_row.get("totp_secret_enc")
    if not blob:
        return None
    try:
        raw = crypto.decrypt_text(bytes(blob), purpose="totp", scope=seeker_row["id"])
    except crypto.CryptoUnavailable:
        raise MFAUnavailable(
            "DREAMJOB_MASTER_KEY is not configured, so the stored MFA secret cannot be read"
        ) from None
    except Exception:  # noqa: BLE001 - a corrupt envelope must not lock the account silently
        log.exception("Could not decrypt the MFA secret for job seeker %s", seeker_row["id"])
        return None
    return json.loads(raw)


def _store_envelope(seeker_id: str, envelope: dict) -> None:
    try:
        blob = crypto.encrypt_text(
            json.dumps(envelope), purpose="totp", scope=seeker_id
        )
    except crypto.CryptoUnavailable as exc:
        raise MFAUnavailable(str(exc)) from exc
    repo.set_totp_blob(seeker_id, blob)


def mfa_status(seeker_id: str) -> dict:
    row = repo.get_seeker(seeker_id)
    if row is None:
        return {"enrolled": False, "active": False}
    try:
        envelope = _totp_envelope(row)
    except MFAUnavailable:
        return {"enrolled": bool(row["totp_secret_enc"]), "active": False, "readable": False}
    return {
        "enrolled": envelope is not None,
        "active": bool(envelope and envelope.get("active")),
        "readable": True,
    }


def enroll_totp(seeker_id: str) -> dict:
    """Generate a secret and the otpauth URI for the authenticator app.

    The secret is stored immediately but marked inactive: logins keep working
    until :func:`activate_totp` proves the job seeker can produce a code.
    """
    row = repo.get_seeker(seeker_id)
    if row is None:
        raise InvalidCredentials("Unknown job seeker")
    secret = pyotp.random_base32()
    _store_envelope(seeker_id, {"secret": secret, "active": False, "enrolled_at": utcnow()})
    uri = pyotp.TOTP(secret).provisioning_uri(name=row["email"], issuer_name=TOTP_ISSUER)
    record_audit("mfa.enrolment_started", "job_seeker", seeker_id, seeker_id=seeker_id)
    return {"secret": secret, "otpauth_uri": uri, "issuer": TOTP_ISSUER}


def activate_totp(seeker_id: str, code: str) -> bool:
    row = repo.get_seeker(seeker_id)
    envelope = _totp_envelope(row) if row else None
    if not envelope:
        raise InvalidCredentials("No MFA enrolment is in progress")
    if not _verify_totp(envelope["secret"], code):
        return False
    envelope["active"] = True
    envelope["activated_at"] = utcnow()
    _store_envelope(seeker_id, envelope)
    record_audit("mfa.activated", "job_seeker", seeker_id, seeker_id=seeker_id)
    return True


def disable_totp(seeker_id: str, password: str, code: str | None = None) -> None:
    """Turn MFA off.  Needs the password, and a valid code while one is active."""
    row = repo.get_seeker(seeker_id)
    if row is None or not row["password_hash"]:
        raise InvalidCredentials("Unknown job seeker")
    if not crypto.verify_password(password, row["password_hash"]):
        raise InvalidCredentials("Password is incorrect")
    envelope = _totp_envelope(row)
    if envelope and envelope.get("active"):
        if not _verify_totp(envelope["secret"], code):
            raise InvalidCredentials("A valid one-time code is required to disable MFA")
    repo.set_totp_blob(seeker_id, None)
    record_audit("mfa.disabled", "job_seeker", seeker_id, seeker_id=seeker_id)


# ---------------------------------------------------------------------------
# Consent (CR-410, CR-401, FR-126)
# ---------------------------------------------------------------------------


def record_consent(seeker_id: str, kind: str, granted: bool, detail: str | None = None) -> dict:
    """Record a consent decision.  Append-only, so withdrawal is visible too.

    The text stored is always the text this system presents (CR-410); a note
    from the job seeker is kept beside it rather than in its place, so the
    record cannot be made to say that something else was agreed to.
    """
    if kind not in CONSENT_KINDS:
        raise ValueError(f"Unknown consent kind {kind!r}; expected one of {sorted(CONSENT_KINDS)}")
    presented = CONSENT_KINDS[kind]
    stored = f"{presented}\n\nNote from the job seeker: {detail}" if detail else presented
    consent_id = repo.record_consent(seeker_id, kind, granted, stored)
    record_audit(
        "consent.granted" if granted else "consent.withdrawn",
        "consent_record",
        consent_id,
        seeker_id=seeker_id,
        detail={"kind": kind},
    )
    return repo.latest_consent(seeker_id, kind) or {}


def has_consent(seeker_id: str, kind: str) -> bool:
    """True when this job seeker's latest decision for ``kind`` was 'granted'.

    The gate other slices call before profile data leaves the machine
    (CR-410), before a LinkedIn automation run (CR-401), and before online
    enrichment (FR-126).
    """
    return repo.has_consent(seeker_id, kind)


def require_consent(seeker_id: str, kind: str) -> None:
    """Raise :class:`ConsentRequired` unless the consent is in place."""
    if not has_consent(seeker_id, kind):
        raise ConsentRequired(kind)


def consent_state(seeker_id: str) -> dict[str, Any]:
    latest = repo.latest_consents(seeker_id)
    return {
        kind: {
            "kind": kind,
            "text": text,
            "granted": bool(latest[kind]["granted"]) if kind in latest else False,
            "decided_at": latest[kind]["granted_at"] if kind in latest else None,
            "detail": latest[kind]["detail"] if kind in latest else None,
        }
        for kind, text in CONSENT_KINDS.items()
    }


# ---------------------------------------------------------------------------
# GDPR export and erasure (NFR-301, FR-108)
# ---------------------------------------------------------------------------


def export_data(seeker_id: str) -> dict:
    """Everything held about one job seeker, as JSON (NFR-301)."""
    data = repo.export_private_data(seeker_id)
    data["consent"] = consent_state(seeker_id)
    record_audit("job_seeker.exported", "job_seeker", seeker_id, seeker_id=seeker_id)
    return data


def _delete_owned_files(paths: list[str]) -> int:
    """Remove generated documents from disk, staying inside the data directory."""
    data_dir = get_settings().abs_data_dir.resolve()
    removed = 0
    for raw in paths:
        try:
            path = Path(raw)
            path = path if path.is_absolute() else (get_settings().abs_data_dir.parent / path)
            path = path.resolve()
            if data_dir in path.parents and path.is_file():
                path.unlink()
                removed += 1
        except OSError:
            log.warning("Could not delete file during erasure: %s", raw)
    return removed


def erase(seeker_id: str) -> dict[str, int]:
    """Erase a job seeker and all their private data (FR-108, NFR-301).

    Shared knowledge-base rows - companies, vacancies, financial years, raw
    documents - stay: they carry no link back to the job seeker (FR-344) and
    are what makes the next campaign cheaper (FR-342).
    """
    seeker = repo.get_seeker(seeker_id)
    if seeker is None:
        raise InvalidCredentials("Unknown job seeker")
    files = repo.private_file_paths(seeker_id)
    counts = repo.erase_seeker(seeker_id)
    counts["files_deleted"] = _delete_owned_files(files)
    # A one-way reference: proof the erasure happened, no way back to the person.
    repo.record_erasure_marker(counts, crypto.hash_token(f"erased:{seeker_id}")[:16])
    log.info("Erased job seeker %s: %s", seeker_id, counts)
    return counts
