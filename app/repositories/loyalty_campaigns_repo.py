"""
app/repositories/loyalty_campaigns_repo.py

Repository for loyalty_campaigns table.
Created as part of the Campaigns feature (Sprint Loyalty v2).

All functions require an active tenant_scope (org_id) in the call site.
Table: loyalty_campaigns (org_id FK → organizations, RLS org_isolation).

Status state machine:
  draft   → active   (launch)
  active  → paused   (pause)
  paused  → active   (resume)
  draft   → paused   REJECTED (409)
  active  → draft    REJECTED (409)
  paused  → draft    REJECTED (409)

revenue_numeric is NUMERIC(14,2) in the DB → Decimal in Python
→ float at the JSON boundary via quantize_money.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.services.logging import get_logger
from app.services.money import quantize_money
from app.services.tenant_db import tenant_connection

log = get_logger(__name__)

# ── Allowed status transitions ────────────────────────────────────────────────
# Mapping: current_status → set of valid next statuses
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "draft":  {"active"},
    "active": {"paused"},
    "paused": {"active"},
}


def _row_to_dict(row) -> dict:
    """Convert an asyncpg Record to a plain serialisable dict."""
    if row is None:
        return {}
    d: dict[str, Any] = {}
    for k in row.keys():
        v = row[k]
        if isinstance(v, datetime):
            d[k] = v.isoformat()
        elif isinstance(v, Decimal):
            d[k] = float(quantize_money(v))  # JSON boundary
        else:
            d[k] = v
    return d


# ── Repo functions ────────────────────────────────────────────────────────────

async def db_list_campaigns(status: str | None = None) -> list[dict]:
    """List campaigns for the active tenant, optionally filtered by status.

    # Requires active tenant_scope().
    """
    async with tenant_connection() as conn:
        if status:
            rows = await conn.fetch(
                """
                SELECT *
                FROM loyalty_campaigns
                ORDER BY created_at DESC
                """,
            )
            # Filter in Python — the RLS already scopes org_id; status param is
            # a simple column filter we add here (no user-controlled value in SQL).
            rows = [r for r in rows if r["status"] == status]
        else:
            rows = await conn.fetch(
                "SELECT * FROM loyalty_campaigns ORDER BY created_at DESC"
            )
    return [_row_to_dict(r) for r in rows]


async def db_get_campaign(campaign_id: str) -> dict | None:
    """Fetch a single campaign by UUID.  Returns None if not found.

    # Requires active tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM loyalty_campaigns WHERE id = $1",
            campaign_id,
        )
    return _row_to_dict(row) if row else None


async def db_create_campaign(
    *,
    org_id: int,
    name: str,
    description: str = "",
    segment: str | None = None,
    trigger_type: str,
    message_template: str,
) -> dict:
    """Insert a new campaign in 'draft' status.

    # Requires active tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO loyalty_campaigns
                (org_id, name, description, segment, trigger_type, message_template)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING *
            """,
            org_id,
            name,
            description,
            segment,
            trigger_type,
            message_template,
        )
    result = _row_to_dict(row)
    log.info(
        "loyalty_campaigns.created",
        campaign_id=result.get("id"),
        org_id=org_id,
        trigger_type=trigger_type,
    )
    return result


async def db_update_campaign(campaign_id: str, updates: dict) -> dict | None:
    """Apply a partial update to a campaign.

    Only the following fields are patchable:
      name, description, segment, trigger_type, message_template.

    Silently ignores unknown keys.  Returns None if the campaign is not found.

    # Requires active tenant_scope().
    """
    _PATCHABLE = {"name", "description", "segment", "trigger_type", "message_template"}
    filtered = {k: v for k, v in updates.items() if k in _PATCHABLE}
    if not filtered:
        # Nothing to update — return current state.
        return await db_get_campaign(campaign_id)

    # Build dynamic SET clause — safe: keys are whitelisted above, values go
    # through positional parameters.
    cols = list(filtered.keys())
    vals = [filtered[c] for c in cols]
    set_clause = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))
    # Always bump updated_at
    set_clause += f", updated_at = ${len(cols) + 2}"
    vals.append(datetime.now(tz=timezone.utc))

    sql = f"UPDATE loyalty_campaigns SET {set_clause} WHERE id = $1 RETURNING *"  # noqa: S608

    async with tenant_connection() as conn:
        row = await conn.fetchrow(sql, campaign_id, *vals)

    if row is None:
        return None
    result = _row_to_dict(row)
    log.info("loyalty_campaigns.updated", campaign_id=campaign_id, fields=cols)
    return result


async def db_delete_campaign(campaign_id: str) -> bool:
    """Delete a campaign by UUID.  Returns True if deleted, False if not found.

    # Requires active tenant_scope().
    """
    async with tenant_connection() as conn:
        result = await conn.execute(
            "DELETE FROM loyalty_campaigns WHERE id = $1",
            campaign_id,
        )
    deleted = result == "DELETE 1"
    if deleted:
        log.info("loyalty_campaigns.deleted", campaign_id=campaign_id)
    return deleted


async def db_toggle_campaign_status(
    campaign_id: str,
    new_status: str,
) -> dict | None:
    """Transition campaign to new_status, enforcing the state machine.

    Allowed transitions:
      draft   → active
      active  → paused
      paused  → active

    Raises ValueError (409 in routes) for invalid transitions.
    Returns None if the campaign is not found.

    # Requires active tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT id, status FROM loyalty_campaigns WHERE id = $1",
            campaign_id,
        )
        if row is None:
            return None

        current_status: str = row["status"]
        allowed = _ALLOWED_TRANSITIONS.get(current_status, set())
        if new_status not in allowed:
            raise ValueError(
                f"Invalid transition: {current_status!r} → {new_status!r}. "
                f"Allowed from '{current_status}': {sorted(allowed) or 'none'}."
            )

        updated_row = await conn.fetchrow(
            """
            UPDATE loyalty_campaigns
               SET status = $2, updated_at = $3
             WHERE id = $1
            RETURNING *
            """,
            campaign_id,
            new_status,
            datetime.now(tz=timezone.utc),
        )

    result = _row_to_dict(updated_row)
    log.info(
        "loyalty_campaigns.status_changed",
        campaign_id=campaign_id,
        from_status=current_status,
        to_status=new_status,
    )
    return result
