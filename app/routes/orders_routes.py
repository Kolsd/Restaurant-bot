import hashlib
import hmac
import json as _json
import os
import httpx
import asyncio
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from app.services import database as db
from app.services.orders import cart_summary
from app.routes.deps import require_auth, get_current_restaurant, get_current_user
from app.services.agent import trigger_nps
from app.services.logging import get_logger
from app.services.money import to_decimal
from app.repositories import tables_repo as tr
from app.repositories.loyalty_repo import db_accrue_loyalty_points
from app.repositories.orders_repo import record_wompi_event
from app.services.tenant_context import tenant_scope, bypass_tenant_scope

log = get_logger(__name__)

META_API_VERSION = os.getenv("META_API_VERSION", "v20.0")

router = APIRouter()

# Sin contraseñas por defecto por seguridad
WOMPI_EVENTS_SECRET = os.getenv("WOMPI_EVENTS_SECRET")


@router.get("/orders")
async def list_orders(request: Request):
    await require_auth(request)
    # Tenant-scope — db_get_all_orders uses _tenant_connection() which requires
    # an active scope. Without this the route 500s with TenantNotSetError.
    restaurant = await get_current_restaurant(request)
    org_id = restaurant["id"]
    with tenant_scope(org_id):
        all_orders = await db.db_get_all_orders()
    paid = [o for o in all_orders if o["paid"]]
    total_revenue = sum(o["total"] for o in paid)
    return {
        "summary": {
            "total_orders": len(all_orders),
            "paid": len(paid),
            "pending_payment": len(all_orders) - len(paid),
            "total_revenue": total_revenue,
        },
        "orders": all_orders,
    }


@router.get("/orders/{order_id}")
async def get_single_order(request: Request, order_id: str):
    user = await get_current_user(request)
    # Pre-resolve order's tenant, then scope-check against the authenticated user.
    with bypass_tenant_scope("get_single_order: pre-resolve order tenant"):
        order = await db.db_get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    order_rid = order.get("restaurant_id")
    user_rid = user.get("branch_id") or user.get("restaurant_id")
    if order_rid and user_rid and int(order_rid) != int(user_rid):
        raise HTTPException(status_code=403, detail="La orden no pertenece a tu sucursal")
    return order

@router.get("/cart/{phone}/{bot_number}")
async def view_cart(request: Request, phone: str, bot_number: str):
    await require_auth(request)
    summary = await cart_summary(phone, bot_number)
    return {"summary": summary}

@router.post("/payment/wompi-webhook")
async def wompi_webhook(request: Request):
    if not WOMPI_EVENTS_SECRET:
        log.error("orders.wompi_secret_not_configured")
        raise HTTPException(status_code=500, detail="Configuración de pasarela de pagos incompleta")

    body_bytes = await request.body()
    body = _json.loads(body_bytes)
    signature_header = request.headers.get("x-event-checksum", "")

    expected_sig = hashlib.sha256(
        (body_bytes.decode() + WOMPI_EVENTS_SECRET).encode()
    ).hexdigest()

    # Bug 3 fix: timing-safe comparison to prevent timing-oracle attacks
    if not signature_header or not hmac.compare_digest(
        signature_header.encode(), expected_sig.encode()
    ):
        raise HTTPException(status_code=401, detail="Firma inválida")

    event = body.get("event", "")
    data = body.get("data", {})

    # Wompi webhooks are cross-tenant — no restaurant JWT present.
    # bypass_tenant_scope allows migrated repos to execute without a pinned tenant.
    with bypass_tenant_scope("wompi_webhook_cross_tenant"):
        if event == "transaction.updated":
            # ── Idempotency guard (Phase 2.2) ──────────────────────────────────
            # Extract the canonical Wompi event_id early, before touching any
            # order state.  Wompi retries on timeout → same event_id arrives
            # again; record_wompi_event returns False on replay.
            transaction_data_raw = data.get("transaction", {})
            event_id = transaction_data_raw.get("id")
            if not event_id:
                log.error(
                    "wompi.webhook.missing_event_id",
                    wompi_event_type=event,
                    hint="payload did not contain data.transaction.id",
                )
                raise HTTPException(status_code=400, detail="Payload inválido: falta data.transaction.id")

            is_first = await record_wompi_event(
                event_id,
                transaction_id=event_id,  # Wompi uses the same UUID as the transaction id
                order_id=None,            # order_id not yet resolved at this point
                status=transaction_data_raw.get("status", ""),
            )
            if not is_first:
                log.info(
                    "wompi.duplicate_event_ignored",
                    event_id=event_id,
                )
                return {"status": "already_processed"}
            # ── End idempotency guard ───────────────────────────────────────────

            transaction = data.get("transaction", {})
            tx_status = transaction.get("status", "")
            reference = transaction.get("reference", "")
            transaction_id = transaction.get("id")

            # Bug 8 fix: handle non-APPROVED terminal statuses explicitly.
            # Do not silently ignore DECLINED / VOIDED / ERROR.
            if tx_status in ("DECLINED", "ERROR", "VOIDED"):
                log.warning(
                    "wompi.transaction.non_approved",
                    tx_status=tx_status,
                    reference=reference,
                    transaction_id=transaction_id,
                )
                # Bug 8: special-case VOIDED after an already-paid order
                if tx_status == "VOIDED" and reference and not reference.startswith("dep_"):
                    existing = await db.db_get_order(reference)
                    if existing and existing.get("paid"):
                        log.error(
                            "wompi.transaction.voided_after_paid",
                            reference=reference,
                            transaction_id=transaction_id,
                            action="manual_intervention_required",
                        )
                return {"status": "ok"}

            if tx_status == "PENDING":
                log.info(
                    "wompi.transaction.pending",
                    reference=reference,
                    transaction_id=transaction_id,
                )
                return {"status": "ok"}

            if tx_status == "APPROVED" and reference:
                # Reservation deposit references are prefixed with "dep_"
                if reference.startswith("dep_"):
                    from app.services.reservation_payments import confirm_deposit_payment  # noqa: PLC0415
                    confirmed = await confirm_deposit_payment(reference, transaction_id)
                    if not confirmed:
                        # Bug 5 fix: orphaned deposit — log warning, still return 200
                        # (Wompi must not retry; this requires manual audit)
                        log.warning(
                            "wompi.deposit.orphaned",
                            reference=reference,
                            transaction_id=transaction_id,
                        )
                    return {"status": "ok"}

                result = await db.db_confirm_payment(reference, transaction_id)

                # Bug 2 fix: handle idempotent no-op (already paid / cancelled order)
                if result is None:
                    log.info(
                        "wompi.webhook.noop",
                        reference=reference,
                        transaction_id=transaction_id,
                        reason="already_paid_or_cancelled",
                    )
                    return {"status": "ok"}

                # Bug 12 fix: do not log full result — it may contain PII fields.
                # Only log safe, non-PII identifiers.
                log.info(
                    "orders.payment_confirmed",
                    reference=reference,
                    total=str(result.get("total", "")),
                )

                # Bug 1 fix: call loyalty_repo directly instead of dead loyalty_svc.accrue_on_order.
                # Bug 10 fix: pass total as Decimal, not float.
                # Bug 14 (RLS): loyalty_repo is tenant-scoped — enter tenant_scope after resolving
                # restaurant_id from the confirmed order.
                restaurant_id = result.get("restaurant_id")
                if not restaurant_id:
                    # Fallback: resolve via bot_number
                    bot_number = result.get("bot_number", "")
                    if bot_number:
                        rest = await db.db_get_restaurant_by_bot_number(bot_number)
                        restaurant_id = rest["id"] if rest else None

                if restaurant_id:
                    async def _accrue_loyalty(rid: int, res: dict) -> None:
                        try:
                            with tenant_scope(rid):
                                pts = await db_accrue_loyalty_points(
                                    restaurant_id=rid,
                                    phone=res.get("phone", ""),
                                    order_id=reference,
                                    total_cop=to_decimal(res.get("total", 0)),  # Bug 10: Decimal, not float
                                )
                                if pts:
                                    log.info(
                                        "orders.loyalty_accrued",
                                        reference=reference,
                                        points=pts,
                                    )
                        except Exception:
                            log.exception(
                                "orders.loyalty_accrue_failed",
                                reference=reference,
                                restaurant_id=rid,
                            )

                    asyncio.create_task(_accrue_loyalty(restaurant_id, result))
                else:
                    log.warning(
                        "orders.loyalty_skipped_no_restaurant",
                        reference=reference,
                    )

        return {"status": "ok"}

@router.get("/payment/confirm")
async def payment_confirm(request: Request):
    params = dict(request.query_params)
    order_id = params.get("id", "")
    status = params.get("status", "")
    if order_id:
        with bypass_tenant_scope("payment_confirm_return_url: pre-resolve order tenant (public Wompi return URL)"):
            order = await db.db_get_order(order_id)
    else:
        order = None

    if status == "APPROVED" and order:
        return {
            "message": "Payment successful",
            "order_id": order_id,
            "total": order['total'],
            "status": "Your order is being prepared"
        }
    return {
        "message": "Payment not completed",
        "order_id": order_id,
        "status": status
    }

class UpdateOrderStatusRequest(BaseModel):
    status: str

# --- FUNCIONES Y ENDPOINTS DEL DOMICILIARIO ---

async def send_delivery_notification(phone: str, status: str, bot_number: str = "", order_type: str = "domicilio"):
    """Envía un mensaje automático de WhatsApp según el estado del pedido"""
    log.info("orders.delivery_notification", status=status, phone=phone, bot_number=bot_number)

    # Fetch restaurant credentials first (restaurant-specific phone_id takes priority)
    rest_name = ""
    rest_phone_id = ""
    rest_token = ""
    if bot_number:
        try:
            rest = await db.db_get_restaurant_by_bot_number(bot_number)
            if rest:
                rest_name = rest.get("name", "")
                rest_phone_id = rest.get("wa_phone_id", "") or ""
                rest_token = rest.get("wa_access_token", "") or ""
                log.info("orders.restaurant_resolved", name=rest_name, has_phone_id=bool(rest_phone_id), has_token=bool(rest_token))

                # Wave-2: if branch lacks credentials, fall back to the org's primary location.
                if (not rest_phone_id or not rest_token) and rest.get("org_id"):
                    from app.repositories.restaurant_repo import db_get_default_location  # noqa: PLC0415
                    primary = await db_get_default_location(rest["org_id"])
                    parent = (
                        await db.db_get_restaurant_by_id(primary["id"])
                        if primary and primary.get("id") and primary.get("id") != rest.get("location_id")
                        else None
                    )
                    if parent:
                        if not rest_phone_id:
                            rest_phone_id = parent.get("wa_phone_id", "") or ""
                        if not rest_token:
                            rest_token = parent.get("wa_access_token", "") or ""
                        if not rest_name:
                            rest_name = parent.get("name", "")
                        log.info("orders.restaurant_credentials_inherited", has_phone_id=bool(rest_phone_id), has_token=bool(rest_token))
            else:
                log.warning("orders.restaurant_not_found", bot_number=bot_number)
        except Exception as e:
            log.error("orders.restaurant_lookup_failed", bot_number=bot_number, error=str(e))

    token = rest_token or os.getenv("META_ACCESS_TOKEN") or os.getenv("WHATSAPP_TOKEN", "")
    phone_id = rest_phone_id or os.getenv("META_PHONE_NUMBER_ID", "")

    has_credentials = bool(token and phone_id)
    log.info("orders.delivery_credentials_check", has_token=bool(token), has_phone_id=bool(phone_id))

    is_pickup = order_type == "recoger"

    # Statuses válidos según tipo de orden
    if is_pickup and status not in ('listo', 'entregado'):
        return  # Para recoger solo notificamos cuando está listo y cuando se recoge
    if not is_pickup and status not in ('en_camino', 'en_puerta', 'entregado'):
        return  # Para domicilio notificamos despacho, llegada y entrega

    clean_phone = phone.replace("+", "").replace(" ", "")

    if has_credentials:
        if is_pickup:
            if status == 'listo':
                msg = "✅ *¡Tu pedido está listo!*\n\nPasa a recogerte cuando quieras. Te esperamos. 🛍️"
            else:  # entregado
                msg = "✅ *¡Pedido recogido!*\n\nEsperamos que lo disfrutes muchísimo. ¡Gracias por elegirnos y buen provecho! 🌟"
        else:
            if status == 'en_camino':
                msg = "🛵 *¡Buenas noticias!*\n\nNuestro domiciliario acaba de salir del restaurante con tu pedido. ¡Ve preparando la mesa! 🍔"
            elif status == 'en_puerta':
                msg = "📍 *¡El domiciliario está en la puerta!*\n\n¡Ya casi llega tu pedido! Por favor ten listo el pago si aplica. 🏠"
            else:  # entregado
                msg = "✅ *¡Pedido Entregado!*\n\nEsperamos que lo disfrutes muchísimo. ¡Gracias por elegirnos y buen provecho! 🌟"

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                res = await client.post(
                    f"https://graph.facebook.com/{META_API_VERSION}/{phone_id}/messages",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "messaging_product": "whatsapp",
                        "to": clean_phone,
                        "type": "text",
                        "text": {"body": msg}
                    }
                )
                if res.status_code == 200:
                    log.info("orders.delivery_notification_sent", phone=clean_phone, status=status)
                else:
                    log.error("orders.delivery_notification_rejected", phone=clean_phone, status=res.status_code, body=res.text[:200])
        except Exception as e:
            log.error("orders.delivery_notification_failed", phone=clean_phone, error=str(e))

        # For entregado: send NPS question as a second message so the customer
        # doesn't have to reply first (they rarely would after a delivery ends).
        if status == 'entregado':
            try:
                from app.services.meta_api import send_text as _meta_send_text  # noqa: PLC0415
                nps_name = f" de *{rest_name}*" if rest_name else ""
                nps_msg = (
                    f"⭐ ¿Cómo calificarías tu pedido{nps_name}?\n"
                    f"Responde con un número del *1 al 5*\n"
                    f"_(1 = Muy mala · 5 = Excelente)_"
                )
                await _meta_send_text(
                    bot_number=bot_number,
                    access_token=token,
                    phone=clean_phone,
                    text=nps_msg,
                    phone_id=phone_id,
                )
                log.info("orders.nps_question_sent", phone=clean_phone)
            except Exception as _nps_e:
                log.error("orders.nps_question_failed", phone=clean_phone, error=str(_nps_e))
    else:
        log.warning("orders.delivery_notification_no_credentials", phone=phone, bot_number=bot_number, has_token=bool(token), has_phone_id=bool(phone_id))

    # NPS state is set by the caller (update_delivery_status → trigger_nps).


@router.get("/delivery/check-updates")
async def check_delivery_updates(request: Request):
    restaurant = await get_current_restaurant(request)
    with tenant_scope(restaurant["id"]):
        rows = await tr.db_get_delivery_status_hash_for_restaurant(restaurant["id"])
    current_state_hash = "".join([f"{r['id']}{r['status']}" for r in rows])
    return {"hash": current_state_hash}

@router.get("/delivery/orders")
async def get_delivery_orders(request: Request):
    restaurant = await get_current_restaurant(request)
    with tenant_scope(restaurant["id"]):
        raw = await db.db_get_delivery_orders(
            ['pendiente', 'confirmado', 'en_preparacion', 'listo', 'en_camino', 'en_puerta', 'entregado'],
            restaurant_id=restaurant["id"]
        )
    return {"orders": raw}

class ValidateDeliveryBody(BaseModel):
    """Body for manual proof validation from Caja.

    `proof_media_id` is informational — caja staff has already inspected the
    photo in the UI by the time they hit this endpoint; we only log it for
    audit. `notes` attaches a free-text justification (optional).
    """
    proof_media_id: str | None = None
    notes: str | None = None


@router.post("/delivery/orders/{order_id}/validate")
async def validate_delivery_order(order_id: str, body: ValidateDeliveryBody, request: Request):
    """
    Caja-initiated manual validation of a delivery/pickup order's payment proof.

    Mirror of the Wompi callback path but triggered by a human operator. Used
    when the customer paid via nequi / transferencia / daviplata and uploaded
    a screenshot — the operator visually confirms the proof then clicks
    "Validar" in the Comprobantes tab.

    Idempotent: second call for an already-paid order returns the existing
    confirmation. Loyalty accrual fires once (handled by db_confirm_payment
    returning None on replay).
    """
    from datetime import datetime as _dt
    user = await get_current_user(request)

    # 1. Pre-resolve order (cross-tenant) to obtain its org_id.
    with bypass_tenant_scope("validate_delivery_order: pre-resolve order tenant"):
        order = await db.db_get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Orden no encontrada")

    if order.get("paid"):
        # Already validated — return current state instead of erroring. Caja may
        # double-click; this must be safe.
        return {
            "success": True,
            "already_paid": True,
            "order_id": order_id,
            "status": order.get("status"),
        }

    if order.get("status") in ("cancelado", "entregado"):
        raise HTTPException(
            status_code=409,
            detail=f"La orden está en estado '{order.get('status')}' y no se puede validar.",
        )

    # 2. Ownership check (org-level, same pattern as update_delivery_status).
    order_rid = order.get("org_id") or order.get("restaurant_id")
    user_rid = user.get("branch_id") or user.get("restaurant_id")
    if not order_rid or not user_rid:
        raise HTTPException(status_code=403, detail="No se puede resolver la pertenencia de la orden")
    if order_rid and user_rid:
        with bypass_tenant_scope("validate_delivery_order: resolve user org_id"):
            user_rest = await db.db_get_restaurant_by_id(int(user_rid))
        user_org_id = (user_rest or {}).get("org_id") or user_rid
        order_org_id = int(order.get("org_id") or order_rid)
        if int(user_org_id) != order_org_id:
            raise HTTPException(status_code=403, detail="La orden no pertenece a tu sucursal")

    scope_rid = int(order_rid) if order_rid else None
    if not scope_rid:
        raise HTTPException(status_code=500, detail="No se pudo resolver tenant de la orden")

    # 3. Mark paid + confirmed. Synthetic transaction_id lets us distinguish
    #    manual validations from Wompi callbacks in audit logs and reports.
    manual_tx_id = f"manual:{user.get('id', 'unknown')}:{_dt.utcnow().isoformat()}Z"

    with tenant_scope(scope_rid):
        result = await db.db_confirm_payment(order_id, manual_tx_id)

    if result is None:
        # Race: another worker / second click beat us to it. Still a success
        # from caja's perspective.
        log.info(
            "orders.validate_delivery.noop",
            order_id=order_id,
            user_id=user.get("id"),
            reason="already_paid_or_terminal",
        )
        return {
            "success": True,
            "already_paid": True,
            "order_id": order_id,
        }

    log.info(
        "orders.validate_delivery.confirmed",
        order_id=order_id,
        user_id=user.get("id"),
        proof_media_id=body.proof_media_id,
        notes=(body.notes or "")[:200],
        transaction_id=manual_tx_id,
    )

    # 4. Loyalty accrual (best-effort, same pattern as Wompi webhook).
    try:
        phone = result.get("phone", "")
        total = result.get("total", 0)
        if scope_rid and phone:
            async def _accrue_loyalty_manual():
                try:
                    with tenant_scope(scope_rid):
                        await db_accrue_loyalty_points(
                            restaurant_id=scope_rid,
                            phone=phone,
                            order_id=order_id,
                            total_cop=to_decimal(total),
                        )
                except Exception:
                    log.exception(
                        "orders.validate_delivery.loyalty_failed",
                        order_id=order_id,
                    )
            asyncio.create_task(_accrue_loyalty_manual())
    except Exception:
        log.exception("orders.validate_delivery.loyalty_schedule_failed", order_id=order_id)

    # 5. Notify the customer asynchronously — same helper kitchen uses on status transitions.
    bot_number = result.get("bot_number", "")
    if bot_number:
        asyncio.create_task(send_delivery_notification(
            result.get("phone", ""), "confirmado", bot_number, result.get("order_type", "domicilio"),
        ))

    return {
        "success": True,
        "order_id": order_id,
        "status": "confirmado",
        "transaction_id": manual_tx_id,
    }


class SetEtaRequest(BaseModel):
    """Body for /api/delivery/orders/{order_id}/eta.

    `minutes` is the admin-entered ETA (1..180). Outside that range we 422.
    Sub-1 makes no sense; >180 is almost always a typo (3+ hours).
    """
    minutes: int


@router.post("/delivery/orders/{order_id}/eta")
async def set_delivery_eta(order_id: str, body: SetEtaRequest, request: Request):
    """Set or update the ETA for a delivery/pickup order.

    Admin/owner only — caja staff sees the input on the proposal card and
    submits it. The scheduler picks the order up on its next sweep and
    WhatsApps the customer "Tu pedido llega en aproximadamente N min."

    Resetting the flag (handled in db_set_order_eta) lets admins update an
    in-flight ETA — the scheduler will re-send with the new number.

    Owner check: the route uses get_current_restaurant which already enforces
    that the calling JWT belongs to a registered restaurant; we then verify
    the order tenant matches before mutating.
    """
    minutes = body.minutes
    if not isinstance(minutes, int) or minutes < 1 or minutes > 180:
        raise HTTPException(
            status_code=422,
            detail=f"minutes must be an integer 1..180, got {minutes!r}",
        )

    restaurant = await get_current_restaurant(request)
    user_org = restaurant.get("org_id") or restaurant.get("id")
    if not user_org:
        raise HTTPException(status_code=403, detail="No se pudo resolver tu organización")

    # Pre-resolve the order tenant before we enter scope.
    with bypass_tenant_scope("set_delivery_eta: pre-resolve order tenant"):
        order = await db.db_get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Orden no encontrada")

    order_org = order.get("org_id") or order.get("restaurant_id")
    if order_org and int(order_org) != int(user_org):
        raise HTTPException(status_code=403, detail="La orden no pertenece a tu organización")

    if order.get("status") in ("cancelado", "entregado"):
        raise HTTPException(
            status_code=409,
            detail=f"La orden está en estado '{order.get('status')}' y ya no admite ETA.",
        )

    with tenant_scope(int(order_org or user_org)):
        updated = await db.db_set_order_eta(order_id, minutes)

    if not updated:
        raise HTTPException(status_code=404, detail="Orden no encontrada")

    log.info(
        "orders.eta_set",
        order_id=order_id,
        minutes=minutes,
        org_id=int(order_org or user_org),
    )
    return {
        "success": True,
        "order_id": order_id,
        "estimated_minutes": minutes,
        "eta_communicated": False,
    }


@router.patch("/delivery/orders/{order_id}/status")
async def update_delivery_status(order_id: str, req: UpdateOrderStatusRequest, request: Request):
    user = await get_current_user(request)

    # 1. Buscamos el pedido original en la base de datos para obtener el número del cliente.
    #    Bypass to resolve order → restaurant_id first; then re-enter tenant_scope for the update.
    with bypass_tenant_scope("update_delivery_status: pre-resolve order tenant before scope"):
        order = await db.db_get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Orden no encontrada")

    # Ownership check: staff user can only touch orders of their own org.
    # Wave-2: orders carry org_id; user resolves to org_id via db_get_restaurant_by_id.
    order_rid = order.get("org_id") or order.get("restaurant_id")
    user_rid = user.get("branch_id") or user.get("restaurant_id")
    if order_rid and user_rid and int(order_rid) != int(user_rid):
        # Resolve the user's org_id (location_id may differ from org_id in Wave-2)
        with bypass_tenant_scope("update_delivery_status: check org ownership"):
            user_rest = await db.db_get_restaurant_by_id(int(user_rid))
        user_org_id = (user_rest or {}).get("org_id") or user_rid
        order_org_id = int(order.get("org_id") or order_rid)
        if int(user_org_id) != order_org_id:
            raise HTTPException(status_code=403, detail="La orden no pertenece a tu sucursal")

    scope_rid = int(order_rid) if order_rid else int(user_rid) if user_rid else None
    if not scope_rid:
        raise HTTPException(status_code=500, detail="No se pudo resolver tenant de la orden")

    # 2. Actualizamos el estado dentro del scope del tenant — db_update_order_status
    #    returns None on no-op (status already set or order modified concurrently). Return 409
    #    so the frontend can inform the operator instead of silently ignoring the race.
    with tenant_scope(scope_rid):
        updated = await db.db_update_order_status(order_id, req.status)
    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="La orden ya tiene ese estado o fue modificada por otro usuario",
        )

    # 3. For "entregado": trigger NPS synchronously (await) so state_store is
    #    populated BEFORE the PATCH response returns.  This eliminates the race
    #    where asyncio.create_task delays the nps_set past the customer's next
    #    inbound message (which would cause _try_nps_active_flow to return None).
    bot_number_for_nps = order.get("bot_number", "")
    if req.status == "entregado" and bot_number_for_nps:
        try:
            # Resolve restaurant name for NPS prompt (best-effort; empty string is fine)
            with bypass_tenant_scope("update_delivery_status: resolve restaurant name for NPS"):
                _rest_for_nps = await db.db_get_restaurant_by_bot_number(bot_number_for_nps)
            _rest_name_for_nps = (_rest_for_nps or {}).get("name", "")
            await trigger_nps(order["phone"], bot_number_for_nps, _rest_name_for_nps)
            log.info("orders.nps_triggered_post_delivery", phone=order["phone"])
        except Exception as _nps_exc:
            log.error("orders.nps_trigger_failed", phone=order["phone"], error=str(_nps_exc))

    # 4. Disparamos el mensaje de WhatsApp en SEGUNDO PLANO
    order_type = order.get("order_type", "domicilio")
    notify_statuses = ['listo', 'en_camino', 'en_puerta', 'entregado']
    if req.status in notify_statuses:
        asyncio.create_task(send_delivery_notification(
            order["phone"], req.status, order.get("bot_number", ""), order_type
        ))

    return {"success": True, "new_status": req.status}