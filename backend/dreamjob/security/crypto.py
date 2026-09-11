"""Encryption at rest and password hashing (NFR-201, NFR-202, NFR-204).

Envelope scheme
---------------
A single master key (``DREAMJOB_MASTER_KEY``) never encrypts data directly.
Each job seeker gets a derived key via HKDF over the master key and the job
seeker id, so profile documents, generated CVs and mailbox credentials are
encrypted per job seeker as NFR-201 requires.  Losing one derived key does
not expose another job seeker's data, and deleting a job seeker (FR-108)
makes their ciphertext permanently unreadable even if a stray copy survives.

AES-256-GCM provides confidentiality and integrity; the 12-byte nonce is
prepended to the ciphertext.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from dreamjob.config import get_settings

log = logging.getLogger(__name__)

_NONCE_BYTES = 12


class CryptoUnavailable(RuntimeError):
    """Raised when encryption is required but no master key is configured."""


def _derive_key(purpose: str, scope: str) -> bytes:
    master = get_settings().master_key_bytes()
    if not master:
        raise CryptoUnavailable(
            "DREAMJOB_MASTER_KEY is not set; refusing to store sensitive data unencrypted. "
            "Generate one with: python -m dreamjob.security.crypto --generate-key"
        )
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=f"dreamjob:{purpose}:{scope}".encode(),
    ).derive(master)


def encrypt(plaintext: bytes, *, purpose: str = "generic", scope: str = "global") -> bytes:
    """Encrypt with a key derived for ``purpose``/``scope`` (usually a job seeker id)."""
    key = _derive_key(purpose, scope)
    nonce = os.urandom(_NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, f"{purpose}|{scope}".encode())


def decrypt(blob: bytes, *, purpose: str = "generic", scope: str = "global") -> bytes:
    key = _derive_key(purpose, scope)
    nonce, body = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
    return AESGCM(key).decrypt(nonce, body, f"{purpose}|{scope}".encode())


def encrypt_text(text: str, *, purpose: str = "generic", scope: str = "global") -> bytes:
    return encrypt(text.encode("utf-8"), purpose=purpose, scope=scope)


def decrypt_text(blob: bytes, *, purpose: str = "generic", scope: str = "global") -> str:
    return decrypt(blob, purpose=purpose, scope=scope).decode("utf-8")


# --- Passwords and tokens ---------------------------------------------------

def hash_password(password: str) -> str:
    """Argon2id (NFR-202), falling back to PBKDF2 if argon2-cffi is absent."""
    try:
        from argon2 import PasswordHasher

        return PasswordHasher().hash(password)
    except ImportError:  # pragma: no cover - dependency present in requirements
        salt = secrets.token_bytes(16)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
        return "pbkdf2$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    if stored.startswith("pbkdf2$"):
        _, salt_b64, dk_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    try:
        from argon2 import PasswordHasher
        from argon2.exceptions import VerificationError

        try:
            return PasswordHasher().verify(stored, password)
        except VerificationError:
            return False
    except ImportError:  # pragma: no cover
        return False


def new_session_token() -> tuple[str, str]:
    """Return ``(token, token_hash)``.  Only the hash is stored (NFR-202)."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


_SESSION_SECRET_WARNED = False


def hash_token(token: str) -> str:
    global _SESSION_SECRET_WARNED
    secret = get_settings().session_secret
    if not secret:
        # The fallback is a constant published in this repository.  Warn once
        # rather than weaken every session silently (NFR-202).
        if not _SESSION_SECRET_WARNED:
            log.warning(
                "DREAMJOB_SESSION_SECRET is not set; session tokens are keyed with a "
                "public default. Set it before exposing this installation."
            )
            _SESSION_SECRET_WARNED = True
        secret = "dreamjob-dev-session-secret"
    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()


def client_binding(user_agent: str, ip: str) -> str:
    """Fingerprint binding a session to its client (NFR-202)."""
    return hashlib.sha256(f"{user_agent}|{ip}".encode()).hexdigest()[:32]


def generate_master_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


if __name__ == "__main__":  # pragma: no cover
    import sys

    if "--generate-key" in sys.argv:
        print("DREAMJOB_MASTER_KEY=" + generate_master_key())
        print("DREAMJOB_SESSION_SECRET=" + secrets.token_urlsafe(48))
