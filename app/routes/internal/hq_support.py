"""
Mesio HQ — support actions on one organization (PM 2026-10-02).

  GET  /api/internal/hq/orgs/{org_id}/sedes/{location_id}/support  → what can be fixed
  POST /api/internal/hq/support/{org_id}/close-sitting      {session_id, reason}
  POST /api/internal/hq/support/{org_id}/cancel-round       {order_id, reason}
  POST /api/internal/hq/support/{org_id}/cancel-web-order   {order_id, reason}
  POST /api/internal/hq/support/{org_id}/dismiss-alerts     {location_id, older_than_minutes, reason}
  POST /api/internal/hq/support/{org_id}/clear-sold-out     {location_id, dish_name|null, reason}
  POST /api/internal/hq/support/{org_id}/reask-ops          {location_id, reason}
  POST /api/internal/hq/support/{org_id}/unpause            {reason}
  POST /api/internal/hq/support/{org_id}/close-sessions     {username | staff_id, reason}
  POST /api/internal/hq/support/{org_id}/password-reset     {username, reason}

A closed set: nothing here edits a carta, an amount or a paid flag, and
Mesio never sets or sees a password (the reset sends the owner's own code to
their email). Every action needs a reason and writes one hq_audit_log row
(action "support.<name>", the org, the reason and what changed) — the
generic AuditMiddleware skips these paths so there is exactly one row.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from app.repositories import sessions_repo
from app.repositories.internal import hq_support_repo as support
from app.routes.deps import verify_superadmin
from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)

router = APIRouter(tags=["internal-hq-support"])

_REASON = Field(..., min_length=8, max_length=500, description="Por qué Mesio hace esto (queda en el audit log)")


class _Reason(BaseModel):
    reason: str = _REASON


class CloseSittingBody(_Reason):
    session_id: int


class OrderBody(_Reason):
    order_id: str = Field(..., min_length=1, max_length=100)


class SedeBody(_Reason):
    location_id: int


class DismissAlertsBody(SedeBody):
    older_than_minutes: int = Field(60, ge=0, le=60 * 24 * 30)


class ClearSoldOutBody(SedeBody):
    dish_name: str | None = Field(None, max_length=200)


class SessionsBody(_Reason):
    username: str | None = Field(None, max_length=200)
    staff_id: str | None = Field(None, max_length=64)

    @model_validator(mode="after")
    def _one_target(self):
        if bool(self.username) == bool(self.staff_id):
            raise ValueError("Indica username o staff_id (uno de los dos).")
        return self


class UserBody(_Reason):
    username: str = Field(..., min_length=3, max_length=200)


def _json_safe(value):
    if isinstance(value, Decimal):
        return float(value)  # JSON boundary
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


async def _audit(request: Request, org_id: int, action: str, reason: str, target_type: str,
                 target_id, details: dict) -> None:
    from app.repositories.internal.audit_log_repo import db_log_audit_event  # noqa: PLC0415
    await db_log_audit_event(
        actor="superadmin",
        action=f"support.{action}",
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        org_id=org_id,
        payload={"reason": reason, **_json_safe(details)},
        request_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    log.info("hq_support.action", action=action, org_id=org_id, target=str(target_id))


async def _sede(org_id: int, location_id: int) -> dict:
    loc = await support.db_location_of_org(org_id, location_id)
    if not loc:
        raise HTTPException(status_code=404, detail="Esa sede no es de esta organización.")
    return loc


def _scope(action: str):
    return bypass_tenant_scope(f"internal_hq_support: {action} on one organization")


@router.get("/api/internal/hq/orgs/{org_id}/sedes/{location_id}/support")
async def support_lists(org_id: int, location_id: int, _: None = Depends(verify_superadmin)):
    with _scope("read fixable items"):
        await _sede(org_id, location_id)
        lists = await support.db_support_lists(org_id, location_id)
    return _json_safe(lists)


@router.post("/api/internal/hq/support/{org_id}/close-sitting")
async def close_sitting(org_id: int, body: CloseSittingBody, request: Request, _: None = Depends(verify_superadmin)):
    with _scope("close a stuck sitting"):
        row = await support.db_close_sitting(org_id, body.session_id)
        if not row:
            raise HTTPException(status_code=404, detail="No hay una mesa abierta con ese id en esta organización.")
        await _audit(request, org_id, "close_sitting", body.reason, "table_session", row["id"], row)
    return {"ok": True, "closed": _json_safe(row)}


@router.post("/api/internal/hq/support/{org_id}/cancel-round")
async def cancel_round(org_id: int, body: OrderBody, request: Request, _: None = Depends(verify_superadmin)):
    with _scope("cancel a hung table round"):
        row = await support.db_cancel_round(org_id, body.order_id)
        if not row:
            raise HTTPException(status_code=404, detail="No hay una ronda abierta con ese id en esta organización.")
        await _audit(request, org_id, "cancel_round", body.reason, "table_order", row["id"], row)
    return {"ok": True, "cancelled": _json_safe(row)}


@router.post("/api/internal/hq/support/{org_id}/cancel-web-order")
async def cancel_web_order(org_id: int, body: OrderBody, request: Request, _: None = Depends(verify_superadmin)):
    with _scope("cancel a hung web order"):
        row = await support.db_cancel_web_order(org_id, body.order_id, f"Soporte Mesio: {body.reason}")
        if not row:
            raise HTTPException(status_code=404, detail="No hay un pedido web abierto con ese id en esta organización.")
        await _audit(request, org_id, "cancel_web_order", body.reason, "order", row["id"], row)
    return {"ok": True, "cancelled": _json_safe(row)}


@router.post("/api/internal/hq/support/{org_id}/dismiss-alerts")
async def dismiss_alerts(org_id: int, body: DismissAlertsBody, request: Request, _: None = Depends(verify_superadmin)):
    with _scope("dismiss old waiter alerts"):
        await _sede(org_id, body.location_id)
        n = await support.db_dismiss_alerts(org_id, body.location_id, body.older_than_minutes)
        await _audit(request, org_id, "dismiss_alerts", body.reason, "location", body.location_id,
                     {"dismissed": n, "older_than_minutes": body.older_than_minutes})
    return {"ok": True, "dismissed": n}


@router.post("/api/internal/hq/support/{org_id}/clear-sold-out")
async def clear_sold_out(org_id: int, body: ClearSoldOutBody, request: Request, _: None = Depends(verify_superadmin)):
    with _scope("clear sold-out dishes"):
        await _sede(org_id, body.location_id)
        n = await support.db_clear_sold_out(org_id, body.location_id, body.dish_name or None)
        await _audit(request, org_id, "clear_sold_out", body.reason, "location", body.location_id,
                     {"dish_name": body.dish_name or "(todos)", "cleared": n})
    return {"ok": True, "cleared": n}


@router.post("/api/internal/hq/support/{org_id}/reask-ops")
async def reask_ops(org_id: int, body: SedeBody, request: Request, _: None = Depends(verify_superadmin)):
    from app.repositories import ops_config_repo  # noqa: PLC0415
    from app.services import ops_config  # noqa: PLC0415
    with _scope("re-ask Configurar operación"):
        await _sede(org_id, body.location_id)
        current = ops_config.normalize(await ops_config_repo.db_get_ops_config(org_id, body.location_id) or {})
        # Keep the old answers as the wizard's starting point; only
        # `configured` goes back to false so the owner is asked again.
        await ops_config_repo.db_set_ops_config(org_id, body.location_id, {**current, "configured": False})
        await _audit(request, org_id, "reask_ops", body.reason, "location", body.location_id, {"previous": current})
    return {"ok": True}


@router.post("/api/internal/hq/support/{org_id}/unpause")
async def unpause(org_id: int, body: _Reason, request: Request, _: None = Depends(verify_superadmin)):
    from app.repositories import restaurant_repo  # noqa: PLC0415
    with _scope("unpause the restaurant"):
        org = await restaurant_repo.db_get_org_by_id(org_id)
        if not org:
            raise HTTPException(status_code=404, detail="Organización no encontrada")
        await restaurant_repo.db_merge_restaurant_features(org_id, {"bot_active": True})
        await _audit(request, org_id, "unpause", body.reason, "organization", org_id, {})
    return {"ok": True}


@router.post("/api/internal/hq/support/{org_id}/close-sessions")
async def close_sessions(org_id: int, body: SessionsBody, request: Request, _: None = Depends(verify_superadmin)):
    with _scope("close a user's sessions"):
        if body.username:
            user = await support.db_user_of_org(org_id, body.username)
            if not user:
                raise HTTPException(status_code=404, detail="Ese usuario no es de esta organización.")
            identity, target_type, target = user["username"], "user", user["username"]
        else:
            staff = await support.db_staff_of_org(org_id, body.staff_id)
            if not staff:
                raise HTTPException(status_code=404, detail="Ese empleado no es de esta organización.")
            identity, target_type, target = f"staff:{staff['id']}", "staff", staff["id"]
        closed = await sessions_repo.delete_sessions_for_user(identity)
        await _audit(request, org_id, "close_sessions", body.reason, target_type, target, {"sessions_closed": closed})
    return {"ok": True, "sessions_closed": closed}


@router.post("/api/internal/hq/support/{org_id}/password-reset")
async def password_reset(org_id: int, body: UserBody, request: Request, _: None = Depends(verify_superadmin)):
    """Send the user their own reset code by email — the same code and page
    as "¿Olvidaste tu contraseña?". Mesio never sets or sees the password."""
    from app.services import hq_support  # noqa: PLC0415
    with _scope("send a password reset code"):
        user = await support.db_user_of_org(org_id, body.username)
        if not user:
            raise HTTPException(status_code=404, detail="Ese usuario no es de esta organización.")
    if "@" not in user["username"]:
        raise HTTPException(status_code=422, detail="Ese usuario no tiene un email como usuario: no hay a dónde enviar el código.")
    sent = await hq_support.send_password_reset(user["username"], user.get("restaurant_name"))
    with _scope("audit a password reset code"):
        await _audit(request, org_id, "password_reset", body.reason, "user", user["username"], {"email_sent": sent})
    if not sent:
        raise HTTPException(status_code=502, detail="El código se generó pero el email no salió (revisa RESEND_API_KEY). Queda en el audit log.")
    return {"ok": True, "sent_to": user["username"]}
