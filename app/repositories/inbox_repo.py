"""
Repository for webhook_inbox table.

All SQL uses positional $1/$2/... params (no f-strings for data).
"""
from __future__ import annotations

from typing import Any

import asyncpg

# Backoff schedule in seconds: attempt 1→2→3→4→5 delays
_BACKOFF_SECONDS = [30, 120, 600, 3600, 21600]


async def enqueue(
    pool: asyncpg.Pool,
    *,
    provider: str,
    external_id: str | None,
    payload: dict,
) -> bool:
    """
    Insert a new inbox row.

    Returns True if inserted, False if deduped (provider+external_id already exists).
    Uses a manual pre-check instead of ON CONFLICT ON CONSTRAINT because asyncpg
    does not support partial-index ON CONFLICT targets.  The SELECT + INSERT is
    safe for our write-rate; the unique index still protects against true races.
    """
    async with pool.acquire() as conn:
        if external_id is not None:
            # Use INSERT ... ON CONFLICT DO NOTHING to avoid TOCTOU race.
            # The partial unique index ux_webhook_inbox_dedup catches duplicates.
            result = await conn.execute(
                """
                INSERT INTO webhook_inbox (provider, external_id, payload)
                VALUES ($1, $2, $3)
                ON CONFLICT (provider, external_id) WHERE external_id IS NOT NULL
                DO NOTHING
                """,
                provider,
                external_id,
                payload,
            )
            # asyncpg returns e.g. "INSERT 0 1" (inserted) or "INSERT 0 0" (conflict)
            return result.endswith(" 1")

        await conn.execute(
            """
            INSERT INTO webhook_inbox (provider, external_id, payload)
            VALUES ($1, $2, $3)
            """,
            provider,
            external_id,
            payload,
        )
        return True


async def fetch_batch(
    conn: asyncpg.Connection,
    limit: int = 10,
) -> list[asyncpg.Record]:
    """
    Fetch up to *limit* pending rows, locking them for exclusive processing.
    Caller must hold an open transaction.
    """
    return await conn.fetch(
        """
        SELECT id, provider, payload, attempts
        FROM webhook_inbox
        WHERE processed_at IS NULL
          AND next_attempt_at <= NOW()
        ORDER BY id
        FOR UPDATE SKIP LOCKED
        LIMIT $1
        """,
        limit,
    )


async def mark_processed(conn: asyncpg.Connection, inbox_id: int) -> None:
    """Mark a row as successfully processed."""
    await conn.execute(
        "UPDATE webhook_inbox SET processed_at = NOW() WHERE id = $1",
        inbox_id,
    )


async def claim_rows(conn: asyncpg.Connection, row_ids: list[int]) -> None:
    """
    Atomically claim rows so no other worker can pick them up during dispatch.

    Sets next_attempt_at 3 minutes into the future and increments attempts by 1.
    Must be called inside an open transaction (together with fetch_batch).
    If the worker crashes after claiming but before ack, the row becomes visible
    again after the 3-minute window and will be retried.
    """
    await conn.execute(
        """
        UPDATE webhook_inbox
        SET next_attempt_at = NOW() + INTERVAL '3 minutes',
            attempts        = attempts + 1
        WHERE id = ANY($1::bigint[])
        """,
        row_ids,
    )


async def mark_failed(
    conn: asyncpg.Connection,
    inbox_id: int,
    error: str,
    attempts: int,
    *,
    already_incremented: bool = False,
) -> None:
    """
    Schedule the next retry with exponential backoff, or dead-letter the row.

    After _BACKOFF_SECONDS is exhausted, mark as DEAD_LETTER (processed_at set
    so it stops being polled, but the row is kept for auditing).

    ``attempts`` is the value *before* this failure unless ``already_incremented``
    is True (used by the claim-then-ack pattern where Phase 1 already bumped the
    counter).
    """
    new_attempts = attempts if already_incremented else attempts + 1

    if new_attempts > len(_BACKOFF_SECONDS):
        # Dead-letter: no more retries
        await conn.execute(
            """
            UPDATE webhook_inbox
            SET attempts    = $2,
                last_error  = $3,
                processed_at = NOW()
            WHERE id = $1
            """,
            inbox_id,
            new_attempts,
            f"DEAD_LETTER: {error}",
        )
    else:
        delay = _BACKOFF_SECONDS[new_attempts - 1]
        await conn.execute(
            """
            UPDATE webhook_inbox
            SET attempts        = $2,
                last_error      = $3,
                next_attempt_at = NOW() + ($4 || ' seconds')::INTERVAL
            WHERE id = $1
            """,
            inbox_id,
            new_attempts,
            error,
            str(delay),
        )
