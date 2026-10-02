import hashlib
import hmac
import json as _json
import os
from fastapi import APIRouter, Request, HTTPException
from app.services import database as db
from app.services.orders import cart_summary
from app.routes.deps import require_auth, get_current_restaurant, get_current_user, resolve_sede_filter
from app.services.logging import get_logger
from app.repositories.orders_repo import record_wompi_event
from app.services.tenant_context import tenant_scope, bypass_tenant_scope

log = get_logger(__name__)

router = APIRouter()

# No default passwords, for security
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
    # Read inside the caller's own org (orders has FORCE RLS). This used to
    # read under bypass and compare order["restaurant_id"] — a column orders
    # no longer has — so the check never ran and any login could read any
    # restaurant's order, customer phone and address included.
    org_id = int(user.get("org_id") or 0)
    if not org_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    sede = resolve_sede_filter(request, user)
    with tenant_scope(org_id):
        order = await db.db_get_order(order_id)
    if not order or int(order.get("org_id") or 0) != org_id:
        raise HTTPException(status_code=404, detail="Order not found")
    if sede is not None and order.get("location_id") is not None and int(order["location_id"]) != int(sede):
        raise HTTPException(status_code=404, detail="Order not found")
    return order

@router.post("/payment/wompi-webhook")
async def wompi_webhook(request: Request):
    # H1: Global rate limit 50 req/s. Return 200 (not 429) — Wompi retries on
    # any non-2xx, so a 429 would create a retry flood. 200 silently drops the
    # excess and lets the legitimate spike drain naturally.
    from app.services import state_store as _ss  # noqa: PLC0415
    allowed = await _ss.rate_limit_check("wompi_webhook_global", max_requests=50, window_seconds=1)
    if not allowed:
        log.warning("wompi.webhook.rate_limited_global")
        return {"status": "ok"}  # 200 to prevent Wompi retry storm

    if not WOMPI_EVENTS_SECRET:
        log.error("orders.wompi_secret_not_configured")
        raise HTTPException(status_code=500, detail="Configuración de pasarela de pagos incompleta")

    body_bytes = await request.body()
    body = _json.loads(body_bytes)
    signature_header = request.headers.get("x-event-checksum", "")

    expected_sig = hashlib.sha256(
        (body_bytes.decode() + WOMPI_EVENTS_SECRET).encode()
    ).hexdigest()

    # H2: Return 200 (not 401) on invalid signature — a 401 causes Wompi to
    # retry indefinitely. Return 200 with status=invalid_signature so the
    # rejection is visible in logs but Wompi stops retrying.
    # Bug 3 fix: timing-safe comparison to prevent timing-oracle attacks.
    if not signature_header or not hmac.compare_digest(
        signature_header.encode(), expected_sig.encode()
    ):
        log.warning("wompi.webhook.invalid_signature", has_header=bool(signature_header))
        return {"status": "invalid_signature"}  # 200 to suppress Wompi retries

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

    # H4: Public unauthenticated return URL from Wompi — never expose financial
    # fields (total, phone, items). Return only {order_id, status, message}.
    if status == "APPROVED" and order:
        return {
            "order_id": order_id,
            "status": "approved",
            "message": "Payment successful. Your order is being prepared.",
        }
    return {
        "order_id": order_id,
        "status": status.lower() if status else "pending",
        "message": "Payment not completed.",
    }
