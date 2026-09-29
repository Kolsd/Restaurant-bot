import os
import json
from fastapi import APIRouter, Request, HTTPException, Query, Depends
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
from app.services import database as db
from app.routes.deps import (
    require_auth,
    get_current_restaurant,
    get_current_user,
    get_current_restaurant_scoped,
    may_span_locations,
    resolve_sede_filter,
)
from app.repositories import conversations_repo
from app.repositories import stats_repo
from app.services.money import quantize_money
from app.services.sede_menu import parse_price
from app.services.tenant_context import tenant_scope
from app.services.logging import get_logger

_log = get_logger(__name__)


router = APIRouter()

# strftime("%a") follows the server locale (English on Railway); the dashboard is Spanish.
_ES_WEEKDAYS = ("Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom")


def _chart_label(d: datetime, period: str) -> str:
    if period in ("today", "week"):
        return f"{_ES_WEEKDAYS[d.weekday()]} {d.day:02d}"
    return d.strftime("%d/%m")


def get_tz(restaurant: dict) -> str:
    feats = restaurant.get("features", {})
    if isinstance(feats, str):
        try: feats = json.loads(feats)
        except: feats = {}
    return feats.get("timezone", "UTC")

def get_date_range(period: str, tz_str: str):
    tz = ZoneInfo(tz_str)
    today = datetime.now(tz).date()
    if period == "today": return str(today), str(today)
    elif period == "week": return str(today - timedelta(days=6)), str(today)
    elif period == "month": return str(today.replace(day=1)), str(today)
    elif period == "semester": return str(today.replace(month=1 if today.month <= 6 else 7, day=1)), str(today)
    elif period == "year": return str(today.replace(month=1, day=1)), str(today)
    return str(today), str(today)

async def filter_conversations_for_branch(conversations: list, branch_id: int | str, bot_number: str) -> list:
    """If the user belongs to a branch, only shows chats from their tables."""
    if not branch_id or branch_id == "all" or not conversations:
        return conversations
    from app.repositories import tables_repo
    allowed_phones = await tables_repo.db_get_session_phones_by_branch(branch_id, bot_number)
    return [c for c in conversations if c.get("phone") in allowed_phones]
        
async def _get_effective_bot_number(restaurant: dict) -> str:
    """Wave-2: returns the restaurant's own WhatsApp number.

    Pre-Wave-2 this resolved to the parent's number for branch restaurants
    (parent_restaurant_id was used). That column was dropped in 0038; in the
    Wave-2 model every location has its own whatsapp_number on the locations
    row, so we always return the restaurant's own number.
    """
    return restaurant.get("whatsapp_number", "")

def _resolve_branch_id(request: Request, user: dict, restaurant: dict) -> int | str | None:
    """Resolve the effective sede for stats filtering.

    "all" and "matriz" are admin-only sentinels the sidebar sends; everything
    else is a location_id, or None for "no sede filter".

    Fixed 2026-09-20 (PM: an employee of one sede must not see another): the
    non-admin branch returned `user["branch_id"]`, which for a staff row is
    the ORG id, not a sede — the exact org/location confusion
    rls-multitenant.md forbids. Depending on the query it matched nothing or
    matched the whole org. Non-admins now get their real `location_id` from
    resolve_sede_filter, and are refused when they have none.
    """
    return resolve_sede_filter(request, user, allow_all_sentinel=True)

@router.get("/api/dashboard/sync")
async def dashboard_sync(request: Request, period: str = Query("today")):
    user = await get_current_user(request)
    restaurant = await get_current_restaurant(request)
    bot_number = restaurant["whatsapp_number"]
    date_from, date_to = get_date_range(period, get_tz(restaurant))
    branch_id = _resolve_branch_id(request, user, restaurant)
    effective_bot = await _get_effective_bot_number(restaurant)
    sede_id = resolve_sede_filter(request, user)

    with tenant_scope(restaurant["id"]):
        # Revenue/orders/chart: table rounds + paid delivery, same rules as the
        # channel card. `orders` alone (below) never saw a single table sale.
        sales_by_day  = await stats_repo.db_sales_daily(date_from, date_to, location_id=sede_id)
        orders        = await db.db_get_orders_range(date_from, date_to, bot_number=bot_number)
        reservations  = await db.db_get_reservations_range(date_from, date_to, bot_number=bot_number)
        all_convs     = await db.db_get_all_conversations(
            bot_number=effective_bot,
            date_from=date_from,
            date_to=date_to
        )
    conversations = await filter_conversations_for_branch(all_convs, branch_id, effective_bot)

    paid    = [o for o in orders if o["paid"]]
    pending = [o for o in orders if not o["paid"]]

    formatted_orders = []
    for o in orders:
        try:
            items = o.get("items", [])
            if isinstance(items, str):
                items = json.loads(items)
            items_summary = ", ".join(f"{i.get('quantity',1)}x {i.get('name','')}" for i in items) if isinstance(items, list) else str(items)
        except: 
            items_summary = str(o.get("items", ""))
            
        created = datetime.fromisoformat(o["created_at"])
        formatted_orders.append({
            "id": o["id"], "items": items_summary or "-", "type": o["order_type"], 
            "paid": o["paid"], "total": o["total"], "address": o.get("address", ""), 
            "status": o["status"], "phone": o.get("phone", ""),
            "time": created.strftime("%d/%m %H:%M") if period != "today" else created.strftime("%H:%M")
        })

    labels, revenue_data, orders_data = [], [], []
    current = datetime.strptime(date_from, "%Y-%m-%d")
    end     = datetime.strptime(date_to, "%Y-%m-%d")
    while current <= end:
        day = sales_by_day.get(current.date().isoformat(), {})
        labels.append(_chart_label(current, period))
        revenue_data.append(float(day.get("total", 0)))  # JSON boundary
        orders_data.append(day.get("count", 0))
        current += timedelta(days=1)

    return {
        "stats": {
            "orders": {
                "total": sum(orders_data),
                "revenue": float(quantize_money(sum(
                    (d["total"] for d in sales_by_day.values()), Decimal("0")))),  # JSON boundary
                # Delivery only: a table round is settled on its check, not here.
                "paid": len(paid), "pending": len(pending),
                "pending_revenue": sum(o["total"] for o in pending)
            },
            "reservations": {
                "total": len(reservations),
                "guests": sum(r.get("guests", 0) for r in reservations)
            },
            "conversations": {"active": len(conversations)}
        },
        "chart": {"labels": labels, "revenue": revenue_data, "orders": orders_data},
        "orders": formatted_orders,
        "reservations": reservations,
        "conversations": conversations
    }

@router.get("/api/dashboard/conversations")
async def get_conversations(request: Request):
    await require_auth(request)
    user = await get_current_user(request)
    restaurant = await get_current_restaurant(request)

    bot_number = restaurant.get("whatsapp_number", "")

    # X-Branch-ID semantics for /dashboard/conversations:
    # - digit value (= location_id from sidebar dropdown) → filter to that sede
    # - 'matriz', 'all', missing → no sede filter; RLS (org_isolation on
    #   conversations) returns every conversation of the current org
    # Pre-2026-04-29 the default fallback was restaurant["id"] (= org_id post-
    # Wave-2 normalization in db_get_restaurant_by_id), but conversations
    # carry branch_id == location_id post-migration 0057 — filtering
    # branch_id = org_id matched zero rows. Same bug family as floor_plan.
    # Non-admins used to fall through to branch_id = None here, i.e. every
    # conversation of the org regardless of which sede they work at.
    branch_id = resolve_sede_filter(request, user)

    with tenant_scope(restaurant["id"]):
        conversations = await db.db_get_all_conversations(bot_number=bot_number, branch_id=branch_id)
    return {"conversations": conversations}

@router.get("/api/menu/availability")
async def get_menu_availability(request: Request):
    await require_auth(request)
    user = await get_current_user(request)
    restaurant = await get_current_restaurant(request)

    # Sold out is PER SEDE (migration 0091). Each sede is its own restaurant:
    # running out of salmon at Sede Norte says nothing about Sede Centro.
    # Until 0091 the key was (org_id, dish_name) and this endpoint returned —
    # and wrote — one state for the whole business.
    org_id = restaurant["id"]
    sede = resolve_sede_filter(request, user, admin_without_header="own")
    if not isinstance(sede, int):
        raise HTTPException(
            status_code=400,
            detail="Elegí una sede para ver qué platos están agotados",
        )

    with tenant_scope(org_id):
        availability = await db.db_get_menu_availability(org_id, sede)
    return {"availability": availability, "location_id": sede}

@router.post("/api/menu/availability")
async def set_dish_availability(request: Request):
    await require_auth(request)
    body = await request.json()
    if not body.get("dish_name"): raise HTTPException(status_code=400, detail="dish_name requerido")

    user = await get_current_user(request)
    restaurant = await get_current_restaurant(request)

    # Same rule as GET: the sede whose menu is being changed. An owner
    # looking at every sede at once has to pick one first — "agotado" has
    # to mean somewhere in particular.
    org_id = restaurant["id"]
    sede = resolve_sede_filter(request, user, admin_without_header="own")
    if not isinstance(sede, int):
        raise HTTPException(
            status_code=400,
            detail="Elegí la sede en la que se agotó este plato",
        )

    with tenant_scope(org_id):
        await db.db_set_dish_availability(
            restaurant_id=org_id,
            dish_name=body["dish_name"],
            available=body.get("available", True),
            location_id=sede,
        )
    return {
        "success": True,
        "dish_name": body["dish_name"],
        "available": body.get("available", True),
        "location_id": sede,
    }

@router.post("/api/menu/sync-branches")
async def sync_menu_to_branches(request: Request):
    """
    Endpoint exclusive to Casa Matriz (HQ). Propagates the menu to all branches.
    """
    await require_auth(request)
    user = await get_current_user(request)
    restaurant = await get_current_restaurant(request)

    # Security checks
    if "owner" not in user.get("role", ""):
        raise HTTPException(status_code=403, detail="Solo el dueño puede sincronizar el menú.")

    # Wave-2: parent_restaurant_id no longer exists. The X-Branch-ID guard below
    # is sufficient — syncing from a specific branch view is still blocked.
    branch_header = request.headers.get("X-Branch-ID")
    if branch_header and branch_header != "matriz" and branch_header != "all":
        raise HTTPException(status_code=400, detail="Debes estar en la vista de la Casa Matriz para sincronizar.")

    with tenant_scope(restaurant["id"]):
        branches_updated = await db.db_sync_menu_to_branches(restaurant["id"])
    return {"success": True, "branches_updated": branches_updated}

@router.put("/api/menu/update")
async def update_menu_structure(request: Request):
    """
    Endpoint exclusive to Casa Matriz (HQ). Updates the menu JSON structure.
    """
    await require_auth(request)
    user = await get_current_user(request)
    restaurant = await get_current_restaurant(request)
    
    # owner == admin (PM 2026-09-20). A gerente changes their own sede's
    # carta through /api/menu/sede/*, never the base every sede inherits.
    if not may_span_locations(user):
        raise HTTPException(status_code=403, detail="Solo el dueño o un admin pueden editar la carta general.")

    # Wave-2: parent_restaurant_id no longer exists. X-Branch-ID guard is sufficient.
    branch_header = request.headers.get("X-Branch-ID")
    if branch_header and branch_header != "matriz" and branch_header != "all":
        raise HTTPException(status_code=400, detail="Debes estar en la vista de la Casa Matriz para editar el menú.")

    body = await request.json()
    new_menu = body.get("menu")
    if not isinstance(new_menu, dict):
        raise HTTPException(status_code=400, detail="Formato de menú inválido.")

    # 🛡️ Strict backend validation: ensure prices are numeric
    for cat, items in new_menu.items():
        if not isinstance(items, list):
            raise HTTPException(status_code=400, detail=f"La categoría '{cat}' no tiene una lista de platos.")
        for item in items:
            if not isinstance(item, dict):
                raise HTTPException(status_code=400, detail=f"Hay un plato inválido en '{cat}'.")
            try:
                item["price"] = parse_price(item.get("price", 0))
            except ValueError:
                raise HTTPException(status_code=400, detail=f"El precio del plato '{item.get('name')}' debe ser un número (sin signos $ ni letras).")

    with tenant_scope(restaurant["id"]):
        success = await db.db_update_menu(restaurant["id"], new_menu)
    if not success:
        raise HTTPException(status_code=500, detail="No se pudo actualizar el menú en la base de datos.")
    return {"success": True}

@router.delete("/api/conversations/cleanup")
async def cleanup_conversations(request: Request):
    restaurant = await get_current_restaurant(request)
    with tenant_scope(restaurant["id"]):
        result = await db.db_cleanup_old_conversations(days=7, bot_number=restaurant["whatsapp_number"])
    return {"success": True, "result": str(result)}

@router.get("/api/conversations/{phone}")
async def get_conversation(phone: str, request: Request):
    restaurant = await get_current_restaurant(request)
    with tenant_scope(restaurant["id"]):
        details = await db.db_get_conversation_details(phone, restaurant["whatsapp_number"])
    return {"phone": phone, "history": details.get("history", []), "bot_paused": details.get("bot_paused", False)}

# ── DASHBOARD ANALYTICS — TIER 2 ─────────────────────────────────────────────


@router.get("/api/stats/by-channel")
async def get_sales_by_channel(
    request: Request,
    period_start: str | None = Query(None),
    period_end:   str | None = Query(None),
    branch_id:    str | None = Query(None),
    compare:      bool       = Query(False),
):
    """Sales breakdown by channel (WhatsApp Bot, POS, QR, Delivery, etc.).

    Aggregates both `orders` (delivery/pickup) and `table_orders` (salon).
    Period defaults to the last 7 days if not supplied.

    If compare=true, also returns `previous` period data and `deltas` dict.
    """
    restaurant = await get_current_restaurant(request)
    ps, pe = stats_repo._default_period(period_start, period_end)
    bid = int(branch_id) if branch_id and branch_id.isdigit() else None
    org_id = restaurant["id"]
    loc_id = bid if bid is not None else (
        restaurant.get("location_id") if branch_id and branch_id not in ("all", "matriz") else None
    )

    with tenant_scope(org_id):
        current = await stats_repo.db_sales_by_channel(
            org_id=org_id,
            period_start=ps,
            period_end=pe,
            location_id=loc_id if branch_id else None,
        )
        if not compare:
            return current

        prev_ps, prev_pe = stats_repo._prev_period(ps, pe)
        previous = await stats_repo.db_sales_by_channel(
            org_id=org_id,
            period_start=prev_ps,
            period_end=prev_pe,
            location_id=loc_id if branch_id else None,
        )

    # compute deltas
    curr_total = current.get("total", 0)
    prev_total = previous.get("total", 0)
    curr_count = current.get("total_count", 0)
    prev_count = previous.get("total_count", 0)

    def _pct_delta(curr, prev):
        if not prev:
            return None
        return round((curr - prev) / prev * 100, 1)

    return {
        **current,
        "previous": previous,
        "deltas": {
            "total_pct":       _pct_delta(curr_total, prev_total),
            "total_count_pct": _pct_delta(curr_count, prev_count),
        },
    }


@router.get("/api/stats/top-dishes")
async def get_top_dishes(
    request: Request,
    period_start: str | None = Query(None),
    period_end:   str | None = Query(None),
    branch_id:    str | None = Query(None),
    limit:        int        = Query(10, ge=1, le=50),
    compare:      bool       = Query(False),
):
    """Top N dishes by revenue with food cost and margin % for the period.

    Joins JSONB items arrays from both orders and table_orders, enriches with
    recipe-based food costs and current menu metadata.

    If compare=true, also returns `previous` period data and `deltas` dict.
    """
    restaurant = await get_current_restaurant(request)
    ps, pe = stats_repo._default_period(period_start, period_end)
    bid = int(branch_id) if branch_id and branch_id.isdigit() else None
    org_id = restaurant["id"]

    with tenant_scope(org_id):
        current = await stats_repo.db_top_dishes(
            org_id=org_id,
            period_start=ps,
            period_end=pe,
            limit=limit,
            location_id=bid,
        )
        if not compare:
            return current

        prev_ps, prev_pe = stats_repo._prev_period(ps, pe)
        previous = await stats_repo.db_top_dishes(
            org_id=org_id,
            period_start=prev_ps,
            period_end=prev_pe,
            limit=limit,
            location_id=bid,
        )

    # deltas: compare top-1 revenue if available
    curr_top_rev = current["dishes"][0]["revenue"] if current.get("dishes") else 0
    prev_top_rev = previous["dishes"][0]["revenue"] if previous.get("dishes") else 0

    def _pct_delta(curr, prev):
        if not prev:
            return None
        return round((curr - prev) / prev * 100, 1)

    return {
        **current,
        "previous": previous,
        "deltas": {
            "top_dish_revenue_pct": _pct_delta(curr_top_rev, prev_top_rev),
        },
    }


@router.get("/api/stats/inventory-critical")
async def get_inventory_critical(
    request: Request,
    ok_limit: int = Query(3, ge=0, le=20),
):
    """Low-stock ingredients with the dishes they affect.

    Returns `alerts` (critical + warning items) and `ok` (a small sample of
    healthy items for visual reference in the dashboard card).
    """
    restaurant = await get_current_restaurant(request)
    org_id = restaurant["id"]
    user = await get_current_user(request)
    sede = resolve_sede_filter(request, user)

    with tenant_scope(org_id):
        return await stats_repo.db_inventory_critical(
            org_id=org_id,
            ok_limit=ok_limit,
            location_id=sede if isinstance(sede, int) else None,
        )


@router.get("/api/stats/live-orders")
async def get_live_orders(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
):
    """Unified live feed of active orders: delivery + table, sorted newest-first.

    Intended for dashboard polling (lightweight — does not include full item lists).
    """
    restaurant = await get_current_restaurant(request)
    org_id = restaurant["id"]

    with tenant_scope(org_id):
        return await stats_repo.db_live_orders(
            org_id=org_id,
            limit=limit,
        )


# ── DASHBOARD ANALYTICS — TIER 3 ─────────────────────────────────────────────


@router.get("/api/stats/payment-status")
async def get_payment_status(
    request: Request,
    period_start: str | None = Query(None),
    period_end:   str | None = Query(None),
    branch_id:    str | None = Query(None),
    compare:      bool       = Query(False),
):
    """Payment status donut: paid / pending / disputed / courtesy buckets.

    Aggregates both orders (delivery/pickup) and table_checks (salon).
    Period defaults to last 7 days when not supplied.

    If compare=true, also returns `previous` period data and `deltas` dict.
    """
    restaurant = await get_current_restaurant(request)
    ps, pe = stats_repo._default_period(period_start, period_end)
    bid = int(branch_id) if branch_id and branch_id.isdigit() else None
    org_id = restaurant["id"]
    loc_id = bid if bid is not None else (
        restaurant.get("location_id") if branch_id and branch_id not in ("all", "matriz") else None
    )

    with tenant_scope(org_id):
        current = await stats_repo.db_payment_status(
            org_id=org_id,
            period_start=ps,
            period_end=pe,
            location_id=loc_id if branch_id else None,
        )
        if not compare:
            return current

        prev_ps, prev_pe = stats_repo._prev_period(ps, pe)
        previous = await stats_repo.db_payment_status(
            org_id=org_id,
            period_start=prev_ps,
            period_end=prev_pe,
            location_id=loc_id if branch_id else None,
        )

    curr_total = current.get("total_count", 0)
    prev_total = previous.get("total_count", 0)

    # Compute paid-count delta
    def _bucket_count(data, key):
        for b in data.get("buckets", []):
            if b["key"] == key:
                return b["count"]
        return 0

    curr_paid = _bucket_count(current, "paid")
    prev_paid = _bucket_count(previous, "paid")

    def _pct_delta(curr, prev):
        if not prev:
            return None
        return round((curr - prev) / prev * 100, 1)

    return {
        **current,
        "previous": previous,
        "deltas": {
            "total_count_pct": _pct_delta(curr_total, prev_total),
            "paid_count_pct":  _pct_delta(curr_paid, prev_paid),
        },
    }


@router.get("/api/stats/staff-performance")
async def get_staff_performance(
    request: Request,
    staff_id: str = Query(..., description="Staff UUID"),
    weeks: int = Query(7, ge=1, le=26),
):
    """Weekly sales sparkline for a single staff member (table_orders only).

    Returns a week series of sales_total + tickets_count.
    Returns empty weeks array if staff not found or has no activity.
    Note: delivery orders have no staff author column; salon only.
    """
    restaurant = await get_current_restaurant(request)
    org_id = restaurant["id"]

    with tenant_scope(org_id):
        return await stats_repo.db_staff_performance(
            org_id=org_id,
            staff_id=staff_id,
            weeks=weeks,
        )


@router.get("/api/stats/tips-pool")
async def get_tips_pool(
    request: Request,
    period_start: str | None = Query(None),
    period_end:   str | None = Query(None),
    branch_id:    str | None = Query(None),
):
    """Tip pool summary for a period (default: current week).

    Wraps db_calculate_tips_by_attendance and returns pool_total, top-5
    entries_preview, unallocated amount, and my_pool (when called by staff).
    """
    restaurant = await get_current_restaurant(request)
    org_id = restaurant["id"]
    # `restaurant` is already the caller's own sede for anyone who cannot
    # span locations (app/routes/deps.py), so no org_id fallback here — an
    # org id in a location_id slot is the ambiguity rls-multitenant.md bans.
    location_id = restaurant.get("location_id")
    caller = await get_current_user(request)
    if may_span_locations(caller):
        bid = int(branch_id) if branch_id and branch_id.isdigit() else None
    else:
        # ?branch_id= used to be honoured for anyone: a waiter could read
        # another sede's tip pool by changing one number in the URL.
        bid = resolve_sede_filter(request, caller)
        location_id = location_id or bid

    # Detect if caller is a staff member (JWT claim "staff:<uuid>").
    # `caller` is already resolved above — the function-local
    # `from app.routes.deps import get_current_user` that used to sit here
    # shadowed the module-level name for the WHOLE function body, so the
    # sede resolution above raised UnboundLocalError.
    caller_staff_id: str | None = None
    username = caller.get("username") or caller.get("sub") or ""
    if username.startswith("staff:"):
        caller_staff_id = username[len("staff:"):]

    with tenant_scope(org_id):
        result = await stats_repo.db_tips_pool(
            org_id=org_id,
            location_id=location_id,
            period_start=period_start,
            period_end=period_end,
            branch_id=bid,
            caller_staff_id=caller_staff_id,
        )
    return result


# ── DASHBOARD ANALYTICS — TIER 5 (Churn + Branches) ─────────────────────────


@router.get("/api/stats/branches-consolidated")
async def get_branches_consolidated(
    request: Request,
    days: int = Query(7, ge=1, le=365, description="Rolling window in days"),
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Cross-location KPI roll-up for the sucursales page.

    Query params:
      days (int, default 7): rolling window for sales + ticket counts.

    Returns:
      period        — human-readable period label (e.g. "Últimos 7d")
      total_sales   — float, sum of paid orders + table orders in window
      total_tickets — int, count of orders in window
      avg_nps       — float | null, last 30d NPS average
      total_staff   — int, currently active staff count
      growth_yoy    — float | null, % growth vs same 30d window last year
    """
    org_id = restaurant["id"]
    return await stats_repo.db_branches_consolidated(org_id=org_id, days=days)


@router.get("/api/stats/branches-comparison")
async def get_branches_comparison(
    request: Request,
    days: int = Query(30, ge=1, le=365, description="Rolling window in days"),
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Per-location metric comparison matrix for the sucursales page.

    Query params:
      days (int, default 30): rolling window for sales / reservation metrics.

    Returns:
      locations — list of {id, name} for all locations in the org
      rows      — list of comparison rows, each with:
                    metric         — display label
                    per_location   — list of float|null, one per location (same order as locations)
                    avg            — float|null, average across non-null locations
                    target         — float|null, benchmark target (null if not defined)
                    top_location_id — int|null, id of the best-performing location
                    vs_target_pct   — float|null, % vs target (null if no target or no data)

    Metrics with null values: Rotación mesas/día, Food cost %, Costo nómina/ventas,
    Rotación de personal, Crecimiento YoY — pending additional schema/telemetry.
    """
    org_id = restaurant["id"]
    return await stats_repo.db_branches_comparison(org_id=org_id, days=days)