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

import asyncio
import html as _html_lib
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field, field_validator, model_validator

from app.repositories import delivery_repo, diner_sessions_repo
from app.repositories.orders_repo import InsufficientStockError
from app.routes.diner import _checkout_amount_label, _client_ip, _features_dict
from app.services import database as db
from app.services import delivery
from app.services import orders
from app.services import realtime
from app.services import state_store
from app.services import turnstile
from app.services.logging import get_logger, mask_email
from app.services.money import money_sum, quantize_money, to_decimal
from app.services.tenant_context import tenant_scope

log = get_logger(__name__)

router = APIRouter(prefix="/api/diner", tags=["diner-delivery"])

_VALID_MODES = ("delivery", "pickup")

# Fire-and-forget background tasks (chunk 6's confirmation email) MUST be
# held by a strong reference somewhere, or asyncio is free to garbage-collect
# an in-flight Task before it finishes (a well-known asyncio.create_task
# gotcha) — see https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task.
_background_tasks: set[asyncio.Task] = set()


def _fire_and_forget(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

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


async def _send_order_confirmation_email(to: str, org_name: str, public_code: str) -> None:
    """Best-effort order-confirmation email (docs/claude/delivery-web.md
    chunk 6: "when a checkout succeeds and the customer gave an email, send
    one message with the code and the /pedido/{code} link"). Fired via
    _fire_and_forget AFTER the checkout's own DB transaction has committed —
    never awaited by the checkout response, so a slow or failing provider
    can never make checkout fail or feel slow.

    app.services.email.send_email() itself already never raises; the extra
    try/except here only guards the HTML-building code above it, so a bug
    there becomes a logged warning instead of an "exception was never
    retrieved" asyncio warning with no context.
    """
    try:
        from app.services import email as email_service  # noqa: PLC0415 — lazy, mirrors image_host's own pattern

        link = f"/pedido/{public_code}"
        safe_org = _html_lib.escape(org_name or "Mesio")
        safe_code = _html_lib.escape(public_code)
        html = (
            f"<p>Gracias por tu pedido en {safe_org}.</p>"
            f"<p>Tu código de pedido es <strong>{safe_code}</strong>.</p>"
            f"<p>Puedes ver el estado de tu pedido aquí: <a href=\"{link}\">{link}</a></p>"
        )
        text = f"Gracias por tu pedido en {org_name or 'Mesio'}. Código: {public_code}. Estado: {link}"
        ok = await email_service.send_email(
            to=to, subject=f"Tu pedido en {org_name or 'Mesio'}", html=html, text=text,
        )
        if not ok:
            log.warning("diner.delivery_confirmation_email_failed", to=mask_email(to))
    except Exception:
        log.warning("diner.delivery_confirmation_email_error", to=mask_email(to), exc_info=True)


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
        availability = await db.db_get_menu_availability(org_id, location_id)
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

    if body.customer_email:
        # Fire-and-forget — see _send_order_confirmation_email's docstring.
        # Never awaited: a slow/broken email provider must not delay or
        # fail this response.
        _fire_and_forget(_send_order_confirmation_email(body.customer_email, org.get("name") or "", public_code))

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


# ── Chunk 6: the customer status page /pedido/{public_code} ────────────────
#
# docs/claude/delivery-web.md, "Customer status page". PUBLIC (no auth) —
# the public_code itself is "effectively a bearer secret" (module docstring
# for db_get_order_by_public_code): globally unique, not sequential, minted
# from a 32-symbol alphabet at 6 chars (~1e9 combinations). Every endpoint
# below is rate-limited per IP AND per code through state_store (Redis,
# cross-worker — never a module-level counter) to make guessing impractical
# on top of the keyspace itself.

_STATUS_WINDOW = 60
_STATUS_READ_IP_MAX = 30
_STATUS_READ_CODE_MAX = 40
_STATUS_READ_404_IP_MAX = 8  # a burst of misses from one IP looks like enumeration
_STATUS_CANCEL_IP_MAX = 10
_STATUS_CANCEL_CODE_MAX = 5
_STATUS_NPS_IP_MAX = 10
_STATUS_NPS_CODE_MAX = 5


def _iso_or_none(value) -> str | None:
    return value.isoformat() if value is not None else None


def _money_or_none(value) -> float | None:
    return float(value) if value is not None else None  # JSON boundary


async def _resolve_order_by_code_or_404(public_code: str) -> dict:
    order = await delivery_repo.db_get_order_by_public_code(public_code.strip())
    if not order:
        raise HTTPException(status_code=404, detail="Pedido no encontrado")
    return order


def _public_order_items_view(items: list) -> list[dict]:
    """Only what the status page needs per line: name, quantity, note, its
    own subtotal. Never sku/category/line_id — internal cart bookkeeping the
    customer's own status page has no use for."""
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        out.append({
            "name": item.get("name") or "",
            "quantity": item.get("quantity") if item.get("quantity") is not None else item.get("qty") or 1,
            "note": item.get("note") or "",
            "subtotal": _money_or_none(item.get("subtotal")) or 0.0,
        })
    return out


def _public_order_view(order: dict, org: dict, location: dict, currency: str, nps_already_submitted: bool) -> dict:
    """Everything (and ONLY what) /pedido/{code} needs to render.

    Deliberately EXCLUDES: customer_phone, customer_email (chunk-6
    instructions — the code is a bearer secret, so these would otherwise
    leak to anyone who guesses/finds a link), customer_name (same PII
    surface — the customer already knows their own name, the page doesn't
    need to echo it back), every internal id except the public_code itself
    (no order id, no org_id, no location_id), and any staff/courier data
    (no courier name — Phase A's customer-facing status vocabulary per
    docs/claude/delivery-web.md is limited to the order's own lifecycle
    states, not who is assigned to it).
    """
    order_type = order.get("order_type")
    status = order.get("status")
    return {
        "public_code": order.get("public_code"),
        "org_name": org.get("name") or "",
        "location_name": location.get("name") or "",
        "location_phone": location.get("phone") or None,
        "order_type": order_type,
        "status": status,
        "items": _public_order_items_view(order.get("items")),
        "currency": currency,
        "subtotal": _money_or_none(order.get("subtotal")),
        "delivery_fee": _money_or_none(order.get("delivery_fee")),
        "tip_amount": _money_or_none(order.get("tip_amount")),
        "total": _money_or_none(order.get("total")),
        "payment_method": order.get("payment_method"),
        # A customer who paid by transfer uploaded a receipt and then had no
        # way to know the restaurant accepted it — the page showed only WHICH
        # method was chosen, never whether the money was taken as received.
        # Not PII and not an internal id: it is the state of their own order.
        "paid": bool(order.get("paid")),
        # Address only for delivery orders (chunk-6 instructions) — a pickup
        # order's "address" column is always empty anyway, but this keeps
        # the contract explicit rather than incidental.
        "address": order.get("address") if order_type == "domicilio" else None,
        "created_at": _iso_or_none(order.get("created_at")),
        "accepted_at": _iso_or_none(order.get("accepted_at")),
        "estimated_minutes": order.get("estimated_minutes"),
        "eta": delivery.compute_eta(order.get("accepted_at"), order.get("estimated_minutes"), location),
        "rejected_at": _iso_or_none(order.get("rejected_at")),
        "rejection_reason": order.get("rejection_reason"),
        "cancelled_at": _iso_or_none(order.get("cancelled_at")),
        "delivered_at": _iso_or_none(order.get("delivered_at")),
        "can_cancel": status == delivery_repo.STATUS_PENDING_ACCEPTANCE,
        "nps": {
            "eligible": status == delivery_repo.STATUS_DELIVERED,
            "already_submitted": nps_already_submitted,
        },
    }


@router.get("/order/{public_code}")
async def diner_order_public_status(public_code: str, request: Request):
    """The public read behind /pedido/{public_code}. See _public_order_view's
    docstring for the exact field list and what is deliberately left out."""
    ip = _client_ip(request)
    code = public_code.strip()
    if not await state_store.rate_limit_check(
        f"diner_order_read_ip:{ip}", max_requests=_STATUS_READ_IP_MAX, window_seconds=_STATUS_WINDOW,
    ):
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta más tarde.")
    if not await state_store.rate_limit_check(
        f"diner_order_read_code:{code}", max_requests=_STATUS_READ_CODE_MAX, window_seconds=_STATUS_WINDOW,
    ):
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes para este pedido. Intenta más tarde.")

    order = await delivery_repo.db_get_order_by_public_code(code)
    if not order:
        # A burst of distinct-code misses from the same IP looks like code
        # enumeration — logged WITHOUT the guessed code itself (never log a
        # full public_code — docs/claude/delivery-web.md chunk 6).
        if not await state_store.rate_limit_check(
            f"diner_order_read_404_ip:{ip}", max_requests=_STATUS_READ_404_IP_MAX, window_seconds=_STATUS_WINDOW,
        ):
            log.warning("diner.order_read_enumeration_suspected", ip=ip)
        raise HTTPException(status_code=404, detail="Pedido no encontrado")

    org_id = int(order["org_id"])
    location_id = order.get("location_id")
    with tenant_scope(org_id):
        org = await db.db_get_org_by_id(org_id)
        location = await db.db_get_location_by_id(location_id) if location_id else None
        already_submitted = order.get("nps_answered_at") is not None

    if not org or not location or int(location.get("org_id") or -1) != org_id:
        # Same defensive shape as diner_delivery_checkout above — org_id and
        # location_id are distinct integers, never trusted paired together
        # without checking (bot-rules.md / rls-multitenant.md).
        raise HTTPException(status_code=404, detail="Pedido no encontrado")

    currency = _features_dict(org.get("features")).get("currency", "COP")
    return _public_order_view(order, org, location, currency, already_submitted)


class DinerOrderCancelRequest(BaseModel):
    # Optional on purpose — a device holding only the link (no session
    # token) still gets a real 403 with a helpful message instead of a
    # validation error (chunk-6 instructions: "the page tells them to call
    # the restaurant").
    token: str | None = Field(default=None, max_length=200)


@router.post("/order/{public_code}/cancel")
async def diner_order_cancel(public_code: str, body: DinerOrderCancelRequest, request: Request):
    """Only from pendiente_aceptacion, only by the diner_sessions token that
    CREATED the order (orders.phone IS that token — see the module
    docstring's "web:<uuid4>" convention). Anyone else — including another
    device that only has the link — is refused with a message pointing at
    calling the restaurant, never a silent no-op."""
    ip = _client_ip(request)
    code = public_code.strip()
    if not await state_store.rate_limit_check(
        f"diner_order_cancel_ip:{ip}", max_requests=_STATUS_CANCEL_IP_MAX, window_seconds=_STATUS_WINDOW,
    ):
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta más tarde.")
    if not await state_store.rate_limit_check(
        f"diner_order_cancel_code:{code}", max_requests=_STATUS_CANCEL_CODE_MAX, window_seconds=_STATUS_WINDOW,
    ):
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta más tarde.")

    order = await _resolve_order_by_code_or_404(code)
    token = (body.token or "").strip()
    if not token or order.get("phone") != token:
        log.warning("diner.order_cancel_wrong_token", order_id=order["id"])
        raise HTTPException(
            status_code=403,
            detail="No puedes cancelar este pedido desde este dispositivo. Llama al restaurante para cancelarlo.",
        )

    org_id = int(order["org_id"])
    with tenant_scope(org_id):
        row = await delivery_repo.db_cancel_order(org_id, order["id"])
    if not row:
        raise HTTPException(
            status_code=409,
            detail="Tu pedido ya fue aceptado por el restaurante y no se puede cancelar. Llama al restaurante.",
        )

    # Publish OUTSIDE tenant_scope — the UPDATE already committed (same
    # convention as every other transition in this wave). This is what makes
    # the cashier's Domicilios queue drop the order without polling
    # (docs/claude/delivery-web.md: "A cancel emits the realtime event so
    # the cashier's queue drops it").
    await realtime.publish_delivery_status(org_id, row.get("location_id"), row["id"])
    log.info("diner.order_cancelled_by_customer", org_id=org_id, order_id=row["id"])
    return {"status": row["status"], "cancelled_at": _iso_or_none(row.get("cancelled_at"))}


class DinerOrderNpsRequest(BaseModel):
    token: str | None = Field(default=None, max_length=200)
    score: int | None = Field(default=None, ge=1, le=5)
    comment: str | None = Field(default=None, max_length=500)
    skip: bool = Field(default=False)

    @model_validator(mode="after")
    def _score_required_unless_skipping(self):
        if not self.skip and self.score is None:
            raise ValueError("score is required unless skip=true")
        return self


@router.post("/order/{public_code}/nps")
async def diner_order_nps(public_code: str, body: DinerOrderNpsRequest, request: Request):
    """1-5 survey on the status page, once the order is entregado. Reuses the
    SAME storage path the WhatsApp-era bot flow writes to
    (conversations_repo.db_save_nps_response, re-exported as
    db.db_save_nps_response), attributed to the order's sede. Deduplicated
    PER ORDER by orders.nps_answered_at (migration 0087) — not by the
    per-token state_store flag, because the web session token is reused
    across orders and that flag let a returning customer rate only once.

    Deliberately does NOT go through agent.trigger_nps / tables.py's
    _farewell_and_nps: those also fire send_wa_interactive_nps(phone, ...)
    for ANY phone, which is exactly the trap this chunk was told to avoid —
    calling Meta with an invalid number for a `web:` identity
    (docs/claude/delivery-web.md chunk 6). This handler never imports or
    calls anything WhatsApp-related; it only ever touches the DB write +
    the state_store dedup flag directly. See
    tests/test_delivery_status_page.py::test_nps_submit_never_sends_whatsapp.
    """
    ip = _client_ip(request)
    code = public_code.strip()
    if not await state_store.rate_limit_check(
        f"diner_order_nps_ip:{ip}", max_requests=_STATUS_NPS_IP_MAX, window_seconds=_STATUS_WINDOW,
    ):
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta más tarde.")
    if not await state_store.rate_limit_check(
        f"diner_order_nps_code:{code}", max_requests=_STATUS_NPS_CODE_MAX, window_seconds=_STATUS_WINDOW,
    ):
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta más tarde.")

    order = await _resolve_order_by_code_or_404(code)
    token = (body.token or "").strip()
    if not token or order.get("phone") != token:
        raise HTTPException(status_code=403, detail="No puedes calificar este pedido desde este dispositivo.")
    if order.get("status") != delivery_repo.STATUS_DELIVERED:
        raise HTTPException(status_code=422, detail="Todavía no puedes calificar este pedido.")

    phone, bot_number = order["phone"], order["bot_number"]
    org_id = int(order["org_id"])
    with tenant_scope(org_id):
        # Claim first: the conditional UPDATE is the once-per-order guard,
        # race-safe and durable (not a per-token TTL flag in Redis).
        if not await delivery_repo.db_claim_order_nps(org_id, order["id"]):
            raise HTTPException(status_code=409, detail="Ya calificaste este pedido.")
        if not body.skip:
            await db.db_save_nps_response(
                phone, bot_number, body.score, (body.comment or "").strip(),
                location_id=order.get("location_id"),
            )

    log.info("diner.order_nps_submitted", org_id=org_id, order_id=order["id"], skipped=body.skip)
    return {"ok": True, "skipped": body.skip}
