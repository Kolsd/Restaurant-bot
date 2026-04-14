import contextlib
import hashlib
import json
import uuid
import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from app.services import database as db
from app.services import state_store
from app.services.logging import get_logger
from app.services.money import to_decimal, money_mul, money_sum, ZERO

APP_DOMAIN = os.getenv("APP_DOMAIN", "")

WOMPI_PUBLIC_KEY = os.getenv("WOMPI_PUBLIC_KEY")
WOMPI_INTEGRITY_SECRET = os.getenv("WOMPI_INTEGRITY_SECRET")

log = get_logger(__name__)


@contextlib.asynccontextmanager
async def _cart_lock(phone: str, bot_number: str, ttl_seconds: int = 30):
    """
    Async context manager that acquires a distributed cart lock for (phone, bot_number).
    Uses Redis SET NX EX when available, falls back to an asyncio.Lock per worker.
    Raises RuntimeError if the lock cannot be acquired (e.g. already held by another request).
    """
    token = await state_store.cart_lock_acquire(phone, bot_number, ttl_seconds=ttl_seconds)
    if not token:
        log.warning("cart_lock.contention", phone=phone, bot_number=bot_number)
        raise RuntimeError("cart_lock_contention")
    try:
        yield
    finally:
        await state_store.cart_lock_release(phone, bot_number, token=token)

async def find_dish(dish_name: str, bot_number: str) -> dict | None:
    menu = await db.db_get_menu(bot_number)
    if not menu:
        return None
    name_lower = dish_name.lower().strip()

    # Pass 1: exact match (case-insensitive)
    for category, dishes in menu.items():
        for dish in dishes:
            if dish["name"].lower().strip() == name_lower:
                return {**dish, "category": category}

    # Pass 2: substring matches — collect all and pick the one whose length is
    # closest to the query (avoids always returning the first-inserted item).
    # Require the search term to be at least 40% of the menu item name length
    # to avoid matching a 2-char query against a 30-char name.
    candidates = []
    for category, dishes in menu.items():
        for dish in dishes:
            dish_lower = dish["name"].lower()
            item_len = len(dish_lower)
            query_len = len(name_lower)
            if item_len == 0:
                continue
            if name_lower in dish_lower and query_len / item_len >= 0.4:
                candidates.append({**dish, "category": category})
            elif (dish_lower in name_lower) and (query_len / item_len >= 0.4 if item_len > 0 else False):
                candidates.append({**dish, "category": category})

    if not candidates:
        return None

    # Prefer the dish whose name length is closest to the query length
    candidates.sort(key=lambda d: abs(len(d["name"]) - len(dish_name)))
    return candidates[0]

async def add_to_cart(phone: str, dish_name: str, quantity: int, bot_number: str) -> dict:
    if quantity <= 0:
        return {"success": False, "error": "La cantidad debe ser mayor a cero"}

    dish = await find_dish(dish_name, bot_number)
    if not dish:
        return {"success": False, "error": f"No encontré '{dish_name}' en el menú"}

    try:
        async with _cart_lock(phone, bot_number):
            cart = await db.db_get_cart(phone, bot_number)

            found = False
            for item in cart["items"]:
                if item["name"] == dish["name"]:
                    item["quantity"] += quantity
                    item["subtotal"] = float(money_mul(to_decimal(item["price"]), item["quantity"]))  # JSON boundary
                    found = True
                    break

            if not found:
                cart["items"].append({
                    "name": dish["name"], "price": dish["price"],
                    "quantity": quantity, "subtotal": float(money_mul(to_decimal(dish["price"]), quantity)),  # JSON boundary
                    "category": dish["category"]
                })

            await db.db_save_cart(phone, bot_number, cart)
    except RuntimeError as exc:
        if "cart_lock_contention" in str(exc):
            return {"success": False, "error": "Tu pedido está siendo procesado, por favor espera un momento."}
        raise

    return {"success": True, "cart": cart, "dish": dish}

async def remove_from_cart(phone: str, dish_name: str, bot_number: str) -> dict:
    dish = await find_dish(dish_name, bot_number)
    if not dish:
        return {"success": False, "error": "Plato no encontrado"}

    try:
        async with _cart_lock(phone, bot_number):
            cart = await db.db_get_cart(phone, bot_number)
            original_count = len(cart["items"])
            cart["items"] = [i for i in cart["items"] if i["name"].lower() != dish["name"].lower()]
            if len(cart["items"]) == original_count:
                return {"success": False, "error": f"{dish['name']} no está en tu pedido."}
            await db.db_save_cart(phone, bot_number, cart)
    except RuntimeError as exc:
        if "cart_lock_contention" in str(exc):
            return {"success": False, "error": "Tu pedido está siendo procesado, por favor espera un momento."}
        raise

    return {"success": True, "cart": cart}

async def clear_cart(phone: str, bot_number: str):
    try:
        async with _cart_lock(phone, bot_number):
            await db.db_clear_cart(phone, bot_number)
    except RuntimeError as e:
        if str(e) == "cart_lock_contention":
            log.warning("cart.clear_lock_contention", phone=phone)
            return
        raise


async def migrate_cart(phone: str, from_bot_number: str, to_bot_number: str):
    """Migrate cart from one bot_number to another under a distributed lock."""
    keys = sorted([from_bot_number, to_bot_number])
    try:
        async with _cart_lock(phone, keys[0]):
            async with _cart_lock(phone, keys[1]):
                await db.db_migrate_cart(phone, from_bot_number, to_bot_number)
    except RuntimeError as e:
        if str(e) == "cart_lock_contention":
            log.warning("cart.migrate_lock_contention", phone=phone)
            return
        raise
        
async def get_cart_total(phone: str, bot_number: str) -> float:
    cart = await db.db_get_cart(phone, bot_number)
    return sum(item["subtotal"] for item in cart["items"])

async def cart_summary(phone: str, bot_number: str) -> str:
    cart = await db.db_get_cart(phone, bot_number)
    if not cart["items"]:
        return "Cart is empty."

    lines = [f"• {i['quantity']}x {i['name']} — {i['subtotal']:,}" for i in cart["items"]]
    total = sum(item["subtotal"] for item in cart["items"])
    lines.append(f"\n*Total: {total:,}*")
    return "\n".join(lines)

# Zero-decimal currencies (no cents multiplier needed)
_ZERO_DECIMAL_CURRENCIES = {"COP", "CLP", "JPY", "KRW", "VND", "PYG", "ISK"}


def generate_wompi_payment_link(order_id: str, amount: int, currency: str = "COP") -> str:
    if currency in _ZERO_DECIMAL_CURRENCIES:
        amount_cents = int(amount)
    else:
        amount_cents = int(amount * 100)
    secret = WOMPI_INTEGRITY_SECRET or ""
    signature_string = f"{order_id}{amount_cents}{currency}{secret}"
    signature = hashlib.sha256(signature_string.encode()).hexdigest()
    redirect_base = f"https://{APP_DOMAIN}" if APP_DOMAIN else ""
    redirect_url = f"{redirect_base}/api/payment/confirm"
    return f"https://checkout.wompi.co/p/?public-key={WOMPI_PUBLIC_KEY}&currency={currency}&amount-in-cents={amount_cents}&reference={order_id}&signature:integrity={signature}&redirect-url={redirect_url}"

async def create_order(phone: str, order_type: str, address: str, notes: str, bot_number: str, payment_method: str = "") -> dict:
    from app.repositories.orders_repo import commit_order_transaction, OrderCommitError, InsufficientStockError

    try:
        async with _cart_lock(phone, bot_number):
            cart = await db.db_get_cart(phone, bot_number)
            if not cart["items"]:
                return {"success": False, "error": "El carrito está vacío"}
            if order_type == "domicilio" and not address:
                return {"success": False, "error": "Se necesita dirección de entrega"}

            rest_data = await db.db_get_restaurant_by_phone(bot_number)
            delivery_fee = ZERO
            tz_str = "UTC"
            restaurant_id = None
            if rest_data:
                restaurant_id = rest_data.get("id")
                _raw_feats = rest_data.get("features") or {}
                if isinstance(_raw_feats, str):
                    try:
                        _raw_feats = json.loads(_raw_feats)
                    except Exception:
                        _raw_feats = {}
                if not isinstance(_raw_feats, dict):
                    _raw_feats = {}

                delivery_fee = to_decimal(_raw_feats.get("delivery_fee", 0)) if order_type == "domicilio" else ZERO
                tz_str = _raw_feats.get("timezone", "UTC")

            subtotal = sum(to_decimal(item["subtotal"]) for item in cart["items"])
            total = subtotal + delivery_fee

            # Use a SINGLE connection for all transit/status reads to eliminate TOCTOU races
            pool = await db.get_pool()
            async with pool.acquire() as conn:
                base_order = await conn.fetchrow(
                    """SELECT id, address, notes, payment_method, status
                       FROM orders
                       WHERE phone=$1 AND bot_number=$2
                         AND order_type=$3
                         AND (base_order_id IS NULL OR base_order_id = id)
                         AND status NOT IN ('entregado','cancelado')
                       ORDER BY created_at DESC LIMIT 1""",
                    phone, bot_number, order_type
                )

                if base_order:
                    current_status = base_order["status"]
                    if current_status in ("en_camino", "en_puerta"):
                        return {"success": False, "error": "in_transit", "blocked_in_transit": True}

                    base_id = base_order["id"]
                    # Re-check status within the same connection to close the TOCTOU window
                    locked = await conn.fetchrow(
                        "SELECT status FROM orders WHERE id=$1 AND status NOT IN ('en_camino','en_puerta','entregado','cancelado')",
                        base_id
                    )
                    if not locked:
                        return {"success": False, "error": "in_transit", "blocked_in_transit": True}

                    # sub_number is intentionally a hint only; commit_order_transaction
                    # recomputes it atomically inside the transaction to prevent races.
                    max_sub = await conn.fetchval(
                        "SELECT COALESCE(MAX(sub_number), 1) FROM orders WHERE base_order_id=$1 OR id=$1",
                        base_id
                    )
                    sub_number = max_sub + 1
                    order_id   = f"{base_id}-{sub_number}"

                    order = {
                        "id":             order_id,
                        "phone":          phone,
                        "items":          cart["items"].copy(),
                        "order_type":     order_type,
                        "address":        address or base_order.get("address", ""),
                        "notes":          notes or base_order.get("notes", ""),
                        "subtotal":       subtotal,
                        "delivery_fee":   ZERO,
                        "total":          subtotal,
                        "status":         "pendiente",
                        "paid":           False,
                        "created_at":     datetime.now(ZoneInfo(tz_str)).isoformat(),
                        "bot_number":     bot_number,
                        "payment_method": payment_method or base_order.get("payment_method", ""),
                        "payment_url":    generate_wompi_payment_link(order_id, subtotal),
                        "is_additional":  True,
                        "base_order_id":  base_id,
                        "sub_number":     sub_number,
                    }
                    try:
                        await commit_order_transaction(
                            pool,
                            restaurant_id=restaurant_id or 0,
                            conversation_id=phone,
                            cart=cart,
                            order_payload=order,
                        )
                    except InsufficientStockError as exc:
                        return {"success": False, "error": f"Stock insuficiente para '{exc.sku}'"}
                    except OrderCommitError as exc:
                        log.exception("create_order.commit_failed", error=str(exc), order_id=order_id)
                        return {"success": False, "error": "No pudimos procesar tu pedido, por favor intenta de nuevo."}
                    # Update customer memory after successful order (best-effort, never blocks on failure)
                    try:
                        from app.repositories.customer_profiles_repo import increment_after_order  # noqa: PLC0415
                        from decimal import Decimal
                        item_strs = [f"{i.get('qty', i.get('quantity', 1))}x {i.get('name', 'item')}" for i in cart.get("items", [])][:5]
                        summary = ", ".join(item_strs) if item_strs else "pedido"
                        order_total_decimal = Decimal(str(order["total"]))
                        if restaurant_id:
                            await increment_after_order(
                                restaurant_id=restaurant_id,
                                phone=phone,
                                order_total=order_total_decimal,
                                order_summary=summary,
                            )
                    except Exception:
                        log.exception("customer.profile_increment_failed", phone=phone)
                    return {"success": True, "order": order}

            order_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"
            order = {
                "id":             order_id,
                "phone":          phone,
                "items":          cart["items"].copy(),
                "order_type":     order_type,
                "address":        address or "",
                "notes":          notes,
                "subtotal":       subtotal,
                "delivery_fee":   delivery_fee,
                "total":          total,
                "status":         "pendiente",
                "paid":           False,
                "created_at":     datetime.now(ZoneInfo(tz_str)).isoformat(),
                "bot_number":     bot_number,
                "payment_method": payment_method,
                "payment_url":    generate_wompi_payment_link(order_id, total),
                "is_additional":  False,
                "base_order_id":  None,
                "sub_number":     1,
            }
            try:
                await commit_order_transaction(
                    pool,
                    restaurant_id=restaurant_id or 0,
                    conversation_id=phone,
                    cart=cart,
                    order_payload=order,
                )
            except InsufficientStockError as exc:
                return {"success": False, "error": f"Stock insuficiente para '{exc.sku}'"}
            except OrderCommitError as exc:
                log.exception("create_order.commit_failed", error=str(exc), order_id=order_id)
                return {"success": False, "error": "No pudimos procesar tu pedido, por favor intenta de nuevo."}
            # Update customer memory after successful order (best-effort, never blocks on failure)
            try:
                from app.repositories.customer_profiles_repo import increment_after_order  # noqa: PLC0415
                from decimal import Decimal
                item_strs = [f"{i.get('qty', i.get('quantity', 1))}x {i.get('name', 'item')}" for i in cart.get("items", [])][:5]
                summary = ", ".join(item_strs) if item_strs else "pedido"
                order_total_decimal = Decimal(str(order["total"]))
                if restaurant_id:
                    await increment_after_order(
                        restaurant_id=restaurant_id,
                        phone=phone,
                        order_total=order_total_decimal,
                        order_summary=summary,
                    )
            except Exception:
                log.exception("customer.profile_increment_failed", phone=phone)
            return {"success": True, "order": order}
    except RuntimeError as exc:
        if "cart_lock_contention" in str(exc):
            return {"success": False, "error": "Tu pedido está siendo procesado, por favor espera un momento."}
        raise