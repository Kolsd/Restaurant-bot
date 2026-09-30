"""
app/repositories/onboarding_repo.py

The counts behind the owner's setup checklist.

One query per fact, all inside a single tenant connection, all scoped by
org (and by sede where the thing belongs to a sede). Nothing here is
resolved through the old WhatsApp key: an org created by self-serve signup has no
WhatsApp number, and a checklist that told such a restaurant it had no
tables because it had no phone would be worse than no checklist at all.

Read-only and best-effort by design — the caller renders a checklist, so a
count that cannot be read is shown as "not done yet" rather than failing
the page.
"""

from __future__ import annotations

from app.services.logging import get_logger
from app.services.tenant_db import tenant_connection

log = get_logger(__name__)


async def db_onboarding_counts(org_id: int, location_id: int | None = None) -> dict:
    """Return {"tables": int, "staff": int, "orders": int} for this org.

    `location_id` narrows the table count to one sede — a chain's second
    sede has its own tables to set up and its own codes to print, so the
    checklist is per sede for anything that lives on a sede.

    The order count is org-wide on purpose: the milestone being marked is
    "this restaurant has served someone through Mesio", which happens once.

    # Requires active tenant_scope(org_id) or bypass_tenant_scope().
    """
    counts = {"tables": 0, "staff": 0, "orders": 0}
    async with tenant_connection() as conn:
        if location_id is not None:
            counts["tables"] = int(await conn.fetchval(
                """SELECT COUNT(*) FROM restaurant_tables
                   WHERE org_id = $1 AND location_id = $2 AND active = TRUE""",
                org_id, location_id,
            ) or 0)
        else:
            counts["tables"] = int(await conn.fetchval(
                "SELECT COUNT(*) FROM restaurant_tables WHERE org_id = $1 AND active = TRUE",
                org_id,
            ) or 0)

        counts["staff"] = int(await conn.fetchval(
            "SELECT COUNT(*) FROM staff WHERE org_id = $1",
            org_id,
        ) or 0)

        # Both order tables count: a restaurant's first sale through Mesio
        # is a milestone whether it arrived from a table's QR (table_orders)
        # or from the web ordering link (orders).
        counts["orders"] = int(await conn.fetchval(
            """SELECT (SELECT COUNT(*) FROM table_orders WHERE org_id = $1)
                    + (SELECT COUNT(*) FROM orders       WHERE org_id = $1)""",
            org_id,
        ) or 0)

    return counts
