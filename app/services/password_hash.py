"""
app/services/password_hash.py

Thin bcrypt wrapper that replaces passlib.CryptContext.

Key design decisions:
  1. Rounds pinned at 12 (Fix #4).
  2. Pre-hash + base64 for new hashes to dodge bcrypt's 72-byte silent truncation
     — passwords > ~48 chars (before base64 expansion) would otherwise be silently
     truncated. New hashes use pre-hash path; existing $2b$ hashes in the DB are
     verified via the raw path first (option b from spec).
  3. No custom crypto — uses well-tested `bcrypt` 4.x directly.

Migration compatibility (option b):
  - hash_password()     → always produces pre-hashed bcrypt (new style)
  - verify_password()   → tries raw first (legacy $2b$ hashes), then pre-hashed
                          (new-style hashes). Transparent to callers.
"""

from __future__ import annotations

import base64
import hashlib

import bcrypt

_ROUNDS = 12

# TODO(bcrypt-72-byte): pre-hashing was introduced <date>. After 100% of old
# hashes have been opportunistically upgraded (auth.py legacy upgrade path), the
# raw-path fallback in verify_password can be removed in a future cleanup sprint.


def _prep(password: str) -> bytes:
    """Base64-encode the SHA-256 digest of a password to stay within 72 bytes."""
    return base64.b64encode(hashlib.sha256(password.encode()).digest())


def hash_password(password: str) -> str:
    """Return a bcrypt hash of *password* (pre-hashed, rounds=12).

    Always use this for NEW credentials. The returned string is a standard
    $2b$ hash and is safe to store in the database.
    """
    return bcrypt.hashpw(_prep(password), bcrypt.gensalt(rounds=_ROUNDS)).decode()


def verify_password(plain: str, hashed: str, _username: str = "") -> bool:
    """Return True if *plain* matches *hashed*.

    Tries:
      1. Pre-hashed path  — matches hashes produced by hash_password() above.
      2. Raw path         — matches legacy hashes stored before this migration
                            (plain bytes fed directly to bcrypt).

    The raw-path fallback is needed for existing hashes created by the old
    passlib CryptContext which fed the password bytes directly to bcrypt.
    Both paths use constant-time comparison internally (bcrypt.checkpw).
    """
    if not plain or not hashed:
        return False
    # Path 3 — oldest legacy: plain sha256 hex (not bcrypt at all).
    # Some very early accounts stored sha256(password).hexdigest() directly.
    # is_legacy_hash() returns True for these; auth.py upgrades them on login.
    if not hashed.startswith("$2"):
        import hashlib as _hl
        return _hl.sha256(plain.encode()).hexdigest() == hashed
    try:
        hashed_bytes = hashed.encode()
        # Path 1 — new-style (pre-hashed with SHA-256 before bcrypt)
        if bcrypt.checkpw(_prep(plain), hashed_bytes):
            return True
        # Path 2 — legacy passlib-bcrypt (raw bytes fed directly to bcrypt)
        if bcrypt.checkpw(plain.encode(), hashed_bytes):
            return True
        return False
    except (ValueError, TypeError):
        return False


def is_legacy_hash(hashed_password: str) -> bool:
    """True if the hash looks like it was produced by passlib (sha256 hex or non-bcrypt).

    Bcrypt hashes always start with $2 ($2a$, $2b$, $2y$). Anything else is legacy.
    """
    return not (hashed_password or "").startswith("$2")
