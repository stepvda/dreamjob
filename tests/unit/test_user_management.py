"""User management: create, roles, suspension, recovery, deletion (FR-362, FR-101, NFR-202).

Runs against a throwaway SQLite file with only the auth and admin routers
mounted, so it does not depend on any other slice and never touches the
installed database.

Two properties are the point of these tests, and both are enforced in the
service layer rather than the interface:

* the last active administrator can never be demoted, suspended or deleted -
  an installation left with no admin cannot reach the screens that appoint one;
* a suspension takes effect at once, ending every open session, so "disable"
  means disabled now rather than at the next sign-in.
"""

from __future__ import annotations

import base64
import os
import secrets
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from dreamjob.config import get_settings
from fastapi import FastAPI
from fastapi.testclient import TestClient

_ENV_KEYS = (
    "DREAMJOB_DATA_DIR",
    "DREAMJOB_DB_PATH",
    "DREAMJOB_MASTER_KEY",
    "DREAMJOB_SESSION_SECRET",
    "DREAMJOB_ENV",
)

OWNER = "owner@example.com"
PASSWORD = "Str0ng-Passphrase!2026"


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["DREAMJOB_DATA_DIR"] = str(tmp_path / "data")
    os.environ["DREAMJOB_DB_PATH"] = str(tmp_path / "data" / "test.db")
    os.environ["DREAMJOB_MASTER_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    os.environ["DREAMJOB_SESSION_SECRET"] = secrets.token_urlsafe(32)
    os.environ["DREAMJOB_ENV"] = "development"
    get_settings.cache_clear()

    from dreamjob.db.migrator import migrate

    migrate()
    yield

    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


@pytest.fixture
def clients() -> Iterator[Callable[[], TestClient]]:
    """A factory of clients, each with its own cookie jar.

    Signing in as a different account must not clobber the administrator's
    cookie, so tests that act as two people at once need two clients. A single
    shared ``TestClient`` silently switches identity on the second login.
    """
    from dreamjob.api.routers import admin as admin_router
    from dreamjob.api.routers import auth as auth_router

    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api/auth")
    app.include_router(admin_router.router, prefix="/api/admin")

    def make() -> TestClient:
        return TestClient(app)

    yield make


def _register(client: TestClient, email: str) -> dict:
    resp = client.post(
        "/api/auth/register",
        json={"email": email, "display_name": "Test User", "password": PASSWORD},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _sign_in(client: TestClient, email: str, password: str) -> int:
    return client.post(
        "/api/auth/login", json={"email": email, "password": password}
    ).status_code


@pytest.fixture
def admin(clients: Callable[[], TestClient]) -> TestClient:
    """A signed-in administrator, plus the account id of an ordinary user."""
    c = clients()
    _register(c, OWNER)  # first account becomes the administrator
    return c


# ---------------------------------------------------------------------------
# Listing and search
# ---------------------------------------------------------------------------


def test_the_list_shows_every_account_with_the_fields_the_screen_needs(admin):
    r = admin.get("/api/admin/users")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["administrators"] == 1
    row = body["users"][0]
    assert row["email"] == OWNER
    assert row["is_admin"] is True
    assert row["disabled"] is False
    assert row["active_sessions"] >= 1  # the admin is signed in
    # The password and the TOTP secret are never in the view.
    assert "password_hash" not in row and "totp_secret_enc" not in row


def test_search_matches_email_and_name(admin):
    admin.post(
        "/api/admin/users",
        json={"email": "bob@example.com", "display_name": "Bob Builder", "is_admin": False},
    )
    assert admin.get("/api/admin/users", params={"q": "bob@example.com"}).json()["total"] == 1
    assert admin.get("/api/admin/users", params={"q": "builder"}).json()["total"] == 1
    assert admin.get("/api/admin/users", params={"q": "nobody"}).json()["total"] == 0


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_create_user_generates_a_password_that_works(admin, clients):
    r = admin.post(
        "/api/admin/users",
        json={"email": "carol@example.com", "display_name": "Carol", "is_admin": False},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["password_generated"] is True
    assert body["password"]
    assert body["user"]["is_admin"] is False

    other = clients()
    assert _sign_in(other, "carol@example.com", body["password"]) == 200


def test_create_user_refuses_a_duplicate_email(admin):
    r = admin.post(
        "/api/admin/users",
        json={"email": OWNER, "display_name": "Duplicate", "is_admin": False},
    )
    assert r.status_code == 409


def test_create_user_enforces_password_strength(admin):
    r = admin.post(
        "/api/admin/users",
        json={"email": "weak@example.com", "display_name": "Weak", "password": "short"},
    )
    assert r.status_code == 422


def test_a_created_admin_can_reach_the_admin_surface(admin, clients):
    r = admin.post(
        "/api/admin/users",
        json={
            "email": "second-admin@example.com",
            "display_name": "Second Admin",
            "is_admin": True,
        },
    )
    pw = r.json()["password"]
    other = clients()
    assert _sign_in(other, "second-admin@example.com", pw) == 200
    assert other.get("/api/admin/users").status_code == 200


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


def test_grant_and_revoke_admin(admin):
    uid = admin.post(
        "/api/admin/users",
        json={"email": "dave@example.com", "display_name": "Dave", "is_admin": False},
    ).json()["user"]["id"]

    assert admin.patch(f"/api/admin/users/{uid}", json={"is_admin": True}).json()["is_admin"] is True
    # With two administrators, the role can be taken away again.
    assert (
        admin.patch(f"/api/admin/users/{uid}", json={"is_admin": False}).json()["is_admin"]
        is False
    )


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------


def test_reset_password_replaces_the_old_one_and_ends_sessions(admin, clients):
    created = admin.post(
        "/api/admin/users",
        json={"email": "erin@example.com", "display_name": "Erin", "is_admin": False},
    ).json()
    uid, old = created["user"]["id"], created["password"]

    victim = clients()
    assert _sign_in(victim, "erin@example.com", old) == 200
    assert victim.get("/api/auth/me").status_code == 200

    r = admin.post(f"/api/admin/users/{uid}/reset-password", json={})
    assert r.status_code == 200
    new = r.json()["password"]

    # The reset ended the session that was open, not just future logins.
    assert victim.get("/api/auth/me").status_code == 401
    assert _sign_in(clients(), "erin@example.com", old) == 401
    assert _sign_in(clients(), "erin@example.com", new) == 200


# ---------------------------------------------------------------------------
# Suspension
# ---------------------------------------------------------------------------


def test_suspend_blocks_login_and_ends_open_sessions(admin, clients):
    created = admin.post(
        "/api/admin/users",
        json={"email": "frank@example.com", "display_name": "Frank", "is_admin": False},
    ).json()
    uid, pw = created["user"]["id"], created["password"]

    victim = clients()
    assert _sign_in(victim, "frank@example.com", pw) == 200

    assert admin.patch(f"/api/admin/users/{uid}", json={"disabled": True}).json()["disabled"] is True

    # Suspension is immediate: the open tab is refused (its session was
    # revoked, so 401; were a session to survive, the disabled flag answers
    # 403) and a fresh login is refused outright.
    assert victim.get("/api/auth/me").status_code in (401, 403)
    assert _sign_in(clients(), "frank@example.com", pw) == 403


def test_restore_lets_the_account_back_in(admin, clients):
    created = admin.post(
        "/api/admin/users",
        json={"email": "gina@example.com", "display_name": "Gina", "is_admin": False},
    ).json()
    uid, pw = created["user"]["id"], created["password"]

    admin.patch(f"/api/admin/users/{uid}", json={"disabled": True})
    assert _sign_in(clients(), "gina@example.com", pw) == 403
    admin.patch(f"/api/admin/users/{uid}", json={"disabled": False})
    assert _sign_in(clients(), "gina@example.com", pw) == 200


def test_suspending_an_admin_needs_another_admin(admin):
    """The last administrator cannot be suspended, which a second admin frees up."""
    created = admin.post(
        "/api/admin/users",
        json={"email": "admin2@example.com", "display_name": "Admin Two", "is_admin": True},
    ).json()
    uid = created["user"]["id"]
    # Two admins: suspending one is allowed.
    assert admin.patch(f"/api/admin/users/{uid}", json={"disabled": True}).status_code == 200


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


def test_delete_removes_the_account(admin, clients):
    created = admin.post(
        "/api/admin/users",
        json={"email": "helen@example.com", "display_name": "Helen", "is_admin": False},
    ).json()
    uid, pw = created["user"]["id"], created["password"]

    assert admin.delete(f"/api/admin/users/{uid}").status_code == 200
    assert _sign_in(clients(), "helen@example.com", pw) == 401
    assert admin.get(f"/api/admin/users/{uid}").status_code == 404


# ---------------------------------------------------------------------------
# Guards: the last admin, and yourself
# ---------------------------------------------------------------------------


def test_the_last_admin_cannot_be_demoted_suspended_or_deleted(admin):
    me = admin.get("/api/admin/users").json()["me"]
    # The self-guard fires first for the sole admin (409 either way).
    assert admin.patch(f"/api/admin/users/{me}", json={"is_admin": False}).status_code == 409
    assert admin.patch(f"/api/admin/users/{me}", json={"disabled": True}).status_code == 409
    assert admin.delete(f"/api/admin/users/{me}").status_code == 409


def test_the_last_admin_guard_holds_for_a_second_admin_acting_on_the_first(admin, clients):
    """A second admin cannot strip the last *usable* admin of their role."""
    created = admin.post(
        "/api/admin/users",
        json={"email": "admin3@example.com", "display_name": "Admin Three", "is_admin": True},
    ).json()
    uid, pw = created["user"]["id"], created["password"]

    # admin3 signs in and disables the only *other* admin (the owner).
    third = clients()
    assert _sign_in(third, "admin3@example.com", pw) == 200
    owner_id = admin.get("/api/admin/users").json()["me"]

    # Disabling the owner is fine: admin3 remains an active administrator.
    assert third.patch(f"/api/admin/users/{owner_id}", json={"disabled": True}).status_code == 200
    # Now admin3 is the last active administrator and cannot demote themselves.
    assert third.patch(f"/api/admin/users/{uid}", json={"is_admin": False}).status_code == 409
    assert third.patch(f"/api/admin/users/{uid}", json={"disabled": True}).status_code == 409
    assert third.delete(f"/api/admin/users/{uid}").status_code == 409


def test_a_non_admin_cannot_reach_the_user_surface(admin, clients):
    """An ordinary account is refused, whatever it tries to reach."""
    created = admin.post(
        "/api/admin/users",
        json={"email": "plain@example.com", "display_name": "Plain", "is_admin": False},
    ).json()
    ordinary = clients()
    assert _sign_in(ordinary, "plain@example.com", created["password"]) == 200

    assert ordinary.get("/api/admin/users").status_code == 403
    assert ordinary.post(
        "/api/admin/users",
        json={"email": "sneaky@example.com", "display_name": "Sneaky"},
    ).status_code == 403
    assert ordinary.delete(f"/api/admin/users/{created['user']['id']}").status_code == 403


# ---------------------------------------------------------------------------
# Bulk clean-up of accounts without sessions
# ---------------------------------------------------------------------------


def test_purge_dry_run_reports_without_deleting(admin):
    admin.post(
        "/api/admin/users",
        json={"email": "unused@example.com", "display_name": "Unused", "is_admin": False},
    )
    before = admin.get("/api/admin/users").json()["total"]

    r = admin.post("/api/admin/users/purge-without-sessions", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    assert body["candidates"] == 1  # the never-signed-in account
    assert body["deleted"] == 0
    assert "unused@example.com" in body["sample"]
    # Nothing was removed.
    assert admin.get("/api/admin/users").json()["total"] == before


def test_purge_deletes_accounts_without_sessions_but_keeps_the_admin(admin):
    admin.post(
        "/api/admin/users",
        json={"email": "unused2@example.com", "display_name": "Unused Two", "is_admin": False},
    )
    r = admin.post(
        "/api/admin/users/purge-without-sessions", json={"exclude_admins": True, "dry_run": False}
    )
    assert r.status_code == 200
    assert r.json()["deleted"] == 1
    # The signed-in administrator survives, and is the only account left.
    listing = admin.get("/api/admin/users").json()
    assert listing["total"] == 1
    assert listing["users"][0]["email"] == OWNER


def test_purge_keeps_accounts_that_are_signed_in(admin, clients):
    created = admin.post(
        "/api/admin/users",
        json={"email": "active@example.com", "display_name": "Active", "is_admin": False},
    ).json()
    holder = clients()
    assert _sign_in(holder, "active@example.com", created["password"]) == 200

    r = admin.post(
        "/api/admin/users/purge-without-sessions", json={"exclude_admins": True, "dry_run": False}
    )
    assert r.json()["deleted"] == 0
    assert admin.get("/api/admin/users").json()["total"] == 2
