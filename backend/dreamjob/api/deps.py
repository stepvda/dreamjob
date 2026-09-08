"""FastAPI dependencies: authentication and tenant isolation (FR-101, FR-344).

``current_seeker`` is the isolation boundary.  Every private-data router
depends on it and passes ``seeker.id`` into repository calls, so no request
can read another job seeker's profile, opportunities or documents.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status

from dreamjob.db.connection import query_one, utcnow
from dreamjob.security.crypto import client_binding, hash_token

SESSION_COOKIE = "dreamjob_session"


@dataclass
class CurrentSeeker:
    id: str
    email: str
    display_name: str
    is_admin: bool
    locale: str


def _token_from(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get(SESSION_COOKIE)


def current_seeker(request: Request) -> CurrentSeeker:
    token = _token_from(request)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")

    row = query_one(
        """
        SELECT s.job_seeker_id, s.expires_at, s.client_binding,
               j.email, j.display_name, j.is_admin, j.locale
        FROM session s JOIN job_seeker j ON j.id = s.job_seeker_id
        WHERE s.token_hash = ?
        """,
        (hash_token(token),),
    )
    if row is None or row["expires_at"] <= utcnow():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")

    # NFR-202: sessions are bound to the client that created them.
    if row["client_binding"]:
        expected = client_binding(
            request.headers.get("user-agent", ""),
            request.client.host if request.client else "",
        )
        if expected != row["client_binding"]:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session binding mismatch")

    return CurrentSeeker(
        id=row["job_seeker_id"],
        email=row["email"],
        display_name=row["display_name"],
        is_admin=bool(row["is_admin"]),
        locale=row["locale"],
    )


def current_admin(seeker: CurrentSeeker = Depends(current_seeker)) -> CurrentSeeker:
    if not seeker.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Administrator role required")
    return seeker


def owned_or_404(table: str, row_id: str, seeker_id: str) -> dict:
    """Fetch a private row, 404-ing if it belongs to another job seeker (FR-344)."""
    row = query_one(f"SELECT * FROM {table} WHERE id = ? AND job_seeker_id = ?", (row_id, seeker_id))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{table} not found")
    return row
