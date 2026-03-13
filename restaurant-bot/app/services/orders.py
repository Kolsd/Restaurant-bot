import hashlib
import hmac
import uuid
import os
from datetime import datetime
from app.data.restaurant import MENU

# ─────────────────────────────────────────────
# ALMACENAMIENTO EN MEMORIA
# En producción: reemplazar por PostgreSQL/Supabase
# ─────────────────────────────────────────────

orders: dict = {}          # order_id → order
pending_carts: dict = {}   # user_phone → cart en construcción


# ─────────────────────────────────────────────
# HELPERS DE MENÚ
# ─────────────────────────────────────────────

def find_dish(name: str) -> dict | None:
    """Busca un plato en el menú por nombre (búsqueda flexible)."""
    name_lower = name.lower().strip()
    for category, dishes in MENU.items():
        for dish in dishes:
            if name_lower in dish["name"].lower() or dish["name"].lower() in name_lower:
                return {**dish, "category": category}
    return None


def get_all_dishes_flat() -> list:
    all_dishes = []
    for category, dishes in MENU.items():
        for dish in dishes:
            all_dishes.append({**dish, "category": category})
    return all_dishes


# ─────────────────────────────────────────────
# CARRITO DE COMPRAS
# ─────────────────────────────────────────────

def get_cart(user_phone: str) -> dict:
    if user_phone not in pending_carts:
        pending_carts[user_phone] = {
            "items": [],
            "order_type": None,    # "domicilio" | "recoger"
            "address": None,
            "notes": ""
        }
    return pending_carts[user_phone]


def add_to_cart(user_phone: str, dish_name: str, quantity: int = 1) -> dict:
    cart = get_cart(user_phone)
    dish = find_dish(dish_name)

    if not dish:
        return {"success": False, "error": f"No encontré '{dish_name}' en el menú"}

    # Si ya está en el carrito, aumentar cantidad
    for item in cart["items"]:
        if item["name"] == dish["name"]:
            item["quantity"] += quantity
            item["subtotal"] = item["price"] * item["quantity"]
            return {"success": True, "cart": cart, "dish": dish}

    cart["items"].append({
        "name": dish["name"],
        "price": dish["price"],
        "quantity": quantity,
        "subtotal": dish["price"] * quantity,
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
    if user_phone in pending_carts:
        del pending_carts[user_phone]


def get_cart_total(user_phone: str) -> int:
    cart = get_cart(user_phone)
    return sum(item["subtotal"] for item in cart["items"])


def cart_summary(user_phone: str) -> str:
    cart = get_cart(user_phone)
    if not cart["items"]:
        return "Tu carrito está vacío."

    lines = []
    for item in cart["items"]:
        lines.append(f"• {item['quantity']}x {item['name']} — ${item['subtotal']:,}")

    total = get_cart_total(user_phone)
    tipo = cart.get("order_type", "")
    domicilio_extra = " (+ domicilio)" if tipo == "domicilio" else ""
    lines.append(f"\n*Total: ${total:,} COP{domicilio_extra}*")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# WOMPI — GENERACIÓN DE LINK DE PAGO
# ─────────────────────────────────────────────

WOMPI_PUBLIC_KEY = os.getenv("WOMPI_PUBLIC_KEY", "pub_test_TU_LLAVE_PUBLICA")
WOMPI_PRIVATE_KEY = os.getenv("WOMPI_PRIVATE_KEY", "prv_test_TU_LLAVE_PRIVADA")
WOMPI_INTEGRITY_SECRET = os.getenv("WOMPI_INTEGRITY_SECRET", "TU_SECRETO_INTEGRIDAD")
DELIVERY_FEE = 5000  # $5,000 COP de domicilio


def generate_wompi_payment_link(order_id: str, amount_cop: int, description: str) -> str:
    """
    Genera un link de pago de Wompi.
    Wompi Widget URL — el cliente paga directo en su navegador.
    """
    # En producción usar el Widget de Wompi con firma de integridad
    # https://docs.wompi.co/docs/en/widget-checkout-web
    
    amount_cents = amount_cop * 100  # Wompi usa centavos
    
    # Generar firma de integridad
    signature_string = f"{order_id}{amount_cents}COP{WOMPI_INTEGRITY_SECRET}"
    signature = hashlib.sha256(signature_string.encode()).hexdigest()
    
    payment_url = (
        f"https://checkout.wompi.co/p/?"
        f"public-key={WOMPI_PUBLIC_KEY}"
        f"&currency=COP"
        f"&amount-in-cents={amount_cents}"
        f"&reference={order_id}"
        f"&signature:integrity={signature}"
        f"&redirect-url=https://restaurant-bot-production-594b.up.railway.app/api/payment/confirm"
    )
    return payment_url


# ─────────────────────────────────────────────
# CREAR ORDEN
# ─────────────────────────────────────────────

def create_order(user_phone: str, order_type: str, address: str = None, notes: str = "") -> dict:
    """
    Crea una orden a partir del carrito activo del usuario.
    Retorna la orden con el link de pago de Wompi.
    """
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
        "id": order_id,
        "phone": user_phone,
        "items": cart["items"].copy(),
        "order_type": order_type,
        "address": address,
        "notes": notes,
        "subtotal": subtotal,
        "delivery_fee": delivery_fee,
        "total": total,
        "status": "pendiente_pago",
        "created_at": datetime.now().isoformat(),
        "payment_url": None,
        "paid": False
    }

    # Generar link de pago Wompi
    description = f"Pedido {order_id} - {'Domicilio' if order_type == 'domicilio' else 'Para recoger'}"
    payment_url = generate_wompi_payment_link(order_id, total, description)
    order["payment_url"] = payment_url

    orders[order_id] = order
    clear_cart(user_phone)

    return {"success": True, "order": order}


def confirm_payment(order_id: str, wompi_transaction_id: str) -> dict:
    """Confirma el pago de una orden (llamado por webhook de Wompi)."""
    if order_id not in orders:
        return {"success": False, "error": "Orden no encontrada"}

    orders[order_id]["status"] = "confirmado"
    orders[order_id]["paid"] = True
    orders[order_id]["transaction_id"] = wompi_transaction_id
    orders[order_id]["paid_at"] = datetime.now().isoformat()

    return {"success": True, "order": orders[order_id]}


def get_order(order_id: str) -> dict | None:
    return orders.get(order_id)


def get_orders_by_phone(phone: str) -> list:
    return [o for o in orders.values() if o["phone"] == phone]


def get_all_orders() -> list:
    return list(orders.values())
