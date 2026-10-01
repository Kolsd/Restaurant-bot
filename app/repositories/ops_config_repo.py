"""
Per-sede operational setup (`locations.ops_config`, migration 0105).

Which screens a sede uses (bar, delivery, couriers, waiters) and which carta
categories go to the bar. The shape and defaults live in
app/services/ops_config.py; this module only reads and writes the JSONB.
"""
from __future__ import annotations

import json
from typing import Optional

from app.services.logging import get_logger
from app.services.tenant_db import tenant_connection

log = get_logger(__name__)


async def db_get_ops_config(org_id: int, location_id: int) -> Optional[dict]:
    """The raw ops_config of this sede ({} when never configured), or None
    when the location does not belong to org_id.

    # Requires active tenant_scope(org_id).
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT ops_config FROM locations WHERE id = $1 AND org_id = $2",
            location_id, org_id,
        )
    if not row:
        return None
    raw = row["ops_config"]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = {}
    return raw if isinstance(raw, dict) else {}


async def db_set_ops_config(org_id: int, location_id: int, config: dict) -> bool:
    """Overwrite this sede's ops_config. False when the location does not
    belong to org_id.

    # Requires active tenant_scope(org_id).
    """
    async with tenant_connection() as conn:
        result = await conn.execute(
            "UPDATE locations SET ops_config = $1::jsonb WHERE id = $2 AND org_id = $3",
            json.dumps(config), location_id, org_id,
        )
    ok = result.endswith(" 1")
    if ok:
        log.info("ops_config.saved", org_id=org_id, location_id=location_id)
    return ok
