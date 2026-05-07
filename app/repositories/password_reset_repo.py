"""password_reset_repo.py — CRUD for password_reset_tokens.

GLOBAL repo (no tenant scope). Uses _get_pool() directly like sessions_repo,
because password_reset_tokens and users are both global tables with no
restaurant_id/org_id column (RLS does not apply).

Token storage: SHA-256 hash via Python hashlib (matches sessions_repo style).
The raw 6-digit code never persists in the database.

Max attempts: 5 wrong codes → token is locked (too_many_attempts error).
TTL: 15 minutes from creation. Expired-but-recent rows are kept for audit;
     rows older than 1 hour past expiry are cleaned by db_cleanup_expired_password_resets.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from app.services.logging import get_logger

log = get_logger(__name__)

_MAX_ATTEMPTS = 5
_TTL_MINUTES = 15
_AUDIT_KEEP_HOURS = 1  # keep expired rows for this long before purging


def _hash_code(raw: str) -> bytes:
    """Return the SHA-256 digest of a raw OTP code as bytes."""
    return hashlib.sha256(raw.encode()).digest()


def _generate_code() -> str:
    """Generate a cryptographically random 6-digit numeric OTP."""
    # secrets.randbelow(1_000_000) gives [0, 999999]; zero-pad to 6 digits.
    return f"{secrets.randbelow(1_000_000):06d}"


async def _get_pool():
    from app.services.database import get_pool  # noqa: PLC0415 — break import cycle
    return await get_pool()


async def db_create_password_reset(user_username: str) -> str:
    """Create a new password-reset OTP for *user_username*.

    Steps:
      1. Invalidate any prior unused tokens for this user (set used_at=NOW()).
      2. Generate a new 6-digit code, hash it, insert the row.

    Returns the raw plaintext code (caller sends it to the user via WhatsApp
    and MUST NOT store it anywhere else).
    """
    code = _generate_code()
    code_hash = _hash_code(code)
    expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=_TTL_MINUTES)

    pool = await _get_pool()
    async with pool.acquire() as conn:
        # Invalidate prior unused tokens (anti-replay: old codes stop working
        # the moment a new one is issued).
        await conn.execute(
            """
            UPDATE password_reset_tokens
               SET used_at = NOW()
             WHERE user_username = $1
               AND used_at IS NULL
            """,
            user_username,
        )

        await conn.execute(
            """
            INSERT INTO password_reset_tokens
                (user_username, code_hash, expires_at)
            VALUES ($1, $2, $3)
            """,
            user_username, code_hash, expires_at,
        )

    log.info("password_reset.created", user_username_prefix=user_username[:3] + "***")
    return code


async def db_consume_password_reset(
    user_username: str, code: str
) -> tuple[bool, str | None]:
    """Verify and consume a password-reset OTP.

    Returns:
        (True, None)             — code is valid, token marked used.
        (False, "invalid_code")  — no active token or wrong code (attempts incremented).
        (False, "expired")       — token exists but TTL passed.
        (False, "too_many_attempts") — 5+ wrong codes on this token.
    """
    code_hash = _hash_code(code)
    pool = await _get_pool()
    async with pool.acquire() as conn:
        # Load the most-recent active (unused) token for this user.
        row = await conn.fetchrow(
            """
            SELECT id, code_hash, expires_at, attempts
              FROM password_reset_tokens
             WHERE user_username = $1
               AND used_at IS NULL
             ORDER BY created_at DESC
             LIMIT 1
            """,
            user_username,
        )

        if row is None:
            log.warning(
                "password_reset.consume.no_active_token",
                user_username_prefix=user_username[:3] + "***",
            )
            return False, "invalid_code"

        token_id: int = row["id"]
        attempts: int = row["attempts"]

        # Check attempt ceiling BEFORE verifying code (avoids leaking timing).
        if attempts >= _MAX_ATTEMPTS:
            log.warning(
                "password_reset.consume.too_many_attempts",
                token_id=token_id,
                attempts=attempts,
            )
            return False, "too_many_attempts"

        # Check expiry.
        if row["expires_at"].replace(tzinfo=None) < datetime.now(timezone.utc).replace(tzinfo=None):
            log.info("password_reset.consume.expired", token_id=token_id)
            return False, "expired"

        # Constant-time comparison: compare stored hash bytes to submitted hash bytes.
        submitted_hash = _hash_code(code)
        stored_hash: bytes = bytes(row["code_hash"])

        if submitted_hash != stored_hash:
            # Wrong code — increment attempts counter.
            await conn.execute(
                "UPDATE password_reset_tokens SET attempts = attempts + 1 WHERE id = $1",
                token_id,
            )
            log.warning(
                "password_reset.consume.wrong_code",
                token_id=token_id,
                new_attempts=attempts + 1,
            )
            return False, "invalid_code"

        # Success — mark token as used.
        await conn.execute(
            "UPDATE password_reset_tokens SET used_at = NOW() WHERE id = $1",
            token_id,
        )

    log.info(
        "password_reset.consume.success",
        user_username_prefix=user_username[:3] + "***",
        token_id=token_id,
    )
    return True, None


async def db_cleanup_expired_password_resets() -> int:
    """Delete rows where expires_at < NOW() - 1h (keep recent ones for audit).

    Returns the number of rows deleted. Intended to be called periodically
    by the scheduler (no urgency — rows are harmless after TTL passes).
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            DELETE FROM password_reset_tokens
             WHERE expires_at < NOW() - INTERVAL '1 hour'
            """
        )
    count = int(result.split()[-1]) if result else 0
    if count > 0:
        log.info("password_reset.cleanup", deleted=count)
    return count
