"""Authentication, consent and data-subject rights (NFR-202, CR-410, FR-108, NFR-301).

The account surface of Dream Job: register, log in with an optional TOTP
second factor, log out, read and correct your own record, record the consents
that gate the LLM transfer (CR-410), LinkedIn automation (CR-401) and online
enrichment (FR-126), export everything held about you (NFR-301) and erase it
(FR-108).

Other slices import :func:`has_consent` / :func:`require_consent` from
``dreamjob.security.auth_service`` before profile data is put in a prompt.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field

from dreamjob.api.deps import SESSION_COOKIE, CurrentSeeker, current_seeker
from dreamjob.config import get_settings
from dreamjob.db.repositories import seekers as repo
from dreamjob.security import auth_service as auth
from dreamjob.security.audit import record_audit
from dreamjob.security.auth_service import has_consent, require_consent

router = APIRouter()

__all__ = ["router", "has_consent", "require_consent"]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class RegisterIn(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=auth.MIN_PASSWORD_LENGTH, max_length=1024)
    locale: str = Field("en", max_length=8)


class LoginIn(BaseModel):
    email: EmailStr
    password: str
    totp_code: str | None = None


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=auth.MIN_PASSWORD_LENGTH, max_length=1024)


class ProfileIn(BaseModel):
    display_name: str | None = Field(None, min_length=1, max_length=200)
    locale: str | None = Field(None, max_length=8)


class TotpActivateIn(BaseModel):
    code: str


class TotpDisableIn(BaseModel):
    password: str
    code: str | None = None


class ConsentIn(BaseModel):
    kind: str
    granted: bool
    detail: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _token_from(request: Request) -> str | None:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return request.cookies.get(SESSION_COOKIE)


def _client(request: Request) -> tuple[str, str]:
    return (
        request.headers.get("user-agent", ""),
        request.client.host if request.client else "",
    )


def _issue_session(response: Response, seeker_id: str, request: Request) -> dict:
    user_agent, ip = _client(request)
    token, expires_at = auth.start_session(seeker_id, user_agent, ip)
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.env != "development",
        max_age=auth.SESSION_TTL_HOURS * 3600,
        path="/",
    )
    # The token is also returned so non-browser clients can use the bearer form.
    return {"token": token, "expires_at": expires_at}


def _public(row: dict) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "locale": row["locale"],
        "is_admin": bool(row["is_admin"]),
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------
# Registration and login (NFR-202)
# ---------------------------------------------------------------------------


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(payload: RegisterIn, request: Request, response: Response) -> dict:
    """Create a job seeker with an isolated private space (FR-101)."""
    try:
        row = auth.register(
            str(payload.email), payload.display_name, payload.password, locale=payload.locale
        )
    except auth.WeakPassword as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except repo.EmailAlreadyRegistered as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "An account already exists for this e-mail address"
        ) from exc
    session = _issue_session(response, row["id"], request)
    return {**_public(row), "session": session, "consent": auth.consent_state(row["id"])}


@router.post("/login")
def login(payload: LoginIn, request: Request, response: Response) -> dict:
    """Verify password and second factor, then open a client-bound session."""
    try:
        row = auth.authenticate(str(payload.email), payload.password, payload.totp_code)
    except auth.MFARequired as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            {"error": "mfa_required", "message": str(exc)},
        ) from exc
    except auth.MFAUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except auth.InvalidCredentials as exc:
        record_audit("session.login_failed", "job_seeker", None, detail={"email": payload.email})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    session = _issue_session(response, row["id"], request)
    return {**_public(row), "session": session, "consent": auth.consent_state(row["id"])}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    token = _token_from(request)
    ended = auth.end_session(token) if token else False
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ended": ended}


# ---------------------------------------------------------------------------
# The account itself
# ---------------------------------------------------------------------------


@router.get("/me")
def me(seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    row = repo.get_seeker(seeker.id)
    if row is None:  # the session outlived the account
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account no longer exists")
    return {
        **_public(row),
        "mfa": auth.mfa_status(seeker.id),
        "consent": auth.consent_state(seeker.id),
    }


@router.patch("/me")
def update_me(payload: ProfileIn, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    """Rectification of the account record (NFR-301)."""
    values = {k: v for k, v in payload.model_dump().items() if v is not None}
    repo.update_seeker(seeker.id, values)
    record_audit("job_seeker.updated", "job_seeker", seeker.id, seeker_id=seeker.id, detail=values)
    row = repo.get_seeker(seeker.id)
    return _public(row) if row else {}


@router.post("/me/password")
def change_password(
    payload: PasswordChangeIn,
    response: Response,
    seeker: CurrentSeeker = Depends(current_seeker),
) -> dict:
    try:
        auth.change_password(seeker.id, payload.current_password, payload.new_password)
    except auth.WeakPassword as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except auth.InvalidCredentials as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"changed": True, "sessions_revoked": True}


@router.get("/me/sessions")
def my_sessions(seeker: CurrentSeeker = Depends(current_seeker)) -> list[dict]:
    return repo.list_sessions(seeker.id)


@router.delete("/me/sessions")
def revoke_sessions(response: Response, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    count = auth.end_all_sessions(seeker.id)
    record_audit("session.revoked_all", "job_seeker", seeker.id, seeker_id=seeker.id)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"revoked": count}


# ---------------------------------------------------------------------------
# Multi-factor authentication (NFR-202)
# ---------------------------------------------------------------------------


@router.get("/me/mfa")
def mfa_state(seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    return auth.mfa_status(seeker.id)


@router.post("/me/mfa/enroll")
def mfa_enroll(seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    """Return a fresh secret and otpauth URI; MFA is not enforced until activated."""
    try:
        return auth.enroll_totp(seeker.id)
    except auth.MFAUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


@router.post("/me/mfa/activate")
def mfa_activate(payload: TotpActivateIn, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    try:
        activated = auth.activate_totp(seeker.id, payload.code)
    except auth.InvalidCredentials as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except auth.MFAUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    if not activated:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That code is not valid")
    return auth.mfa_status(seeker.id)


@router.post("/me/mfa/disable")
def mfa_disable(payload: TotpDisableIn, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    try:
        auth.disable_totp(seeker.id, payload.password, payload.code)
    except auth.InvalidCredentials as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    except auth.MFAUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return auth.mfa_status(seeker.id)


# ---------------------------------------------------------------------------
# Consent (CR-410, CR-401, FR-126)
# ---------------------------------------------------------------------------


@router.get("/consent")
def get_consent(seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    """Current state of each consent, with the text that was presented."""
    return auth.consent_state(seeker.id)


@router.post("/consent")
def post_consent(payload: ConsentIn, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    """Record a consent decision before the processing it covers takes place.

    CR-410 requires the ``llm_transfer`` consent to exist before any profile
    data is sent to DeepSeek; the pipeline checks it through
    ``auth_service.has_consent``.
    """
    try:
        return auth.record_consent(seeker.id, payload.kind, payload.granted, payload.detail)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.get("/consent/history")
def consent_history(seeker: CurrentSeeker = Depends(current_seeker)) -> list[dict]:
    """Every decision ever recorded, including withdrawals."""
    return repo.consent_history(seeker.id)


# ---------------------------------------------------------------------------
# Data-subject rights (NFR-301, FR-108)
# ---------------------------------------------------------------------------


@router.get("/me/export")
def export_me(seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    """Every private row held for this job seeker, as JSON (NFR-301)."""
    return auth.export_data(seeker.id)


@router.delete("/me")
def erase_me(response: Response, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    """Erase the account and all private data, keeping the shared knowledge base (FR-108)."""
    counts = auth.erase(seeker.id)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"erased": True, "rows_deleted": counts}
