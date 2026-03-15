import hashlib
import uuid
import os
from datetime import datetime
from app.data.restaurant import MENU
from app.services import database as db

DELIVERY_FEE = 5000
WOMPI_PUBLIC_KEY = os.getenv("WOMPI_PUBLIC_KEY", "pub_test_TU_LLAVE_PUBLICA")
WOMPI_INTEGRITY_SECRET = os.getenv("WOMPI_INTEGRITY_SECRET", "TU_SECRETO")

# Carrito en memoria — se limpia al crear la orden (es temporal, no necesita persistir)
pending_carts: dict = {}


def find_dish(name: str) -> dict | None:
    name_lower = name.lower().strip()
    for category, dishes in MENU.items():
        for dish in dishes:
            if name_lower in dish["name"].lower() or dish["name"].lower() in name_lower:
                return {**dish, "category": category}
    return None


def get_cart(user_phone: str) -> dict:
    if user_phone not in pending_carts:
        pending_carts[user_phone] = {"items": [], "order_type": None, "address": None, "notes": ""}
    return pending_carts[user_phone]


def add_to_cart(user_phone: str, dish_name: str, quantity: int = 1) -> dict:
    cart = get_cart(user_phone)
    dish = find_dish(dish_name)
    if not dish:
        return {"success": False, "error": f"No encontré '{dish_name}' en el menú"}
    for item in cart["items"]:
        if item["name"] == dish["name"]:
            item["quantity"] += quantity
            item["subtotal"] = item["price"] * item["quantity"]
            return {"success": True, "cart": cart, "dish": dish}
    cart["items"].append({
        "name": dish["name"], "price": dish["price"],
        "quantity": quantity, "subtotal": dish["price"] * quantity,
        "category": dish["category"]
    })
    return {"success": True, "cart": cart, "dish": dish}


def remove_from_cart(user_phone: str, dish_name: str) -> dict:
    cart = get_cart(user_phone)
    dish = find_dish(dish_name)
    if not dish:
        return {"success": False, "error": "Plato no encontrado"}
    cart["items"] = [i for i in cart["items"] if i["name"] != dish["name"]]
    return {"success": True, "cart": cart}


def clear_cart(user_phone: str):
    pending_carts.pop(user_phone, None)


def get_cart_total(user_phone: str) -> int:
    return sum(item["subtotal"] for item in get_cart(user_phone)["items"])


def cart_summary(user_phone: str) -> str:
    cart = get_cart(user_phone)
    if not cart["items"]:
        return "Tu carrito está vacío."
    lines = [f"• {i['quantity']}x {i['name']} — ${i['subtotal']:,}" for i in cart["items"]]
    total = get_cart_total(user_phone)
    tipo = cart.get("order_type", "")
    extra = " (+ $5,000 domicilio)" if tipo == "domicilio" else ""
    lines.append(f"\n*Total: ${total:,} COP{extra}*")
    return "\n".join(lines)


def generate_wompi_payment_link(order_id: str, amount_cop: int) -> str:
    amount_cents = amount_cop * 100
    signature_string = f"{order_id}{amount_cents}COP{WOMPI_INTEGRITY_SECRET}"
    signature = hashlib.sha256(signature_string.encode()).hexdigest()
    return (
        f"https://checkout.wompi.co/p/?"
        f"public-key={WOMPI_PUBLIC_KEY}"
        f"&currency=COP&amount-in-cents={amount_cents}"
        f"&reference={order_id}&signature:integrity={signature}"
        f"&redirect-url=https://restaurant-bot-production-594b.up.railway.app/api/payment/confirm"
    )


def create_order(user_phone: str, order_type: str, address: str = None, notes: str = "") -> dict:
    cart = get_cart(user_phone)
    if not cart["items"]:
        return {"success": False, "error": "El carrito está vacío"}
    if order_type == "domicilio" and not address:
        return {"success": False, "error": "Se necesita dirección de entrega"}

    subtotal = get_cart_total(user_phone)
    delivery_fee = DELIVERY_FEE if order_type == "domicilio" else 0
    total = subtotal + delivery_fee
    order_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"

    order = {
        "id": order_id, "phone": user_phone,
        "items": cart["items"].copy(),
        "order_type": order_type, "address": address or "",
        "notes": notes, "subtotal": subtotal,
        "delivery_fee": delivery_fee, "total": total,
        "status": "pendiente_pago", "paid": False,
        "created_at": datetime.now().isoformat(),
        "payment_url": generate_wompi_payment_link(order_id, total)
    }
    clear_cart(user_phone)
    return {"success": True, "order": order}
