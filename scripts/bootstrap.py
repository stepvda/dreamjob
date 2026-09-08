#!/usr/bin/env python3
"""Prepare a fresh installation: schema, product owner, defaults (FR-101, NFR-202).

Run once after cloning:

    python3 scripts/bootstrap.py                     # generates a password
    DREAMJOB_BOOTSTRAP_PASSWORD=... python3 scripts/bootstrap.py

Idempotent: re-running promotes the account to administrator if needed and
leaves an existing password alone unless ``--reset-password`` is given.
"""

from __future__ import annotations

import argparse
import os
import secrets
import string
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from dreamjob.config import get_settings  # noqa: E402
from dreamjob.db.migrator import migrate  # noqa: E402
from dreamjob.db.repositories import seekers as repo  # noqa: E402
from dreamjob.security import auth_service as auth  # noqa: E402
from dreamjob.security import crypto  # noqa: E402
from dreamjob.security.audit import record_audit  # noqa: E402

OWNER_EMAIL = "stephane@stepvda.com"
OWNER_NAME = "Stephane van der Aa"


def generate_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        try:
            auth.validate_password_strength(candidate)
        except auth.WeakPassword:
            continue
        return candidate


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap a Dream Job installation")
    parser.add_argument("--email", default=OWNER_EMAIL)
    parser.add_argument("--name", default=OWNER_NAME)
    parser.add_argument("--password", default=None, help="defaults to $DREAMJOB_BOOTSTRAP_PASSWORD")
    parser.add_argument("--reset-password", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    settings.ensure_dirs()
    applied = migrate()
    print(f"Database   : {settings.abs_db_path}")
    print(f"Migrations : {', '.join(applied) if applied else 'already up to date'}")

    if not settings.master_key:
        print(
            "WARNING    : DREAMJOB_MASTER_KEY is not set. Encryption at rest (NFR-201) and "
            "MFA enrolment are unavailable until it is; generate one with\n"
            "             python3 -m dreamjob.security.crypto --generate-key"
        )

    password = args.password or os.environ.get("DREAMJOB_BOOTSTRAP_PASSWORD") or ""
    generated = False
    if not password:
        password, generated = generate_password(), True

    existing = repo.get_seeker_by_email(args.email)
    if existing is None:
        auth.validate_password_strength(password)
        seeker_id = repo.create_seeker(
            args.email,
            args.name,
            password_hash=crypto.hash_password(password),
            is_admin=True,
        )
        record_audit(
            "job_seeker.bootstrapped", "job_seeker", seeker_id, seeker_id=seeker_id,
            actor="bootstrap", detail={"email": args.email, "is_admin": True},
        )
        print(f"Created    : {args.email} (administrator, id {seeker_id})")
        if generated:
            print(f"Password   : {password}   <- shown once; store it now")
    else:
        seeker_id = existing["id"]
        if not existing["is_admin"]:
            repo.set_admin(seeker_id, True)
            record_audit(
                "admin.role_changed", "job_seeker", seeker_id, seeker_id=seeker_id,
                actor="bootstrap", detail={"is_admin": True},
            )
            print(f"Promoted   : {args.email} is now an administrator")
        if args.reset_password:
            auth.validate_password_strength(password)
            repo.set_password_hash(seeker_id, crypto.hash_password(password))
            repo.delete_sessions_for(seeker_id)
            record_audit(
                "job_seeker.password_changed", "job_seeker", seeker_id, seeker_id=seeker_id,
                actor="bootstrap",
            )
            print("Password   : reset; all sessions revoked")
            if generated:
                print(f"Password   : {password}   <- shown once; store it now")
        print(f"Exists     : {args.email} (id {seeker_id})")

    print(f"Accounts   : {repo.count_seekers()}")
    print(
        "Next       : start the API with `python3 -m dreamjob.main` (PYTHONPATH=backend), "
        "log in, and record the CR-410 consent before running a campaign."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
