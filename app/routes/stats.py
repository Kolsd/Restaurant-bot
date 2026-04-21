import os
import json
from fastapi import APIRouter, Request, HTTPException, Query, Depends
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from app.services import database as db
from app.routes.deps import (
    require_auth,
    get_current_restaurant,
    get_current_user,
    get_current_restaurant_scoped,
)
from app.repositories import reviews_repo as rr, conversations_repo
from app.repositories import stats_repo
from app.services.tenant_context import tenant_scope
from app.services.logging import get_logger

_log = get_logger(__name__)

META_API_VERSION = os.getenv("META_API_VERSION", "v20.0")

router = APIRouter()

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
    """Si el usuario es de una sucursal, solo muestra los chats de sus mesas."""
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
    """Resolve the effective branch_id for stats filtering.

    Wave-2: the legacy `parent_restaurant_id` column was dropped in migration 0038.
    Non-admin users are now scoped to their own branch_id (their staff/user row).
    Admins honour the X-Branch-ID header as before.
    """
    branch_header = request.headers.get("X-Branch-ID")
    is_admin = any(r in user.get("role", "") for r in ["owner", "admin"])

    if is_admin:
        if branch_header == "all": return "all"
        elif branch_header == "matriz": return None
        elif branch_header and branch_header.isdigit(): return int(branch_header)
        return None

    return user.get("branch_id")

@router.get("/api/dashboard/sync")
async def dashboard_sync(request: Request, period: str = Query("today")):
    user = await get_current_user(request)
    restaurant = await get_current_restaurant(request)
    bot_number = restaurant["whatsapp_number"]
    date_from, date_to = get_date_range(period, get_tz(restaurant))
    branch_id = _resolve_branch_id(request, user, restaurant)
    effective_bot = await _get_effective_bot_number(restaurant)

    with tenant_scope(restaurant["id"]):
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

    by_date = {}
    current = datetime.strptime(date_from, "%Y-%m-%d").date()
    end     = datetime.strptime(date_to, "%Y-%m-%d").date()
    while current <= end:
        by_date[str(current)] = {"revenue": 0, "orders": 0, "paid": 0}
        current += timedelta(days=1)
        
    for o in orders:
        day = o["created_at"][:10]
        if day in by_date:
            by_date[day]["orders"] += 1
            if o["paid"]:
                by_date[day]["revenue"] += o["total"]
                by_date[day]["paid"]    += 1

    labels, revenue_data, orders_data = [], [], []
    for date_str, data in sorted(by_date.items()):
        d = datetime.strptime(date_str, "%Y-%m-%d")
        labels.append(d.strftime("%a %d") if period in ("today", "week") else d.strftime("%d/%m"))
        revenue_data.append(data["revenue"])
        orders_data.append(data["orders"])

    return {
        "stats": {
            "orders": {
                "total": len(orders), "paid": len(paid), "pending": len(pending), 
                "revenue": sum(o["total"] for o in paid),
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

@router.get("/api/dashboard/stats")
async def dashboard_stats(request: Request, period: str = Query("today")):
    user = await get_current_user(request)
    restaurant = await get_current_restaurant(request)
    bot_number = restaurant["whatsapp_number"]
    date_from, date_to = get_date_range(period, get_tz(restaurant))
    effective_bot = await _get_effective_bot_number(restaurant)

    with tenant_scope(restaurant["id"]):
        orders       = await db.db_get_orders_range(date_from, date_to, bot_number=bot_number)
        reservations = await db.db_get_reservations_range(date_from, date_to, bot_number=bot_number)
        all_convs    = await db.db_get_all_conversations(
            bot_number=effective_bot,
            date_from=date_from,
            date_to=date_to
        )
    paid         = [o for o in orders if o["paid"]]
    pending      = [o for o in orders if not o["paid"]]
    conversations = await filter_conversations_for_branch(all_convs, _resolve_branch_id(request, user, restaurant), effective_bot)

    return {
        "period": period, "date_from": date_from, "date_to": date_to,
        "orders": {
            "total": len(orders), "paid": len(paid), "pending": len(pending),
            "revenue": sum(o["total"] for o in paid),
            "pending_revenue": sum(o["total"] for o in pending)
        },
        "reservations": {
            "total": len(reservations),
            "guests": sum(r.get("guests", 0) for r in reservations)
        },
        "conversations": {"active": len(conversations)}
    }

@router.get("/api/dashboard/orders")
async def dashboard_orders(request: Request, period: str = Query("today")):
    restaurant = await get_current_restaurant(request)
    bot_number = restaurant["whatsapp_number"]
    date_from, date_to = get_date_range(period, get_tz(restaurant))
    with tenant_scope(restaurant["id"]):
        orders = await db.db_get_orders_range(date_from, date_to, bot_number=bot_number)
    result = []
    for o in orders:
        try:
            items = o.get("items", [])
            if isinstance(items, str):
                import json
                items = json.loads(items)
            items_summary = ", ".join(f"{i.get('quantity',1)}x {i.get('name','')}" for i in items) if isinstance(items, list) else str(items)
        except:
            items_summary = str(o.get("items", ""))
        created = datetime.fromisoformat(o["created_at"])
        result.append({
            "id": o["id"], "items": items_summary or "-", "type": o["order_type"],
            "paid": o["paid"], "total": o["total"], "address": o.get("address", ""),
            "status": o["status"],
            "time": created.strftime("%d/%m %H:%M") if period != "today" else created.strftime("%H:%M"),
            "phone": o.get("phone", "")
        })
    return {"orders": result}

@router.get("/api/dashboard/conversations")
async def get_conversations(request: Request):
    await require_auth(request)
    user = await get_current_user(request)
    restaurant = await get_current_restaurant(request)
    
    bot_number = restaurant.get("whatsapp_number", "")
    
    branch_id = restaurant["id"]

    branch_header = request.headers.get("X-Branch-ID")
    if branch_header and branch_header.isdigit() and any(r in user.get("role", "") for r in ["owner", "admin"]):
        branch_id = int(branch_header)

    with tenant_scope(restaurant["id"]):
        conversations = await db.db_get_all_conversations(bot_number=bot_number, branch_id=branch_id)
    return {"conversations": conversations}

@router.get("/api/dashboard/chart")
async def dashboard_chart(request: Request, period: str = Query("week")):
    restaurant = await get_current_restaurant(request)
    date_from, date_to = get_date_range(period, get_tz(restaurant))
    with tenant_scope(restaurant["id"]):
        orders = await db.db_get_orders_range(date_from, date_to, bot_number=restaurant["whatsapp_number"])
    by_date = {}
    current = datetime.strptime(date_from, "%Y-%m-%d").date()
    end     = datetime.strptime(date_to, "%Y-%m-%d").date()
    while current <= end:
        by_date[str(current)] = {"revenue": 0, "orders": 0, "paid": 0}
        current += timedelta(days=1)
    for o in orders:
        day = o["created_at"][:10]
        if day in by_date:
            by_date[day]["orders"] += 1
            if o["paid"]:
                by_date[day]["revenue"] += o["total"]
                by_date[day]["paid"]    += 1
    labels, revenue_data, orders_data = [], [], []
    for date_str, data in sorted(by_date.items()):
        d = datetime.strptime(date_str, "%Y-%m-%d")
        labels.append(d.strftime("%a %d") if period in ("today", "week") else d.strftime("%d/%m"))
        revenue_data.append(data["revenue"])
        orders_data.append(data["orders"])
    return {"labels": labels, "revenue": revenue_data, "orders": orders_data}

@router.get("/api/dashboard/menu")
async def dashboard_menu(request: Request):
    user = await get_current_user(request)
    restaurant = await get_current_restaurant(request)
    bot_number = restaurant.get("whatsapp_number", "")
    
    branch_header = request.headers.get("X-Branch-ID")
    
    if branch_header and branch_header.isdigit() and "owner" in user.get("role", ""):
        branch_id = int(branch_header)
        branch_rest = await db.db_get_restaurant_by_id(branch_id)
        if branch_rest and branch_rest.get("whatsapp_number"):
            bot_number = branch_rest["whatsapp_number"]
            
    return {"menu": await db.db_get_menu(bot_number) or {}}

@router.get("/api/menu/availability")
async def get_menu_availability(request: Request):
    await require_auth(request)
    user = await get_current_user(request)
    restaurant = await get_current_restaurant(request)

    # Wave-2: menu_availability rows are keyed by (dish_name, org_id) — the
    # same menu state applies to every branch of an org. Always scope by
    # org_id (restaurant["id"] is normalized to org_id post-Wave-2). The
    # X-Branch-ID header used to be honored here pre-Wave-2 as if menu
    # availability were per-branch; since it is not, the override is a
    # no-op for the org_id resolution and we leave it out.
    org_id = restaurant["id"]

    with tenant_scope(org_id):
        availability = await db.db_get_menu_availability(org_id)
    return {"availability": availability}

@router.post("/api/menu/availability")
async def set_dish_availability(request: Request):
    await require_auth(request)
    body = await request.json()
    if not body.get("dish_name"): raise HTTPException(status_code=400, detail="dish_name requerido")

    await get_current_user(request)
    restaurant = await get_current_restaurant(request)

    # Wave-2: same as GET — menu_availability is keyed by (dish_name, org_id).
    # The X-Branch-ID override would have written a row scoped to a
    # location_id, which violates the unique constraint shape and would
    # never be read back by GET (which scopes by org_id). Always use org_id.
    org_id = restaurant["id"]

    with tenant_scope(org_id):
        await db.db_set_dish_availability(
            restaurant_id=org_id,
            dish_name=body["dish_name"],
            available=body.get("available", True)
        )
    return {"success": True, "dish_name": body["dish_name"], "available": body.get("available", True)}

@router.post("/api/menu/sync-branches")
async def sync_menu_to_branches(request: Request):
    """
    Endpoint exclusivo para la Casa Matriz. Propaga el menú a todas las sucursales.
    """
    await require_auth(request)
    user = await get_current_user(request)
    restaurant = await get_current_restaurant(request)
    
    # Validaciones de seguridad
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
    Endpoint exclusivo para la Casa Matriz. Actualiza la estructura del JSON del menú.
    """
    await require_auth(request)
    user = await get_current_user(request)
    restaurant = await get_current_restaurant(request)
    
    if "owner" not in user.get("role", ""):
        raise HTTPException(status_code=403, detail="Solo el dueño puede editar el menú.")

    # Wave-2: parent_restaurant_id no longer exists. X-Branch-ID guard is sufficient.
    branch_header = request.headers.get("X-Branch-ID")
    if branch_header and branch_header != "matriz" and branch_header != "all":
        raise HTTPException(status_code=400, detail="Debes estar en la vista de la Casa Matriz para editar el menú.")

    body = await request.json()
    new_menu = body.get("menu")
    if not isinstance(new_menu, dict):
        raise HTTPException(status_code=400, detail="Formato de menú inválido.")

    # 🛡️ Validación estricta en backend: asegurar que los precios sean numéricos
    for cat, items in new_menu.items():
        for item in items:
            try:
                item["price"] = float(item.get("price", 0))
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

@router.post("/api/conversations/{phone}/pause")
async def pause_bot_for_conversation(phone: str, request: Request):
    restaurant = await get_current_restaurant(request)
    body = await request.json()
    with tenant_scope(restaurant["id"]):
        await db.db_toggle_bot(phone, restaurant["whatsapp_number"], body.get("paused", True))
    return {"success": True, "paused": body.get("paused", True)}

@router.post("/api/conversations/{phone}/reply")
async def manual_reply(phone: str, request: Request):
    import httpx as _httpx
    restaurant  = await get_current_restaurant(request)
    message     = (await request.json()).get("message", "").strip()
    if not message: raise HTTPException(status_code=400, detail="Mensaje vacio")

    with tenant_scope(restaurant["id"]):
        # Usar bot_number real de la conversación (puede diferir en setup multi-sucursal)
        details    = await db.db_get_conversation_details(phone, restaurant["whatsapp_number"])
        actual_bot = details.get("bot_number") or restaurant["whatsapp_number"]
        history    = details.get("history", [])
        history.append({"role": "assistant", "content": f"[Humano] {message}"})
        await db.db_save_history(phone, actual_bot, history)

    # wa_phone_id y wa_access_token viven en la tabla restaurants, no en env vars
    meta_token = restaurant.get("wa_access_token") or os.getenv("META_ACCESS_TOKEN", "")
    phone_id   = restaurant.get("wa_phone_id")    or os.getenv("META_PHONE_NUMBER_ID", "")
    if meta_token and phone_id:
        try:
            async with _httpx.AsyncClient(timeout=8) as client:
                res = await client.post(
                    f"https://graph.facebook.com/{META_API_VERSION}/{phone_id}/messages",
                    headers={"Authorization": f"Bearer {meta_token}"},
                    json={"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": message}},
                )
                if res.status_code != 200:
                    raise HTTPException(status_code=502, detail=f"Meta error {res.status_code}: {res.text[:200]}")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Error enviando a WhatsApp: {e}")
    else:
        raise HTTPException(status_code=503, detail="wa_phone_id o wa_access_token no configurados para este restaurante")
    return {"success": True}


# ── ADVANCED ANALYTICS ───────────────────────────────────────────────────────

@router.get("/api/stats/turn-time")
async def get_turn_time_stats(
    request: Request,
    period_start: str = Query(...),
    period_end: str = Query(...),
    branch_id: str | None = Query(None),
):
    """Average, min and max table turn times (closed sessions) for a period."""
    restaurant = await get_current_restaurant(request)
    bot_number = restaurant["whatsapp_number"]
    bid = int(branch_id) if branch_id and branch_id.isdigit() else None
    with tenant_scope(restaurant["id"]):
        stats = await rr.db_get_turn_time_stats(bot_number, period_start, period_end, branch_id=bid)
    return stats


@router.get("/api/stats/occupancy")
async def get_occupancy_stats(
    request: Request,
    period_start: str = Query(...),
    period_end: str = Query(...),
    branch_id: str | None = Query(None),
):
    """Aggregated occupancy and utilization rates from 15-min snapshots."""
    restaurant = await get_current_restaurant(request)
    bid = int(branch_id) if branch_id and branch_id.isdigit() else None
    with tenant_scope(restaurant["id"]):
        stats = await rr.db_get_occupancy_stats(
            restaurant["id"], period_start, period_end, branch_id=bid
        )
    return stats


@router.get("/api/stats/no-show-rate")
async def get_no_show_rate(
    request: Request,
    period_start: str = Query(...),
    period_end: str = Query(...),
    branch_id: str | None = Query(None),
):
    """No-show rate derived from reservation stats for the given period."""
    restaurant = await get_current_restaurant(request)
    bot_number = restaurant["whatsapp_number"]
    bid = int(branch_id) if branch_id and branch_id.isdigit() else None
    with tenant_scope(restaurant["id"]):
        stats = await db.db_get_reservation_stats(
            bot_number,
            date_from=period_start,
            date_to=period_end,
            branch_id=bid,
        )
    total = stats.get("total", 0) or 0
    no_shows = stats.get("no_show", 0) or 0
    rate = round(no_shows / total * 100, 1) if total > 0 else 0.0
    return {
        "total_reservations": total,
        "no_shows": no_shows,
        "no_show_rate_pct": rate,
    }


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

    with tenant_scope(org_id):
        return await stats_repo.db_inventory_critical(
            org_id=org_id,
            ok_limit=ok_limit,
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


@router.get("/api/stats/customers-at-risk")
async def get_customers_at_risk(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
):
    """Frequent customers who haven't ordered in >= 21 days.

    Returns count + customer list with last_seen, days_since, total_spent.
    """
    restaurant = await get_current_restaurant(request)
    org_id = restaurant["id"]

    with tenant_scope(org_id):
        return await stats_repo.db_customers_at_risk(org_id=org_id, limit=limit)


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
    location_id = restaurant.get("location_id") or restaurant["id"]
    bid = int(branch_id) if branch_id and branch_id.isdigit() else None

    # Detect if caller is a staff member (JWT claim "staff:<uuid>")
    caller_staff_id: str | None = None
    try:
        from app.routes.deps import get_current_user  # noqa: PLC0415
        caller = await get_current_user(request)
        username = caller.get("username") or caller.get("sub") or ""
        if username.startswith("staff:"):
            caller_staff_id = username[len("staff:"):]
    except Exception:
        pass  # admin callers without staff token — my_pool stays None

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


# ── DASHBOARD ANALYTICS — TIER 4a ────────────────────────────────────────────


@router.get("/api/stats/daily-insight")
async def get_daily_insight(request: Request):
    """LLM-generated daily commentary for the dashboard banner.

    Feature-flagged via restaurants.features.ai_daily_insight (JSONB).
    If disabled, returns {"enabled": false, "reason": "feature_disabled"} (200 OK).
    If enabled, checks Redis cache (key: mesio:ai_insight:{org_id}:{YYYY-MM-DD}).
      Cache hit  → cached JSON (24h TTL).
      Cache miss → gather signals, call Haiku, cache 24h, return.
    Missing ANTHROPIC_API_KEY or Anthropic error → signals-only payload (no crash).
    """
    from app.services.ai_insights import generate_daily_insight  # noqa: PLC0415

    restaurant = await get_current_restaurant(request)
    org_id = restaurant["id"]
    location_id = restaurant.get("location_id") or org_id

    # Feature gate: read ai_daily_insight from features JSONB
    features = restaurant.get("features", {})
    if isinstance(features, str):
        try:
            features = json.loads(features)
        except Exception:
            features = {}

    if not features.get("ai_daily_insight"):
        return {"enabled": False, "reason": "feature_disabled"}

    with tenant_scope(org_id):
        return await generate_daily_insight(org_id=org_id, location_id=location_id)


# ── DASHBOARD ANALYTICS — TIER 5 (Churn + Branches) ─────────────────────────


@router.get("/api/stats/churn-summary")
async def get_churn_summary(
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Churn risk aggregate for the clientes-riesgo page.

    Bins customer_profiles by recency-based churn_score:
      high   (score >= 0.80): dormant >= 56 days, ≥3 orders
      medium (0.50–0.79):     dormant 35–55 days, ≥3 orders
      watch  (0.30–0.49):     dormant 14–34 days, ≥2 orders

    Returns:
      high_count, medium_count, watch_count   — integer bin sizes
      ltv_sum                                 — float, total_spent sum for high+medium
      reactivated_count                       — int (0 until schema supports it)
      medium_risk                             — list[dict], top 6 medium-bin customers,
                                               each with: name, phone, churn_score,
                                               days_since, total_visits
    """
    org_id = restaurant["id"]
    return await stats_repo.db_churn_summary(org_id=org_id)


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