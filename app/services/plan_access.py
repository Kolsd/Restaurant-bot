"""What an org's plan lets it use right now — the async side of plans.py.

Routes, the bot and the staff roster ask here instead of reading plan_code
themselves, so the trial rule (free days give at least the Restaurante plan)
lives in one place — and route tests can stub the plan without touching how
the caller's tenant is resolved.
"""

from fastapi import HTTPException

from app.services import database as db
from app.services import plans


async def org_plan_row(org_id: int) -> dict:
    """The org row the plan rules read (plan_code, comp_until); {} if missing."""
    return await db.db_get_org_by_id(org_id) or {}


async def org_has_feature(org_id: int, feature: str) -> bool:
    """Whether the org's plan (trial included) unlocks `feature` today.

    An org that cannot be found unlocks nothing.
    """
    org = await org_plan_row(org_id)
    return bool(org) and plans.has_feature(org, feature)


async def org_staff_cap(org_id: int) -> int | None:
    """Active staff users allowed per sede today; None means no cap."""
    return plans.staff_cap(await org_plan_row(org_id))


async def org_is_open(org_id: int) -> bool:
    """Whether diners can order from the org — false once it is suspendido
    (trial over with no payment, or a payment more than the grace late)."""
    return plans.is_open(await org_plan_row(org_id))


async def require_feature(org_id: int, feature: str) -> None:
    """403 with the upgrade copy when the org's plan lacks `feature`."""
    if not await org_has_feature(org_id, feature):
        raise HTTPException(status_code=403, detail=plans.upgrade_message(feature))
