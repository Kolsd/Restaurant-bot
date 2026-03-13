from fastapi import APIRouter, Request, HTTPException
from datetime import datetime
from app.services.orders import orders, get_all_orders
from app.services.auth import verify_token
from app.data.restaurant import reservations
from app.services.agent import conversation_history

router = APIRouter()


def require_auth(request: Request) -> str:
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        token = request.cookies.get("rb_token", "")
    username = verify_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="No autorizado")
    return username


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ─────────────────────────────────────────────
# STATS GENERALES
# ─────────────────────────────────────────────

@router.get("/api/dashboard/stats")
async def dashboard_stats(request: Request):
    require_auth(request)
    today = today_str()

    all_orders = get_all_orders()
    today_orders = [o for o in all_orders if o["created_at"].startswith(today)]
    paid_orders = [o for o in today_orders if o["paid"]]
    pending_orders = [o for o in today_orders if not o["paid"]]
    revenue = sum(o["total"] for o in paid_orders)

    today_reservations = [r for r in reservations if r.get("date", "") == today]
    total_guests = sum(r.get("guests", 0) for r in today_reservations)

    active_convs = len(conversation_history)

    return {
        "date": today,
        "orders": {
            "total": len(today_orders),
            "paid": len(paid_orders),
            "pending": len(pending_orders),
            "revenue": revenue
        },
        "reservations": {
            "total": len(today_reservations),
            "guests": total_guests
        },
        "conversations": {
            "active": active_convs
        }
    }


# ─────────────────────────────────────────────
# PEDIDOS
# ─────────────────────────────────────────────

@router.get("/api/dashboard/orders")
async def dashboard_orders(request: Request):
    require_auth(request)
    today = today_str()
    all_orders = get_all_orders()
    today_orders = [o for o in all_orders if o["created_at"].startswith(today)]
    today_orders.sort(key=lambda x: x["created_at"], reverse=True)

    result = []
    for o in today_orders:
        items_summary = ", ".join(
            f"{item['quantity']}x {item['name']}" for item in o.get("items", [])
        )
        result.append({
            "id": o["id"],
            "items": items_summary or "—",
            "type": o["order_type"],
            "paid": o["paid"],
            "total": o["total"],
            "address": o.get("address", ""),
            "status": o["status"],
            "time": datetime.fromisoformat(o["created_at"]).strftime("%H:%M"),
            "phone": o.get("phone", "")
        })
    return {"orders": result}


# ─────────────────────────────────────────────
# RESERVACIONES
# ─────────────────────────────────────────────

@router.get("/api/dashboard/reservations")
async def dashboard_reservations(request: Request):
    require_auth(request)
    today = today_str()
    today_res = [r for r in reservations if r.get("date", "") == today]
    today_res.sort(key=lambda x: x.get("time", ""))
    return {"reservations": today_res}


# ─────────────────────────────────────────────
# CONVERSACIONES
# ─────────────────────────────────────────────

@router.get("/api/dashboard/conversations")
async def dashboard_conversations(request: Request):
    require_auth(request)
    result = []
    for phone, history in conversation_history.items():
        if not history:
            continue
        last_msg = history[-1]
        last_user_msg = next(
            (m["content"] for m in reversed(history) if m["role"] == "user"), ""
        )
        result.append({
            "phone": phone,
            "messages": len(history),
            "last_message": last_user_msg[:80] if last_user_msg else "...",
            "last_role": last_msg["role"],
            "preview": last_user_msg[:60] if last_user_msg else "..."
        })
    result.sort(key=lambda x: x["messages"], reverse=True)
    return {"conversations": result}
