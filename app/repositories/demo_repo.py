"""
app/repositories/demo_repo.py — Minimal helpers for the /demo live chat widget.

All DB access uses the demo org's tenant scope explicitly.
Global fallback: db_get_demo_org_id() caches the org_id in a module global
after the first lookup (cheap optimization — the demo org never changes).
"""
from __future__ import annotations

import asyncio

from app.services.logging import get_logger

log = get_logger(__name__)

# ── Module-level cache ────────────────────────────────────────────────────────

_demo_org_id: int | None = None
_demo_org_lock = asyncio.Lock()

DEMO_BOT_NUMBER = "demo:0"


async def db_get_demo_org_id() -> int | None:
    """Look up the demo org id by its canonical bot number.

    Caches the result in a module global after the first successful lookup.
    Returns None if the demo org has not been seeded yet (migration 0071 not run).
    """
    global _demo_org_id  # noqa: PLW0603

    if _demo_org_id is not None:
        return _demo_org_id

    async with _demo_org_lock:
        # Double-check under lock
        if _demo_org_id is not None:
            return _demo_org_id

        from app.services.database import get_pool, _serialize  # noqa: PLC0415

        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM organizations WHERE whatsapp_number = $1 LIMIT 1",
                DEMO_BOT_NUMBER,
            )

        if row is None:
            log.error(
                "demo_repo.demo_org_not_found",
                bot_number=DEMO_BOT_NUMBER,
                note="Run 'alembic upgrade head' to seed the Demo Mesio org (migration 0071).",
            )
            return None

        _demo_org_id = row["id"]
        log.info("demo_repo.demo_org_cached", org_id=_demo_org_id)
        return _demo_org_id


async def db_cleanup_demo_session(customer_phone: str, org_id: int) -> None:
    """Delete all conversation and cart rows for this demo customer.

    Called from POST /api/demo/reset to let the prospect start fresh.
    Must be called inside tenant_scope(org_id) by the route handler.
    """
    from app.services.tenant_db import tenant_connection  # noqa: PLC0415

    async with tenant_connection() as conn:
        # Postgres forbids aggregate functions inside RETURNING — wrap the
        # DELETE in a CTE and aggregate in the outer SELECT instead.
        deleted_convs = await conn.fetchval(
            """
            WITH deleted AS (
                DELETE FROM conversations WHERE phone = $1 RETURNING 1
            )
            SELECT COUNT(*) FROM deleted
            """,
            customer_phone,
        )
        deleted_carts = await conn.fetchval(
            """
            WITH deleted AS (
                DELETE FROM carts WHERE phone = $1 RETURNING 1
            )
            SELECT COUNT(*) FROM deleted
            """,
            customer_phone,
        )
        log.info(
            "demo_repo.session_cleanup",
            phone_prefix=customer_phone[:10],
            conversations_deleted=deleted_convs,
            carts_deleted=deleted_carts,
        )
