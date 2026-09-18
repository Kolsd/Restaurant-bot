"""
app/routes/diner_delivery.py
=============================
Public entry point + sede assignment for the delivery/pickup web wave
(docs/claude/delivery-web.md, chunk 2 — BACKEND ONLY, no HTML/JS here).

Split out of app/routes/diner.py (already 1300+ lines covering the dine-in
chat surface) rather than added to it — these two endpoints are entry-point
concerns (resolve an org from a public slug, assign a sede) that run BEFORE
any diner_sessions token exists, unlike everything in diner.py which already
assumes a session. Shares diner.py's `_client_ip` / `_features_dict` helpers
(imported, not re-implemented) and the same tenant-resolution convention
used throughout this wave: the org is resolved from an untrusted public
value (the slug) via delivery_repo.db_get_org_by_slug(), which itself enters
bypass_tenant_scope() internally — callers here never nest a second bypass
around it (TenantContextConflict) — then every subsequent read enters
tenant_scope(org_id) explicitly.

    GET  /api/diner/org/{slug}          — public org info for the ordering
                                           page: name, currency, whether
                                           delivery/pickup are on anywhere in
                                           the org, and the sede list (name,
                                           address, phone, open now). No
                                           WhatsApp fields, no features blob,
                                           no staff data.
    POST /api/diner/order-mode/resolve  — runs the sede-assignment ladder
                                           (app/services/delivery.resolve_order_mode)
                                           and returns either an assigned
                                           delivery sede or a pickup fallback
                                           with an ordered candidate list and
                                           a machine-readable reason.

Both routes are PUBLIC (no auth) — same security posture as diner.py
(CLAUDE.md "Diner traffic is UNAUTHENTICATED PUBLIC input"). Rate-limited
via state_store.rate_limit_check (Redis, cross-worker — never a module-level
counter), per IP always and per `device_token` when the client supplies one
(there is no diner_sessions token yet at this point in the flow — the
frontend is expected to generate and persist its own opaque device token in
localStorage before calling either of these, mirroring how a diner-web
identity is opaque everywhere else in this codebase).
"""
from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field, field_validator

from app.repositories import delivery_repo, diner_sessions_repo
from app.repositories.orders_repo import InsufficientStockError
from app.routes.diner import _checkout_amount_label, _client_ip, _features_dict
from app.services import database as db
from app.services import delivery
from app.services import orders
from app.services import realtime
from app.services import state_store
from app.services import turnstile
from app.services.logging import get_logger
from app.services.money import money_sum, quantize_money, to_decimal
from app.services.tenant_context import tenant_scope

log = get_logger(__name__)

router = APIRouter(prefix="/api/diner", tags=["diner-delivery"])

_VALID_MODES = ("delivery", "pickup")

# Anti-abuse cap — docs/claude/delivery-web.md chunk 3: "a cap on
# simultaneously OPEN orders per customer phone".
_MAX_OPEN_ORDERS_PER_PHONE = 3

_MAX_PROOF_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 MB — mirrors image_host._MAX_PROOF_BYTES

_CASH_METHOD_KEYS = frozenset({"efectivo", "cash"})

# Order-type/channel vocabulary REUSED verbatim from app/services/orders.py —
# never invent new values (chunk-3 instructions).
_ORDER_TYPE_BY_MODE = {"delivery": "domicilio", "pickup": "recoger"}
_DELIVERY_CHECKOUT_CHANNEL = "web_chat"  # same channel diner.py uses for the dine-in web flow


class OrderModeResolveRequest(BaseModel):
    slug: str = Field(..., min_length=1, max_length=100)
    mode: str = Field(..., max_length=10)
    # Coordinates 0,0 ARE valid (Gulf of Guinea) — every check below uses
    # `is None`, never truthiness (bot-rules.md #11).
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    device_token: str | None = Field(default=None, max_length=200)

    @field_validator("mode")
    @classmethod
    def _valid_mode(cls, v: str) -> str:
        if v not in _VALID_MODES:
            raise ValueError(f"mode must be one of {_VALID_MODES}")
        return v


async def _resolve_org_or_404(slug: str) -> dict:
    org = await delivery_repo.db_get_org_by_slug(slug.strip())
    if not org:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")
    return org


@router.get("/org/{slug}")
async def diner_org_info(
    slug: str,
    request: Request,
    device_token: str | None = Query(default=None, max_length=200),
):
    """Public org info for the /pedir/{slug} ordering page."""
    ip = _client_ip(request)
    if not await state_store.rate_limit_check(f"diner_org_info_ip:{ip}", max_requests=30, window_seconds=60):
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta más tarde.")
    if device_token and not await state_store.rate_limit_check(
        f"diner_org_info:{device_token}", max_requests=30, window_seconds=60,
    ):
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta más tarde.")

    org = await _resolve_org_or_404(slug)
    org_id = int(org["id"])

    with tenant_scope(org_id):
        locations = await delivery_repo.db_get_org_locations_for_entry(org_id)

    sedes = []
    delivery_enabled_any = False
    pickup_enabled_any = False
    for location in locations:
        cfg = delivery.get_delivery_config(org, location)
        delivery_enabled_any = delivery_enabled_any or cfg["delivery_enabled"]
        pickup_enabled_any = pickup_enabled_any or cfg["pickup_enabled"]
        if not (cfg["delivery_enabled"] or cfg["pickup_enabled"]):
            # A sede with neither on isn't part of the delivery/pickup entry
            # flow at all (it may only ever do dine-in) — leave it out of
            # the list this endpoint hands to the ordering page.
            continue
        sedes.append({
            "location_id": location.get("id"),
            "name": location.get("name"),
            "address": location.get("address"),
            "phone": location.get("phone"),
            "open_now": delivery.is_location_open(location),
        })

    currency = _features_dict(org.get("features")).get("currency", "COP")

    return {
        "name": org.get("name") or "",
        "currency": currency,
        "delivery_enabled": delivery_enabled_any,
        "pickup_enabled": pickup_enabled_any,
        "locations": sedes,
        # Site key only — never the secret (turnstile.py never exposes it).
        # None (not "") when unset so the page's own truthiness check is a
        # single `if (data.turnstile_site_key)` with no extra empty-string case.
        "turnstile_site_key": turnstile.get_site_key() or None,
    }


@router.post("/order-mode/resolve")
async def diner_order_mode_resolve(request: Request, body: OrderModeResolveRequest):
    """Runs the sede-assignment ladder (docs/claude/delivery-web.md) and
    returns either an assigned delivery sede or a pickup fallback with an
    ordered candidate list and a machine-readable reason."""
    ip = _client_ip(request)
    if not await state_store.rate_limit_check(f"diner_order_mode_ip:{ip}", max_requests=20, window_seconds=60):
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta más tarde.")
    if body.device_token and not await state_store.rate_limit_check(
        f"diner_order_mode:{body.device_token}", max_requests=20, window_seconds=60,
    ):
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta más tarde.")

    org = await _resolve_org_or_404(body.slug)
    org_id = int(org["id"])

    with tenant_scope(org_id):
        locations = await delivery_repo.db_get_org_locations_for_entry(org_id)

    result = delivery.resolve_order_mode(
        org=org, locations=locations, lat=body.lat, lon=body.lon, requested_mode=body.mode,
    )

    log.info(
        "diner.order_mode_resolved",
        org_id=org_id,
        requested_mode=body.mode,
        resolved_mode=result["mode"],
        reason=result.get("reason"),
    )
    return result


# ── Chunk 3: payment-proof upload + checkout (docs/claude/delivery-web.md) ──
#
# BACKEND ONLY — no HTML/JS here. Both endpoints are PUBLIC (no auth), same
# posture as the rest of this module and app/routes/diner.py.


def _refusal(reason: str, message: str) -> HTTPException:
    """Every checkout refusal is a real 422 with BOTH a Spanish message (for
    the chat bubble) and a machine-readable reason (for the page's own
    branching — e.g. "call the restaurant" on a scheduling refusal). Never a
    silent pass, never just a bare status code (chunk-3 instructions)."""
    return HTTPException(status_code=422, detail={"reason": reason, "message": message})


def _parse_scheduled_for(raw: str | None) -> datetime | None:
    """Parses an ISO-8601 datetime. Naive values are treated as UTC (never
    the server's local time), matching is_location_open()'s own convention.
    Raises _refusal("schedule_invalid", ...) on a malformed value."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise _refusal("schedule_invalid", "La fecha/hora programada no es válida.") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_timezone.utc)
    return dt


class DinerDeliveryCheckoutRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=200)
    idempotency_key: str = Field(..., min_length=1, max_length=100)
    customer_name: str = Field(..., min_length=1, max_length=100)
    customer_phone: str = Field(..., min_length=3, max_length=30)
    customer_email: str | None = Field(default=None, max_length=254)
    address: str = Field(..., min_length=1, max_length=300)
    # Coordinates 0,0 ARE valid (Gulf of Guinea) — every check below uses
    # `is None`, never truthiness (bot-rules.md #11).
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    payment_method: str = Field(..., min_length=1, max_length=30)
    cash_change_for: float | None = Field(default=None, ge=0)
    tip_amount: float = Field(default=0.0, ge=0)
    scheduled_for: str | None = Field(default=None, max_length=40)
    turnstile_token: str | None = Field(default=None, max_length=2000)
    device_token: str | None = Field(default=None, max_length=200)

    @field_validator("customer_email")
    @classmethod
    def _valid_email(cls, v: str | None) -> str | None:
        if not v or not v.strip():
            return None
        v = v.strip()
        if "@" not in v or " " in v:
            raise ValueError("email inválido")
        return v

    @field_validator("payment_method")
    @classmethod
    def _normalize_method(cls, v: str) -> str:
        v = v.strip().lower()
        if not v:
            raise ValueError("payment_method is required")
        return v


@router.post("/delivery/payment-proof")
async def diner_delivery_payment_proof(
    request: Request,
    token: str = Form(..., min_length=1, max_length=200),
    file: UploadFile = File(...),
):
    """The customer uploads a Nequi/Bancolombia transfer screenshot BEFORE
    checkout. Returns the hosted URL — the checkout call does NOT accept a
    proof_url from the client at all; it looks this up server-side by token
    (see state_store.delivery_proof_get), which is what makes "the URL can
    only ever attach to THIS session's own order" true by construction
    rather than by trusting client input.
    """
    ip = _client_ip(request)
    if not await state_store.rate_limit_check(f"delivery_proof_ip:{ip}", max_requests=15, window_seconds=60):
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta más tarde.")

    token = token.strip()
    if not await state_store.rate_limit_check(f"delivery_proof_token:{token}", max_requests=6, window_seconds=60):
        raise HTTPException(status_code=429, detail="Estás subiendo comprobantes muy rápido. Espera un momento.")

    session = await diner_sessions_repo.get_by_token(token)
    if session is None:
        raise HTTPException(status_code=404, detail="Sesión no encontrada o expirada")
    if session.get("order_mode") not in _VALID_MODES:
        raise HTTPException(status_code=422, detail="Esta sesión no admite comprobante de pago")
    org_id = int(session["org_id"])

    # Bounded read — never buffer more than the cap in memory regardless of
    # what Content-Length claims.
    data = await file.read(_MAX_PROOF_UPLOAD_BYTES + 1)
    if len(data) > _MAX_PROOF_UPLOAD_BYTES:
        raise HTTPException(status_code=422, detail="La imagen es demasiado grande (máx. 8 MB).")

    from app.services import image_host  # noqa: PLC0415 — mirrors settings_routes.py's own lazy import

    result = image_host.upload_delivery_proof(org_id, data, file.content_type or "")
    if "error" in result:
        log.warning("diner.delivery_proof_upload_failed", org_id=org_id, error=result["error"])
        raise HTTPException(
            status_code=422,
            detail="No pudimos procesar la imagen. Sube una captura clara del comprobante de pago.",
        )

    await state_store.delivery_proof_set(token, result["secure_url"])
    log.info("diner.delivery_proof_uploaded", org_id=org_id)
    return {"proof_url": result["secure_url"]}


@router.post("/delivery/checkout")
async def diner_delivery_checkout(request: Request, body: DinerDeliveryCheckoutRequest):
    """Deterministic checkout — NEVER LLM-parsed (docs/claude/delivery-web.md).
    Creates the order with status=pendiente_aceptacion. Loyalty accrual and
    the customer-facing confirmation EMAIL are explicitly OUT OF SCOPE for
    this chunk (later waves)."""
    ip = _client_ip(request)
    if not await state_store.rate_limit_check(f"delivery_checkout_ip:{ip}", max_requests=10, window_seconds=60):
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta más tarde.")

    token = body.token.strip()
    if not await state_store.rate_limit_check(f"delivery_checkout_token:{token}", max_requests=6, window_seconds=60):
        raise HTTPException(status_code=429, detail="Estás enviando pedidos muy rápido. Espera un momento.")
    if body.device_token and not await state_store.rate_limit_check(
        f"delivery_checkout_device:{body.device_token}", max_requests=6, window_seconds=60,
    ):
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta más tarde.")

    idem_key = body.idempotency_key.strip()
    cache_key = f"{token}:{idem_key}"

    # Fast path — a retry/double-tap with the SAME idempotency_key returns
    # the exact original result, never a second order (mirrors diner.py's
    # /order/send).
    cached = await state_store.delivery_checkout_result_get(cache_key)
    if cached:
        return cached

    session = await diner_sessions_repo.get_by_token(token)
    if session is None:
        raise HTTPException(status_code=404, detail="Sesión no encontrada o expirada")
    org_id = int(session["org_id"])
    bot_number = session["bot_number"]
    order_mode = session.get("order_mode")
    location_id = session.get("location_id")
    if order_mode not in _VALID_MODES or not location_id:
        raise _refusal(
            "invalid_session_mode",
            "Esta sesión no está asociada a un pedido de domicilio o recogida.",
        )

    # scheduled_for is parsed OUTSIDE tenant_scope — it's pure input parsing,
    # no DB access, and a malformed value should fail fast either way.
    scheduled_dt = _parse_scheduled_for(body.scheduled_for)

    # Turnstile — same posture as session creation: no-op success when
    # TURNSTILE_SECRET is unset (local/test); the rate limits above still apply.
    if turnstile.is_configured():
        if not body.turnstile_token or not await turnstile.verify(body.turnstile_token, remote_ip=ip):
            raise _refusal("turnstile_failed", "Verificación de seguridad fallida. Intenta de nuevo.")

    with tenant_scope(org_id):
        # Re-check the cache inside the scope too — a concurrent duplicate
        # request may have just finished and populated it.
        cached = await state_store.delivery_checkout_result_get(cache_key)
        if cached:
            return cached

        org = await db.db_get_org_by_id(org_id)
        if not org:
            raise HTTPException(status_code=404, detail="Restaurante no encontrado")

        location = await db.db_get_location_by_id(location_id)
        if not location or int(location.get("org_id") or -1) != org_id:
            # Same P0 shape as the deleted db_get_restaurant_by_id — org_id
            # and location_id are distinct integers, never trusted paired
            # without checking (bot-rules.md / rls-multitenant.md).
            raise HTTPException(status_code=404, detail="Sede no encontrada")

        raw_cfg = await delivery_repo.db_get_location_delivery_config(org_id, location_id)
        cfg = delivery.get_delivery_config(org, {**location, "delivery_config": raw_cfg})

        # ── Re-resolve coverage + opening hours on the SERVER — never trust
        #    the mode/sede the client resolved earlier at entry time.
        coverage_reason = delivery.validate_coverage_and_hours(
            location=location, config=cfg, order_mode=order_mode, lat=body.lat, lon=body.lon,
        )
        if coverage_reason == delivery.REASON_ALL_CLOSED:
            raise _refusal(coverage_reason, "Esta sede está cerrada en este momento.")
        if coverage_reason == delivery.REASON_NO_GPS:
            raise _refusal(coverage_reason, "Necesitamos tu ubicación para confirmar la cobertura de domicilios.")
        if coverage_reason == delivery.REASON_OUT_OF_COVERAGE:
            raise _refusal(coverage_reason, "Tu dirección quedó fuera de la zona de cobertura de esta sede.")
        if coverage_reason in (delivery.REASON_DELIVERY_DISABLED, delivery.REASON_PICKUP_DISABLED):
            raise _refusal(coverage_reason, "Esta sede ya no ofrece esta modalidad de pedido.")

        currency = _features_dict(org.get("features")).get("currency", "COP")

        cart = await db.db_get_cart(token, bot_number)
        cart_items = cart.get("items") or []
        if not cart_items:
            raise _refusal("empty_cart", "Tu carrito está vacío.")

        # ── Sold-out re-check (chunk 4, docs/claude/delivery-web.md) ────────
        # SAME source of truth the dine-in dish cards use to render "agotado"
        # (app.services.orders.resolve_dish_for_cart / app.routes.diner's
        # /menu and "cat:" chat shortcut both read db_get_menu_availability)
        # — never a second notion of "agotado" invented here.
        # resolve_dish_for_cart already refuses to ADD a sold-out dish to the
        # cart, but a dish can be marked sold out AFTER it was added and
        # BEFORE the customer checks out; chunk 3 landed this checkout
        # endpoint without re-checking at all, so a stale cart line could
        # still go through. This closes that hole with a real server refusal
        # naming the dish, not just a page that greys it out.
        availability = await db.db_get_menu_availability(org_id)
        for item in cart_items:
            dish_name = (item.get("name") or "").strip()
            if dish_name and availability.get(dish_name, True) is False:
                raise _refusal(
                    "dish_sold_out",
                    f"'{dish_name}' está agotado en este momento. Quítalo de tu carrito para continuar.",
                )

        subtotal = quantize_money(money_sum(to_decimal(i.get("subtotal")) for i in cart_items), currency)

        if subtotal < cfg["min_order"]:
            raise _refusal(
                "below_minimum",
                f"El pedido mínimo para esta sede es {_checkout_amount_label(cfg['min_order'], currency)}.",
            )

        allowed_methods = {str(m).strip().lower() for m in cfg["payment_methods"]}
        if body.payment_method not in allowed_methods:
            raise _refusal("payment_method_not_allowed", "Ese método de pago no está disponible en esta sede.")

        delivery_fee = cfg["delivery_fee"] if order_mode == "delivery" else Decimal("0")
        delivery_fee = quantize_money(delivery_fee, currency)
        tip_amount = quantize_money(to_decimal(body.tip_amount), currency)
        total = quantize_money(subtotal + delivery_fee + tip_amount, currency)

        cash_change_for = None
        if body.payment_method in _CASH_METHOD_KEYS:
            if body.cash_change_for is None:
                raise _refusal("cash_change_for_required", "Indica con cuánto vas a pagar en efectivo.")
            cash_change_for = quantize_money(to_decimal(body.cash_change_for), currency)
            if cash_change_for < total:
                raise _refusal(
                    "cash_change_insufficient",
                    f"El monto indicado no cubre el total del pedido ({_checkout_amount_label(total, currency)}).",
                )

        if scheduled_dt is not None:
            schedule_reason = delivery.validate_schedule(location, scheduled_dt)
            if schedule_reason == delivery.REASON_SCHEDULE_NOT_TODAY:
                phone_hint = location.get("phone") or ""
                raise _refusal(
                    schedule_reason,
                    "Solo podemos programar pedidos para hoy. Para otro día, llama al restaurante"
                    + (f" ({phone_hint})." if phone_hint else "."),
                )
            if schedule_reason == delivery.REASON_SCHEDULE_IN_PAST:
                raise _refusal(schedule_reason, "La hora programada ya pasó.")
            if schedule_reason == delivery.REASON_SCHEDULE_OUTSIDE_HOURS:
                raise _refusal(schedule_reason, "Esa hora está fuera del horario de atención de la sede.")

        open_orders = await delivery_repo.db_count_open_orders_for_phone(org_id, body.customer_phone.strip())
        if open_orders >= _MAX_OPEN_ORDERS_PER_PHONE:
            raise _refusal(
                "too_many_open_orders",
                "Ya tienes pedidos en curso. Espera a que se completen antes de hacer uno nuevo.",
            )

        # Only a URL THIS token itself uploaded moments earlier (via
        # POST /delivery/payment-proof) can ever land on the order — the
        # request body above has no proof_url field to smuggle one through.
        proof_url = await state_store.delivery_proof_get(token)

        order_type = _ORDER_TYPE_BY_MODE[order_mode]

        try:
            async with orders._cart_lock(token, bot_number):
                # Re-check the cache inside the lock too.
                cached = await state_store.delivery_checkout_result_get(cache_key)
                if cached:
                    return cached

                created = await delivery_repo.db_create_delivery_order(
                    org_id=org_id,
                    location_id=location_id,
                    phone=token,
                    bot_number=bot_number,
                    order_type=order_type,
                    items=cart_items,
                    address=body.address.strip(),
                    subtotal=subtotal,
                    delivery_fee=delivery_fee,
                    tip_amount=tip_amount,
                    total=total,
                    payment_method=body.payment_method,
                    cash_change_for=cash_change_for,
                    customer_name=body.customer_name.strip(),
                    customer_phone=body.customer_phone.strip(),
                    customer_email=body.customer_email,
                    delivery_lat=body.lat,
                    delivery_lon=body.lon,
                    proof_url=proof_url,
                    scheduled_pickup_at=scheduled_dt,
                    channel=_DELIVERY_CHECKOUT_CHANNEL,
                )
                public_code = await delivery_repo.db_claim_public_code(created["id"], org_id)
                await db.db_clear_cart(token, bot_number)
        except InsufficientStockError as exc:
            # db_create_delivery_order deducts inventory atomically with the
            # INSERT (docs/claude/delivery-web.md, chunk 4) — a shortage
            # rolls back the whole transaction, so no order row was created
            # and the cart is deliberately left intact (same convention as
            # the dine-in path's insufficient-stock refusal: the customer can
            # remove just the sold-out line and resend the rest).
            log.warning(
                "diner.delivery_checkout_insufficient_stock",
                org_id=org_id, location_id=location_id, sku=exc.sku,
            )
            raise _refusal(
                "insufficient_stock",
                f"Lo siento, '{exc.sku}' no está disponible en este momento. "
                "Ajusta tu pedido y vuelve a intentar.",
            ) from exc
        except RuntimeError as exc:
            if "cart_lock_contention" in str(exc):
                raise HTTPException(
                    status_code=409,
                    detail="Tu pedido está siendo procesado, por favor espera un momento.",
                ) from exc
            raise

    # Publish OUTSIDE tenant_scope/the lock — every write above has already
    # committed by here (same convention as orders_repo.commit_order_transaction).
    await realtime.publish(org_id, "order.created", location_id=location_id, entity_id=created["id"])

    response = {
        "order_id": created["id"],
        "public_code": public_code,
        "status": created["status"],
        "order_type": order_type,
        "subtotal": float(subtotal),      # JSON boundary
        "delivery_fee": float(delivery_fee),  # JSON boundary
        "tip_amount": float(tip_amount),  # JSON boundary
        "total": float(total),            # JSON boundary
        "currency": currency,
        "payment_method": body.payment_method,
        "proof_url": proof_url,
        "scheduled_pickup_at": scheduled_dt.isoformat() if scheduled_dt else None,
        "message": "Listo, tu pedido fue enviado al restaurante.",
    }
    await state_store.delivery_checkout_result_set(cache_key, response, ttl_seconds=300)

    log.info(
        "diner.delivery_checkout_success",
        org_id=org_id, location_id=location_id, order_id=created["id"], order_type=order_type,
    )
    return response
