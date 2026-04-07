"""sessions_repo.py — Low-level CRUD for the `sessions` table.

Tokens are stored as SHA-256 digests (BYTEA column `token_hash`).
The raw token never persists in the database; it lives only on the client.

Lookup strategy (two-phase, one release cycle):
  1. Hash the incoming token and query by token_hash.
  2. If not found, fall back to the legacy plaintext `token` column and log
     a structured INFO event.  Once legacy_lookup volume reaches zero (monitor
     for ~2 weeks) it is safe to remove the fallback and later drop the `token`
     column via a follow-up migration.
"""

import hashlib
import secrets
from datetime import datetime, timedelta

from app.services.database import get_pool
from app.services.logging import get_logger

log = get_logger(__name__)

SESSION_TTL_HOURS = 72


def _hash_token(raw: str) -> bytes:
    """Return the SHA-256 digest of a raw session token as bytes."""
    return hashlib.sha256(raw.encode()).digest()


async def create_session(username: str) -> str:
    """Generate a new session token, persist only its hash, return the raw token."""
    raw = secrets.token_hex(32)
    token_hash = _hash_token(raw)
    expires = datetime.utcnow() + timedelta(hours=SESSION_TTL_HOURS)

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sessions (token, token_hash, username, expires_at)
            VALUES (NULL, $1, $2, $3)
            """,
            token_hash, username, expires,
        )
    return raw


async def get_session(raw_token: str) -> str | None:
    """Return the username for a valid, non-expired session token, or None."""
    token_hash = _hash_token(raw_token)
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Phase 1 — hash lookup (new rows)
        row = await conn.fetchrow(
            """
            SELECT username FROM sessions
             WHERE token_hash = $1 AND expires_at > NOW()
            """,
            token_hash,
        )
        if row:
            return row["username"]

        # Phase 2 — legacy plaintext fallback (pre-migration rows)
        row = await conn.fetchrow(
            """
            SELECT username FROM sessions
             WHERE token = $1 AND expires_at > NOW()
            """,
            raw_token,
        )
        if row:
            log.info(
                "session.legacy_lookup",
                hint="row predates migration; backfill token_hash then drop token column",
            )
            return row["username"]

    return None


async def delete_session(raw_token: str) -> None:
    """Revoke a session by deleting the matching row."""
    token_hash = _hash_token(raw_token)
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Try hash-based delete first; fall back to plaintext for legacy rows.
        result = await conn.execute(
            "DELETE FROM sessions WHERE token_hash = $1",
            token_hash,
        )
        deleted = int(result.split()[-1]) if result else 0
        if deleted == 0:
            await conn.execute(
                "DELETE FROM sessions WHERE token = $1",
                raw_token,
            )


async def cleanup_expired_sessions() -> int:
    """Delete expired sessions and return the count removed."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM sessions WHERE expires_at < NOW()")
        count = int(result.split()[-1]) if result else 0
        if count > 0:
            log.info("sessions.expired_cleaned", count=count)
        return count
