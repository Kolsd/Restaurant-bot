import os
from fastapi import APIRouter, Request, HTTPException, Query
from datetime import datetime, timedelta
from app.services.auth import verify_token
from app.services import database as db

router = APIRouter()


def require_auth(request: Request) -> str:
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    username = verify_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="No autorizado")
    return username


async def get_current_restaurant(request: Request) -> dict:
    username = require_auth(request)
    user = await db.db_get_user(username)
    if not user:
        raise HTTPException(status_code=401, detail="Usuario no encontrado")
    branch_id = user.get("branch_id")
    if branch_id:
        restaurant = await db.db_get_restaurant_by_id(branch_id)
        if restaurant:
            return restaurant
    all_restaurants = await db.db_get_all_restaurants()
    for r in all_restaurants:
        if r["name"].lower().strip() == user["restaurant_name"].lower().strip():
            return r
    if all_restaurants:
        return all_restaurants[0]
    raise HTTPException(status_code=403, detail="Restaurante no encontrado")


def get_date_range(period: str):
    today = datetime.now().date()
    if period == "today":
        return str(today), str(today)
    elif period == "week":
        return str(today - timedelta(days=6)), str(today)
    elif period == "month":
        return str(today.replace(day=1)), str(today)
    elif period == "semester":
        if today.month <= 6:
            return str(today.replace(month=1, day=1)), str(today)
        else:
            return str(today.replace(month=7, day=1)), str(today)
    elif period == "year":
        return str(today.replace(month=1, day=1)), str(today)
    else:
        return str(today), str(today)


@router.get("/api/dashboard/stats")
async def dashboard_stats(request: Request, period: str = Query("today")):
    restaurant = await get_current_restaurant(request)
    bot_number = restaurant["whatsapp_number"]
    date_from, date_to = get_date_range(period)
    orders = await db.db_get_orders_range(date_from, date_to, bot_number=bot_number)
    paid = [o for o in orders if o["paid"]]
    pending = [o for o in orders if not o["paid"]]
    revenue = sum(o["total"] for o in paid)
    pending_revenue = sum(o["total"] for o in pending)
    reservations = await db.db_get_reservations_range(date_from, date_to, bot_number=bot_number)
    total_guests = sum(r.get("guests", 0) for r in reservations)
    convs = await db.db_get_all_conversations(bot_number=bot_number)
    return {
        "period": period,
        "date_from": date_from,
        "date_to": date_to,
        "orders": {"total": len(orders), "paid": len(paid), "pending": len(pending),
                   "revenue": revenue, "pending_revenue": pending_revenue},
        "reservations": {"total": len(reservations), "guests": total_guests},
        "conversations": {"active": len(convs)}
    }


@router.get("/api/dashboard/orders")
async def dashboard_orders(request: Request, period: str = Query("today")):
    restaurant = await get_current_restaurant(request)
    bot_number = restaurant["whatsapp_number"]
    date_from, date_to = get_date_range(period)
    orders = await db.db_get_orders_range(date_from, date_to, bot_number=bot_number)
    result = []
    for o in orders:
        try:
            items = o.get("items", [])
            if isinstance(items, str):
                import json
                items = json.loads(items)
            items_summary = ", ".join(f"{i.get('quantity',1)}x {i.get('name','')}" for i in items) if isinstance(items, list) else str(items)
        except Exception:
            items_summary = str(o.get("items", ""))
        created = datetime.fromisoformat(o["created_at"])
        result.append({
            "id": o["id"],
            "items": items_summary or "-",
            "type": o["order_type"],
            "paid": o["paid"],
            "total": o["total"],
            "address": o.get("address", ""),
            "status": o["status"],
            "time": created.strftime("%d/%m %H:%M") if period != "today" else created.strftime("%H:%M"),
            "phone": o.get("phone", "")
        })
    return {"orders": result}


@router.get("/api/dashboard/reservations")
async def dashboard_reservations(request: Request, period: str = Query("today")):
    restaurant = await get_current_restaurant(request)
    bot_number = restaurant["whatsapp_number"]
    date_from, date_to = get_date_range(period)
    reservations = await db.db_get_reservations_range(date_from, date_to, bot_number=bot_number)
    return {"reservations": reservations}


@router.get("/api/dashboard/conversations")
async def dashboard_conversations(request: Request):
    restaurant = await get_current_restaurant(request)
    bot_number = restaurant["whatsapp_number"]
    convs = await db.db_get_all_conversations(bot_number=bot_number)
    return {"conversations": convs}


@router.get("/api/dashboard/chart")
async def dashboard_chart(request: Request, period: str = Query("week")):
    restaurant = await get_current_restaurant(request)
    bot_number = restaurant["whatsapp_number"]
    date_from, date_to = get_date_range(period)
    orders = await db.db_get_orders_range(date_from, date_to, bot_number=bot_number)
    by_date: dict = {}
    start = datetime.strptime(date_from, "%Y-%m-%d").date()
    end = datetime.strptime(date_to, "%Y-%m-%d").date()
    current = start
    while current <= end:
        by_date[str(current)] = {"revenue": 0, "orders": 0, "paid": 0}
        current += timedelta(days=1)
    for o in orders:
        day = o["created_at"][:10]
        if day in by_date:
            by_date[day]["orders"] += 1
            if o["paid"]:
                by_date[day]["revenue"] += o["total"]
                by_date[day]["paid"] += 1
    labels, revenue_data, orders_data = [], [], []
    for date_str, data in sorted(by_date.items()):
        d = datetime.strptime(date_str, "%Y-%m-%d")
        labels.append(d.strftime("%a %d") if period in ("today", "week") else d.strftime("%d/%m"))
        revenue_data.append(data["revenue"])
        orders_data.append(data["orders"])
    return {"labels": labels, "revenue": revenue_data, "orders": orders_data}


@router.get("/api/dashboard/menu")
async def dashboard_menu(request: Request):
    restaurant = await get_current_restaurant(request)
    bot_number = restaurant["whatsapp_number"]
    menu = await db.db_get_menu(bot_number) or {}
    return {"menu": menu}


@router.get("/api/menu/availability")
async def get_menu_availability(request: Request):
    require_auth(request)
    avail = await db.db_get_menu_availability()
    return {"availability": avail}


@router.post("/api/menu/availability")
async def set_dish_availability(request: Request):
    require_auth(request)
    body = await request.json()
    dish_name = body.get("dish_name")
    available = body.get("available", True)
    if not dish_name:
        raise HTTPException(status_code=400, detail="dish_name requerido")
    await db.db_set_dish_availability(dish_name, available)
    return {"success": True, "dish_name": dish_name, "available": available}


@router.delete("/api/conversations/cleanup")
async def cleanup_conversations(request: Request):
    restaurant = await get_current_restaurant(request)
    bot_number = restaurant["whatsapp_number"]
    result = await db.db_cleanup_old_conversations(days=7, bot_number=bot_number)
    return {"success": True, "result": str(result)}


@router.get("/api/conversations/{phone}")
async def get_conversation(phone: str, request: Request):
    restaurant = await get_current_restaurant(request)
    bot_number = restaurant["whatsapp_number"]
    details = await db.db_get_conversation_details(phone, bot_number)
    return {
        "phone": phone,
        "history": details.get("history", []),
        "bot_paused": details.get("bot_paused", False)
    }


@router.post("/api/conversations/{phone}/pause")
async def pause_bot_for_conversation(phone: str, request: Request):
    restaurant = await get_current_restaurant(request)
    bot_number = restaurant["whatsapp_number"]
    body = await request.json()
    paused = body.get("paused", True)
    await db.db_toggle_bot(phone, bot_number, paused)
    return {"success": True, "paused": paused}


@router.post("/api/conversations/{phone}/reply")
async def manual_reply(phone: str, request: Request):
    import httpx as _httpx
    restaurant = await get_current_restaurant(request)
    bot_number = restaurant["whatsapp_number"]
    body = await request.json()
    message = body.get("message", "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Mensaje vacio")
    details = await db.db_get_conversation_details(phone, bot_number)
    history = details.get("history", [])
    history.append({"role": "assistant", "content": f"[Humano] {message}"})
    await db.db_save_history(phone, bot_number, history)
    meta_token = os.getenv("META_ACCESS_TOKEN", "")
    phone_id = os.getenv("META_PHONE_NUMBER_ID", "")
    if meta_token and phone_id:
        try:
            async with _httpx.AsyncClient(timeout=8) as client:
                await client.post(
                    f"https://graph.facebook.com/v18.0/{phone_id}/messages",
                    headers={"Authorization": f"Bearer {meta_token}"},
                    json={"messaging_product": "whatsapp", "to": phone,
                          "type": "text", "text": {"body": message}}
                )
        except Exception as e:
            print(f"Meta send error: {e}")
    return {"success": True}