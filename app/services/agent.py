import uuid
import json
import traceback
from anthropic import Anthropic
from app.services import orders, database as db

client = Anthropic()

MODEL_FAST    = "claude-haiku-4-5-20251001"
MODEL_PRECISE = "claude-sonnet-4-6"

def _block_attr(block, attr: str):
    if isinstance(block, dict):
        return block.get(attr)
    return getattr(block, attr, None)

async def detect_table_context(message: str, phone: str, bot_number: str) -> dict | None:
    session = await db.db_get_active_session(phone, bot_number)
    if session and session.get("table_id"):
        table = await db.db_get_table_by_id(session["table_id"])
        if table:
            await db.db_touch_session(phone, bot_number)
            return table

    import re as _re
    branch_id    = None
    branch_match = _re.search(r'\[branch=(\d+)\]', message)
    if branch_match:
        branch_id = branch_match.group(1)

    m = _re.search(r'Mesa\s+(\d+)', message, _re.IGNORECASE)
    if not m:
        m = _re.search(r'(?:estoy en|mesa|table)[\s-]*(\d+)', message, _re.IGNORECASE)

    if m:
        number = m.group(1)
        if branch_id:
            table_id = f"b{branch_id}-mesa-{number}"
            table    = await db.db_get_table_by_id(table_id)
            if table:
                await db.db_create_table_session(phone, bot_number, table["id"], table["name"])
                return table
        table_id = f"mesa-{number}"
        table    = await db.db_get_table_by_id(table_id)
        if table:
            await db.db_create_table_session(phone, bot_number, table["id"], table["name"])
            return table
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM restaurant_tables WHERE number=$1 AND active=TRUE LIMIT 1",
                int(number)
            )
        if row:
            table = db._serialize(dict(row))
            await db.db_create_table_session(phone, bot_number, table["id"], table["name"])
            return table
    return None

async def get_session_state(phone: str, bot_number: str) -> dict:
    session = await db.db_get_active_session(phone, bot_number)
    if not session:
        return {"has_order": False, "order_delivered": False, "active": False}
    return {
        "active":          True,
        "has_order":       session.get("has_order", False),
        "order_delivered": session.get("order_delivered", False),
    }

def _build_compact_menu(menu: dict, availability: dict) -> str:
    lines = []
    for category, dishes in menu.items():
        cat_lines = []
        for d in dishes:
            name  = d.get("name", "")
            price = d.get("price", 0)
            avail = availability.get(name, True)
            price_str = f"${price:,}" if price else ""
            status    = "" if avail else " [NO DISPONIBLE]"
            cat_lines.append(f"{name} {price_str}{status}")
        if cat_lines:
            lines.append(f"{category}: {', '.join(cat_lines)}")
    return "\n".join(lines) if lines else "Sin menú."

_STATIC_SYSTEM = """Eres Mesio, asistente de restaurante por WhatsApp. Español, natural, muy conciso.

El cliente ya vio el menú con fotos y descripciones antes de escribir.
Usa el [MENÚ] del contexto para validar pedidos, sugerir upsell y conocer precios.

RESPONDE SIEMPRE con JSON válido, nada más:
{
  "items": [{"name": "nombre exacto del plato", "qty": 1}],
  "action": "chat|order|domicilio|recoger|reserve|bill|waiter|end_session",
  "address": "",
  "notes": "",
  "separate_bill": false,
  "reservation": {"name":"","date":"YYYY-MM-DD","time":"HH:MM","guests":2,"notes":""},
  "reply": "respuesta para el cliente (máx 2 oraciones)"
}

ACCIONES:
- chat:        respuesta sin ejecutar
- order:       agregar items y enviar a cocina (modo mesa)
- domicilio:   agregar items y crear orden delivery con pago
- recoger:     agregar items y crear orden para recoger con pago
- reserve:     crear reservación
- bill:        cliente pide la cuenta
- waiter:      cliente necesita al mesero
- end_session: cliente se despide definitivamente

REGLAS DE VALIDACIÓN (usa el [MENÚ] del contexto):
- Si el plato pedido NO está en [MENÚ] → action=chat, sugiere alternativas
- Si el plato tiene [NO DISPONIBLE] → action=chat, disculpa y sugiere alternativas
- Usa los nombres EXACTOS del menú en "items.name"
- CRÍTICO PARA ÓRDENES ADICIONALES: En "items", incluye **SOLO LOS NUEVOS PLATOS** que el cliente acaba de pedir en su último mensaje. ¡NUNCA repitas los platos que ya están en el [CARRITO] o que ya se pidieron antes!

REGLAS DE FLUJO:
- Items + confirmación en mismo mensaje → action=order
- Solo items sin confirmación explícita en mesa → action=order (proceso directo)
- Saludo o pregunta → action=chat, items=[]
- NO uses end_session si hay pedido en cocina no entregado
- NO uses end_session si hay factura pendiente"""

async def build_system_prompt() -> list:
    return [{"type": "text", "text": _STATIC_SYSTEM, "cache_control": {"type": "ephemeral"}}]

async def call_claude(system: list, messages: list, model: str = MODEL_FAST) -> str:
    response = client.messages.create(
        model=model, max_tokens=350, system=system, messages=messages
    )
    for block in response.content:
        text = _block_attr(block, "text")
        if text:
            return text
    return ""

def _parse_bot_response(raw: str) -> dict | None:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        data = json.loads(raw)
        if "reply" in data:
            return data
    except Exception:
        pass
    return None

async def execute_action(parsed: dict, phone: str, bot_number: str,
                         table_context: dict | None, session_state: dict) -> str:
    action = parsed.get("action", "chat")
    items  = parsed.get("items", [])
    reply  = parsed.get("reply", "")

    try:
        cart_errors = []
        if items and action in ("order", "domicilio", "recoger"):
            for item in items:
                name = item.get("name", "")
                qty  = int(item.get("qty", 1))
                if not name:
                    continue
                res = await orders.add_to_cart(phone, name, qty, bot_number)
                if res["success"]:
                    print(f"🛒 '{res['dish']['name']}' x{qty}", flush=True)
                else:
                    cart_errors.append(name)
                    print(f"⚠️ No encontrado en menú: '{name}'", flush=True)

            if cart_errors and len(cart_errors) == len([i for i in items if i.get("name")]):
                names = ", ".join(cart_errors)
                return f"No encontré '{names}' en el menú. ¿Puedes verificar el nombre?"

        if action == "chat":
            pass

        elif action == "order":
            if not table_context:
                return reply
            cart = await db.db_get_cart(phone, bot_number)
            if not cart or not cart.get("items"):
                if cart_errors:
                    return reply
                return reply

            cart_total    = await orders.get_cart_total(phone, bot_number)
            cart_items    = cart["items"]
            extra_notes   = parsed.get("notes", "")
            separate_bill = parsed.get("separate_bill", False)
            items_summary = ", ".join(f"{i['quantity']}x {i['name']}" for i in cart_items)

            base_order_id = await db.db_get_base_order_id(phone, table_context["id"])

            if separate_bill or base_order_id is None:
                order_id      = f"MESA-{uuid.uuid4().hex[:6].upper()}"
                base_order_id = order_id
                sub_number    = 1
            else:
                sub_number = await db.db_get_next_sub_number(base_order_id)
                order_id   = f"{base_order_id}-{sub_number}"

            await db.db_save_table_order({
                "id":            order_id,
                "table_id":      table_context["id"],
                "table_name":    table_context["name"],
                "phone":         phone,
                "items":         cart_items,
                "notes":         extra_notes,
                "total":         cart_total,
                "status":        "recibido",
                "base_order_id": base_order_id if sub_number > 1 else None,
                "sub_number":    sub_number,
            })
            await orders.clear_cart(phone, bot_number)
            await db.db_session_mark_order(phone, bot_number)
            tag = f"adicional #{sub_number}" if sub_number > 1 else "orden inicial"
            print(f"🆕 {order_id} ({tag}): {items_summary}", flush=True)

            if cart_errors:
                failed = ", ".join(cart_errors)
                reply += f" (No pude agregar: {failed} — no está en el menú)"

        elif action in ("domicilio", "recoger"):
            address = parsed.get("address", "")
            notes   = parsed.get("notes", "")
            if action == "domicilio" and not address:
                return reply
            res = await orders.create_order(phone, action, address, notes, bot_number)
            if res["success"]:
                order = res["order"]
                await db.db_save_order(order)
                print(f"🆕 {order['id']} {action}", flush=True)
            if cart_errors:
                failed = ", ".join(cart_errors)
                reply += f" (No pude agregar: {failed})"

        elif action == "reserve":
            rv = parsed.get("reservation", {})
            if rv.get("name") and rv.get("date") and rv.get("time"):
                await db.db_add_reservation(
                    rv["name"], rv["date"], rv["time"],
                    int(rv.get("guests", 1)), phone, bot_number, rv.get("notes", "")
                )
                print(f"📅 Reservación {rv['name']} {rv['date']}", flush=True)

        elif action in ("bill", "waiter"):
            alert_type = "bill" if action == "bill" else "waiter"
            message    = parsed.get("notes", "Asistencia requerida.")
            table_id   = table_context["id"]   if table_context else ""
            table_name = table_context["name"] if table_context else ""
            await db.db_create_waiter_alert(
                phone=phone, bot_number=bot_number, alert_type=alert_type,
                message=message, table_id=table_id, table_name=table_name,
            )
            print(f"🔔 {alert_type} — {table_name}", flush=True)

        elif action == "end_session":
            if session_state.get("has_order") and not session_state.get("order_delivered"):
                print(f"⚠️ end_session bloqueado — pedido en cocina {phone}", flush=True)
                return reply
            if session_state.get("order_delivered"):
                if await db.db_has_pending_invoice(phone):
                    print(f"⚠️ end_session bloqueado — factura pendiente {phone}", flush=True)
                    return reply
            await db.db_close_session(phone=phone, bot_number=bot_number,
                                      reason="client_goodbye", closed_by_username="")
            pool = await db.get_pool()
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM conversations WHERE phone=$1 AND bot_number=$2",
                                   phone, bot_number)
            print(f"👋 Sesión cerrada: {phone}", flush=True)

    except Exception:
        print(f"❌ execute_action({action}): {traceback.format_exc()}", flush=True)

    return reply

HISTORY_WINDOW = 5

async def chat(user_phone: str, user_message: str, bot_number: str, meta_phone_id: str = "") -> dict:
    table_context = await detect_table_context(user_message, user_phone, bot_number)
    session_state = await get_session_state(user_phone, bot_number)

    if meta_phone_id and table_context:
        await db.db_touch_session_with_phone_id(user_phone, bot_number, meta_phone_id)

    full_history = await db.db_get_history(user_phone, bot_number)
    cart_text    = await orders.cart_summary(user_phone, bot_number)

    availability = await db.db_get_menu_availability()
    menu         = await db.db_get_menu(bot_number) or {}
    compact_menu = _build_compact_menu(menu, availability)

    table_note   = f"\n[MESA: {table_context['name']}]" if table_context else ""
    session_note = ""
    if session_state.get("has_order") and not session_state.get("order_delivered"):
        session_note = "\n[Pedido en cocina no entregado. NO uses end_session.]"
    elif session_state.get("order_delivered"):
        session_note = "\n[Pedido entregado, factura pendiente. NO uses end_session.]"

    enriched = (
        f"{user_message}"
        f"\n[MENÚ:\n{compact_menu}]"
        f"\n[CARRITO: {cart_text}]"
        f"{table_note}{session_note}"
    )

    messages = full_history[-(HISTORY_WINDOW * 2):]
    messages.append({"role": "user", "content": enriched})

    sys_prompt = await build_system_prompt()

    raw    = await call_claude(sys_prompt, messages, model=MODEL_FAST)
    parsed = _parse_bot_response(raw)

    if parsed is None:
        print(f"⚠️ Haiku JSON inválido, fallback Sonnet. Raw: {raw[:120]}", flush=True)
        raw    = await call_claude(sys_prompt, messages, model=MODEL_PRECISE)
        parsed = _parse_bot_response(raw)

    if parsed is None:
        print(f"❌ JSON inválido con Sonnet: {raw[:120]}", flush=True)
        assistant_message = "Lo siento, hubo un problema. ¿Puedes repetir tu pedido?"
    else:
        assistant_message = await execute_action(parsed, user_phone, bot_number, table_context, session_state)

    full_history.append({"role": "user",      "content": user_message})
    full_history.append({"role": "assistant", "content": assistant_message})
    await db.db_save_history(user_phone, bot_number, full_history[-(HISTORY_WINDOW * 2 + 2):])

    return {"message": assistant_message}

async def reset_conversation(user_phone: str):
    await db.db_delete_conversation(user_phone)