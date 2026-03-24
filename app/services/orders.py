import hashlib
import uuid
import os
import asyncio
from datetime import datetime, timezone, timedelta
from app.services import database as db

# ── FIX: Zona Horaria de Colombia (UTC -5) ──
COT = timezone(timedelta(hours=-5))

DELIVERY_FEE = 5000

# 🚨 FIX SEGURIDAD: Eliminamos los fallbacks en texto plano
WOMPI_PUBLIC_KEY = os.getenv("WOMPI_PUBLIC_KEY")
WOMPI_INTEGRITY_SECRET = os.getenv("WOMPI_INTEGRITY_SECRET")

# 🚀 FIX CARRERA: Diccionario de candados (Locks) por teléfono
_cart_locks = {}

def _get_cart_lock(phone: str) -> asyncio.Lock:
    if phone not in _cart_locks:
        _cart_locks[phone] = asyncio.Lock()
    return _cart_locks[phone]

async def find_dish(dish_name: str, bot_number: str) -> dict | None:
    menu = await db.db_get_menu(bot_number)
    if not menu: 
        return None
    name_lower = dish_name.lower().strip()
    for category, dishes in menu.items():
        for dish in dishes:
            if name_lower in dish["name"].lower() or dish["name"].lower() in name_lower:
                return {**dish, "category": category}
    return None

async def add_to_cart(phone: str, dish_name: str, quantity: int, bot_number: str) -> dict:
    dish = await find_dish(dish_name, bot_number)
    if not dish:
        return {"success": False, "error": f"No encontré '{dish_name}' en el menú"}
    
    # 🚀 FIX CARRERA: Bloqueamos el acceso al carrito SOLO para este usuario
    async with _get_cart_lock(phone):
        cart = await db.db_get_cart(phone, bot_number)
        
        found = False
        for item in cart["items"]:
            if item["name"] == dish["name"]:
                item["quantity"] += quantity
                item["subtotal"] = item["price"] * item["quantity"]
                found = True
                break
                
        if not found:
            cart["items"].append({
                "name": dish["name"], "price": dish["price"],
                "quantity": quantity, "subtotal": dish["price"] * quantity,
                "category": dish["category"]
            })
            
        await db.db_save_cart(phone, bot_number, cart)
        
    return {"success": True, "cart": cart, "dish": dish}

async def remove_from_cart(phone: str, dish_name: str, bot_number: str) -> dict:
    dish = await find_dish(dish_name, bot_number)
    if not dish: 
        return {"success": False, "error": "Plato no encontrado"}

    # Bloqueamos también al eliminar, por si acaso
    async with _get_cart_lock(phone):
        cart = await db.db_get_cart(phone, bot_number)
        cart["items"] = [i for i in cart["items"] if i["name"] != dish["name"]]
        await db.db_save_cart(phone, bot_number, cart)
        
    return {"success": True, "cart": cart}

async def clear_cart(phone: str, bot_number: str):
    async with _get_cart_lock(phone):
        await db.db_clear_cart(phone, bot_number)

async def get_cart_total(phone: str, bot_number: str) -> int:
    cart = await db.db_get_cart(phone, bot_number)
    return sum(item["subtotal"] for item in cart["items"])

async def cart_summary(phone: str, bot_number: str) -> str:
    cart = await db.db_get_cart(phone, bot_number)
    if not cart["items"]: 
        return "Tu carrito está vacío."
    
    lines = [f"• {i['quantity']}x {i['name']} — ${i['subtotal']:,}" for i in cart["items"]]
    total = sum(item["subtotal"] for item in cart["items"])
    tipo = cart.get("order_type", "")
    extra = " (+ $5,000 domicilio)" if tipo == "domicilio" else ""
    lines.append(f"\n*Total: ${total:,} COP{extra}*")
    return "\n".join(lines)

def generate_wompi_payment_link(order_id: str, amount_cop: int) -> str:
    amount_cents = amount_cop * 100
    # Validamos que el secreto exista para no generar un hash erróneo
    secret = WOMPI_INTEGRITY_SECRET or ""
    signature_string = f"{order_id}{amount_cents}COP{secret}"
    signature = hashlib.sha256(signature_string.encode()).hexdigest()
    return f"https://checkout.wompi.co/p/?public-key={WOMPI_PUBLIC_KEY}&currency=COP&amount-in-cents={amount_cents}&reference={order_id}&signature:integrity={signature}&redirect-url=https://mesioai.com/api/payment/confirm"

async def create_order(phone: str, order_type: str, address: str, notes: str, bot_number: str, payment_method: str = "") -> dict:
    async with _get_cart_lock(phone):
        cart = await db.db_get_cart(phone, bot_number)
        if not cart["items"]:
            return {"success": False, "error": "El carrito está vacío"}
        if order_type == "domicilio" and not address:
            return {"success": False, "error": "Se necesita dirección de entrega"}

        subtotal = sum(item["subtotal"] for item in cart["items"])
        delivery_fee = DELIVERY_FEE if order_type == "domicilio" else 0
        total = subtotal + delivery_fee

        # ── Buscar orden activa para este teléfono (aún no en camino/entregada) ──
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                """SELECT id, items, subtotal, total, address, notes, payment_method
                   FROM orders
                   WHERE phone=$1 AND bot_number=$2
                     AND order_type=$3
                     AND status NOT IN ('en_camino','entregado','cancelado')
                   ORDER BY created_at DESC LIMIT 1""",
                phone, bot_number, order_type
            )

        if existing:
            # ── MODO ADICIONAL: agregar items a la orden existente ──
            import json as _json
            existing_items = existing["items"]
            if isinstance(existing_items, str):
                try: existing_items = _json.loads(existing_items)
                except: existing_items = []

            # Merge items — acumula cantidades si el plato ya está
            merged = {i["name"]: i for i in existing_items}
            for item in cart["items"]:
                if item["name"] in merged:
                    merged[item["name"]]["quantity"] += item["quantity"]
                    merged[item["name"]]["subtotal"] += item["subtotal"]
                else:
                    merged[item["name"]] = item.copy()

            new_items     = list(merged.values())
            new_subtotal  = sum(i["subtotal"] for i in new_items)
            new_total     = new_subtotal + (DELIVERY_FEE if order_type == "domicilio" else 0)
            order_id      = existing["id"]

            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE orders
                       SET items=$1, subtotal=$2, total=$3, status='confirmado'
                       WHERE id=$4""",
                    _json.dumps(new_items), new_subtotal, new_total, order_id
                )

            await db.db_clear_cart(phone, bot_number)

            order = {
                "id": order_id,
                "phone": phone,
                "items": new_items,
                "order_type": order_type,
                "address": address or existing.get("address", ""),
                "notes": notes or existing.get("notes", ""),
                "subtotal": new_subtotal,
                "delivery_fee": delivery_fee,
                "total": new_total,
                "status": "confirmado",
                "paid": False,
                "bot_number": bot_number,
                "payment_method": payment_method or existing.get("payment_method", ""),
                "payment_url": generate_wompi_payment_link(order_id, new_total),
                "is_additional": True,
            }
            return {"success": True, "order": order}

        else:
            # ── ORDEN NUEVA ──
            order_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"
            order = {
                "id": order_id,
                "phone": phone,
                "items": cart["items"].copy(),
                "order_type": order_type,
                "address": address or "",
                "notes": notes,
                "subtotal": subtotal,
                "delivery_fee": delivery_fee,
                "total": total,
                "status": "confirmado",
                "paid": False,
                "created_at": datetime.now(COT).isoformat(),
                "bot_number": bot_number,
                "payment_method": payment_method,
                "payment_url": generate_wompi_payment_link(order_id, total),
                "is_additional": False,
            }
            await db.db_clear_cart(phone, bot_number)
            return {"success": True, "order": order}