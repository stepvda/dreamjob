"""Per-job-seeker encryption of sensitive rows at rest (NFR-201).

NFR-201 requires all job seeker data at rest to be encrypted, and profile
documents and generated CVs to be encrypted with per-job-seeker keys.  The
envelope in :mod:`dreamjob.security.crypto` already provided the keys - they
were used for the two-factor secret and mailbox tokens - but the profile itself
and the generated CV/motivation text were stored as plaintext in
``profile_version.sections`` and ``application_package.generation_notes``.

Sealing happens where a row is written (the repository knows the job seeker) and
unsealing happens centrally in :mod:`dreamjob.db.connection`, so that every
existing reader keeps seeing the same plaintext JSON string it always did.  A
value carries a marker prefix when sealed; a row written before this change, or
on an installation with no master key, has no marker and is returned untouched.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from dreamjob.security.crypto import CryptoUnavailable, decrypt_text, encrypt_text

log = logging.getLogger(__name__)

PREFIX = "enc:v1:"

#: Column name -> encryption purpose.  The key is derived per job seeker, and
#: the row's ``job_seeker_id`` supplies the scope on read.
SEALED_COLUMNS: dict[str, str] = {
    "sections": "profile",
    "generation_notes": "cv",
}


def seal(text: str | None, *, purpose: str, scope: str) -> str | None:
    """Seal a JSON string, or return it unchanged when no master key is set."""
    if text is None:
        return None
    try:
        blob = encrypt_text(text, purpose=purpose, scope=scope)
    except CryptoUnavailable:
        # Development/test installs without a master key keep working, in the
        # open - but loudly, because this is the difference NFR-201 is about.
        log.warning("DREAMJOB_MASTER_KEY is not set; storing %s unencrypted (NFR-201)", purpose)
        return text
    return PREFIX + base64.b64encode(blob).decode("ascii")


def unseal(value: Any, *, purpose: str, scope: str) -> Any:
    """Unseal a value, leaving an unmarked (plaintext) one untouched."""
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value
    try:
        blob = base64.b64decode(value[len(PREFIX):])
        return decrypt_text(blob, purpose=purpose, scope=scope)
    except Exception:  # noqa: BLE001 - a wrong key must not take a request down
        log.exception("Could not decrypt a sealed %s value", purpose)
        return value


def unseal_row(row: dict | None) -> dict | None:
    """Unseal every sealed column of a row, using its own job seeker key."""
    if row is None:
        return None
    for column, purpose in SEALED_COLUMNS.items():
        value = row.get(column)
        if isinstance(value, str) and value.startswith(PREFIX):
            scope = str(row.get("job_seeker_id") or "")
            row[column] = unseal(value, purpose=purpose, scope=scope)
    return row
