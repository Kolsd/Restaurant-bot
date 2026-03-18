"""
agent.py — Arquitectura de UN SOLO CALL (sin loop agéntico)

PROBLEMA ANTERIOR:
  Un mensaje con "2 items + confirma pedido" generaba 4 llamadas a la API:
    Call 1 → add_to_cart(item1)
    Call 2 → add_to_cart(item2)
    Call 3 → create_table_order()
    Call 4 → respuesta final al usuario
  = 4x el costo, aun con prompt caching.
  El loop agéntico es la raíz del problema.

SOLUCIÓN:
  Claude responde en UNA sola llamada con JSON estructurado que incluye:
    - items a agregar al carrito
    - acción a ejecutar (order, reserve, call_waiter, end_session, chat)
    - respuesta final para el usuario
  El backend Python ejecuta todo directamente sin más llamadas a la API.

MODELO:
  Haiku-3 ($0.25/MTok input) en lugar de Sonnet ($3/MTok) = 12x más barato.
  Para el 95% de los mensajes de restaurante (pedir comida, saludar, confirmar)
  Haiku es completamente suficiente.
  Sonnet solo se usa si Haiku falla o el JSON es inválido (fallback).

AHORRO ESTIMADO VS VERSIÓN ANTERIOR:
  - 4 llamadas → 1 llamada:  75% menos llamadas
  - Sonnet → Haiku:          12x más barato por token
  - Combinado:               ~95% reducción de costo
"""

import uuid
import json
import traceback
import asyncio
from anthropic import Anthropic
from app.services import orders, database as db

client = Anthropic()

# ── MODELOS ──────────────────────────────────────────────────────────
MODEL_FAST    = "claude-haiku-4-5-20251001"   # 12x más barato que Sonnet, suficiente para restaurante
MODEL_PRECISE = "claude-sonnet-4-6"            # Solo para fallback si Haiku falla


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


def _build_menu_text(menu: dict, availability: dict) -> str:
    """Menú comprimido: solo nombre + precio, sin descripciones. ~250 tokens."""
    lines = []
    for category, dishes in menu.items():
        av = [d for d in dishes if availability.get(d['name'], True)]
        if not av:
            continue
        lines.append(f"\n{category}:")
        for d in av:
            price = f"${d['price']:,}" if d.get('price') else ""
            lines.append(f"  - {d['name']} {price}")
    return "\n".join(lines) if lines else "Sin menu."


async def build_system_prompt(bot_number: str, table_context: dict | None) -> list:
    """
    System prompt CACHEADO como lista de bloques.
    Bloque 1 [CACHED]: instrucciones estáticas + menú (no cambia entre turnos)
    El carrito y el estado dinámico van en el mensaje del usuario, no aquí.
    """
    availability = await db.db_get_menu_availability()
    menu         = await db.db_get_menu(bot_number) or {}
    menu_text    = _build_menu_text(menu, availability)

    mode = "MODO MESA: El cliente está en el restaurante. Usa action=order para enviar a cocina. No pidas dirección." \
           if table_context else \
           "MODO DELIVERY/RECOGER: Puedes usar action=domicilio o action=recoger."

    static = f"""Eres Mesio, bot de WhatsApp para restaurante. Responde en español, natural y conciso, sin markdown.

{mode}

MENU DISPONIBLE:
{menu_text}

FORMATO DE RESPUESTA — responde SIEMPRE con JSON válido y NADA más:
{{
  "items": [           // lista de platos que el cliente quiere pedir (puede ser vacía [])
    {{"name": "nombre exacto del plato", "qty": 1}}
  ],
  "action": "...",     // UNA de: chat | order | domicilio | recoger | reserve | bill | waiter | end_session
  "address": "",       // solo para action=domicilio
  "notes": "",         // notas para cocina (alergias, términos, etc)
  "separate_bill": false,  // true SOLO si cliente pide cuenta separada explícitamente
  "reservation": {{    // solo para action=reserve
    "name": "", "date": "YYYY-MM-DD", "time": "HH:MM", "guests": 2, "notes": ""
  }},
  "reply": "..."       // respuesta amigable para enviar al cliente por WhatsApp
}}

ACCIONES:
- chat:         respuesta conversacional, sin ejecutar nada (saludos, preguntas, info del menú)
- order:        agregar items al carrito Y enviar a cocina (modo mesa)
- domicilio:    agregar items y crear orden de domicilio con pago
- recoger:      agregar items y crear orden para recoger con pago
- reserve:      hacer una reservación
- bill:         el cliente quiere la cuenta (llama al mesero)
- waiter:       el cliente necesita al mesero por otro motivo
- end_session:  el cliente se despide definitivamente

REGLAS CRÍTICAS:
- Si el cliente pide varios items y confirma en el mismo mensaje → action=order con todos los items
- Si solo menciona items sin confirmar → action=order igual (en mesa siempre se procesa)
- Si solo pregunta o saluda → action=chat, items=[]
- NUNCA uses action=end_session si hay pedido en cocina no entregado
- NUNCA uses action=end_session si hay factura pendiente
- reply debe ser corto, máximo 2 oraciones, natural como WhatsApp"""

    return [
        {
            "type": "text",
            "text": static,
            "cache_control": {"type": "ephemeral"}  # TTL 5 min → ahorro ~80% en llamadas frecuentes
        }
    ]


async def call_claude(system: list, messages: list, model: str = MODEL_FAST) -> str:
    """Una sola llamada a la API. Retorna el texto de la respuesta."""
    response = client.messages.create(
        model=model,
        max_tokens=400,   # JSON de respuesta raramente supera 200 tokens
        system=system,
        messages=messages
    )
    for block in response.content:
        text = _block_attr(block, "text")
        if text:
            return text
    return ""


def _parse_bot_response(raw: str) -> dict | None:
    """Parsea el JSON que devuelve Claude. Tolerante a markdown fences."""
    raw = raw.strip()
    # Limpiar markdown si Claude lo incluyó
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        data = json.loads(raw)
        # Validar campos mínimos
        if "reply" in data:
            return data
    except Exception:
        pass
    return None


async def execute_action(
    parsed:        dict,
    phone:         str,
    bot_number:    str,
    table_context: dict | None,
    session_state: dict,
) -> str:
    """
    Ejecuta la acción determinada por Claude.
    Todo ocurre en Python — cero llamadas adicionales a la API.
    Retorna el mensaje final para el usuario (puede reemplazar parsed['reply']).
    """
    action  = parsed.get("action", "chat")
    items   = parsed.get("items", [])
    reply   = parsed.get("reply", "")

    try:
        # ── Agregar items al carrito ─────────────────────────────────
        # Se hace para order, domicilio y recoger
        if items and action in ("order", "domicilio", "recoger"):
            for item in items:
                name = item.get("name", "")
                qty  = int(item.get("qty", 1))
                if name:
                    res = await orders.add_to_cart(phone, name, qty, bot_number)
                    if res["success"]:
                        print(f"🛒 add_to_cart '{res['dish']['name']}' x{qty}", flush=True)
                    else:
                        print(f"⚠️ No encontrado: '{name}'", flush=True)

        # ── Ejecutar acción principal ────────────────────────────────

        if action == "chat":
            # Solo respuesta conversacional, nada que ejecutar
            pass

        elif action == "order":
            # Mesa: enviar carrito a cocina
            if not table_context:
                return reply  # sin contexto de mesa, no hacer nada
            cart = await db.db_get_cart(phone, bot_number)
            if not cart or not cart.get("items"):
                return reply

            cart_total    = await orders.get_cart_total(phone, bot_number)
            cart_items    = cart["items"]
            extra_notes   = parsed.get("notes", "")
            separate_bill = parsed.get("separate_bill", False)
            items_summary = ", ".join(f"{i['quantity']}x {i['name']}" for i in cart_items)

            active_order = await db.db_get_active_table_order(phone, table_context["id"])

            if separate_bill:
                active_order = None

            if active_order:
                prev_status = active_order.get("status", "recibido")
                await db.db_add_items_to_table_order(active_order["id"], cart_items, cart_total, extra_notes)
                await orders.clear_cart(phone, bot_number)
                await db.db_session_mark_order(phone, bot_number)
                new_total = (active_order.get("total") or 0) + cart_total
                if prev_status in ("en_preparacion", "listo", "entregado"):
                    print(f"➕ Adicional {active_order['id']} ({prev_status}) → {items_summary}", flush=True)
                else:
                    print(f"➕ Orden {active_order['id']} ({prev_status}) → {items_summary}", flush=True)
            else:
                order_id = f"MESA-{uuid.uuid4().hex[:6].upper()}"
                await db.db_save_table_order({
                    "id":         order_id,
                    "table_id":   table_context["id"],
                    "table_name": table_context["name"],
                    "phone":      phone,
                    "items":      cart_items,
                    "notes":      extra_notes,
                    "total":      cart_total,
                    "status":     "recibido"
                })
                await orders.clear_cart(phone, bot_number)
                await db.db_session_mark_order(phone, bot_number)
                print(f"🆕 {order_id}: {items_summary}", flush=True)

        elif action in ("domicilio", "recoger"):
            address = parsed.get("address", "")
            notes   = parsed.get("notes", "")
            if action == "domicilio" and not address:
                return reply  # Claude debería haber pedido la dirección
            res = await orders.create_order(phone, action, address, notes, bot_number)
            if res["success"]:
                order = res["order"]
                await db.db_save_order(order)
                print(f"🆕 Orden {order['id']} {action}: ${order['total']:,}", flush=True)

        elif action == "reserve":
            rv = parsed.get("reservation", {})
            if rv.get("name") and rv.get("date") and rv.get("time"):
                await db.db_add_reservation(
                    rv["name"], rv["date"], rv["time"],
                    int(rv.get("guests", 1)), phone, bot_number, rv.get("notes", "")
                )
                print(f"📅 Reservación: {rv['name']} {rv['date']} {rv['time']}", flush=True)

        elif action in ("bill", "waiter"):
            alert_type = "bill" if action == "bill" else "waiter"
            message    = parsed.get("notes", "El cliente necesita asistencia.")
            table_id   = table_context["id"]   if table_context else ""
            table_name = table_context["name"] if table_context else ""
            await db.db_create_waiter_alert(
                phone=phone, bot_number=bot_number,
                alert_type=alert_type, message=message,
                table_id=table_id, table_name=table_name,
            )
            print(f"🔔 call_waiter {alert_type}", flush=True)

        elif action == "end_session":
            # Guardias de seguridad
            if session_state.get("has_order") and not session_state.get("order_delivered"):
                print(f"⚠️ end_session bloqueado — pedido en cocina {phone}", flush=True)
                return reply  # Claude ya incluyó la respuesta correcta
            if session_state.get("order_delivered"):
                has_pending = await db.db_has_pending_invoice(phone)
                if has_pending:
                    print(f"⚠️ end_session bloqueado — factura pendiente {phone}", flush=True)
                    return reply
            await db.db_close_session(phone=phone, bot_number=bot_number, reason="client_goodbye", closed_by_username="")
            pool = await db.get_pool()
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM conversations WHERE phone=$1 AND bot_number=$2", phone, bot_number)
            print(f"👋 Sesión cerrada: {phone}", flush=True)

    except Exception as e:
        print(f"❌ execute_action({action}): {traceback.format_exc()}", flush=True)

    return reply


# Cuántos pares user/assistant guardar en el historial enviado al modelo
HISTORY_WINDOW = 5  # 5 turnos = 10 mensajes = suficiente para restaurante


async def chat(user_phone: str, user_message: str, bot_number: str, meta_phone_id: str = "") -> dict:
    table_context = await detect_table_context(user_message, user_phone, bot_number)
    session_state = await get_session_state(user_phone, bot_number)

    if meta_phone_id and table_context:
        await db.db_touch_session_with_phone_id(user_phone, bot_number, meta_phone_id)

    # ── Cargar historial ─────────────────────────────────────────────
    full_history = await db.db_get_history(user_phone, bot_number)

    # Estado dinámico en el mensaje del usuario (no en el system, para que el cache sea estable)
    cart_text = await orders.cart_summary(user_phone, bot_number)

    # Contexto de sesión para el modelo
    session_note = ""
    if session_state.get("has_order") and not session_state.get("order_delivered"):
        session_note = "\n[ESTADO: El cliente tiene un pedido en cocina que no ha sido entregado. NO uses end_session.]"
    elif session_state.get("order_delivered"):
        session_note = "\n[ESTADO: El pedido fue entregado. La factura no ha llegado aún. NO uses end_session hasta que el mesero traiga la factura.]"

    table_note = f"\n[MESA ACTIVA: {table_context['name']}]" if table_context else ""

    # Enriquecer el mensaje del usuario con contexto dinámico
    user_message_with_context = f"{user_message}\n\n[CARRITO ACTUAL: {cart_text}]{table_note}{session_note}"

    # Construir mensajes: historial recortado + mensaje actual
    messages = full_history[-(HISTORY_WINDOW * 2):]
    messages.append({"role": "user", "content": user_message_with_context})

    # ── System prompt cacheado ───────────────────────────────────────
    sys_prompt = await build_system_prompt(bot_number, table_context)

    # ── UNA SOLA LLAMADA a la API (Haiku) ────────────────────────────
    raw = await call_claude(sys_prompt, messages, model=MODEL_FAST)

    parsed = _parse_bot_response(raw)

    # Fallback a Sonnet si Haiku devolvió JSON inválido
    if parsed is None:
        print(f"⚠️ Haiku JSON inválido, fallback a Sonnet. Raw: {raw[:200]}", flush=True)
        raw    = await call_claude(sys_prompt, messages, model=MODEL_PRECISE)
        parsed = _parse_bot_response(raw)

    # Si sigue sin parsear, respuesta genérica
    if parsed is None:
        print(f"❌ JSON inválido incluso con Sonnet: {raw[:200]}", flush=True)
        assistant_message = "Lo siento, hubo un problema. ¿Puedes repetir tu pedido?"
    else:
        # Ejecutar la acción en Python (sin más llamadas a la API)
        assistant_message = await execute_action(parsed, user_phone, bot_number, table_context, session_state)

    # ── Guardar historial limpio ─────────────────────────────────────
    # Guardar el mensaje original del usuario (sin el contexto inyectado)
    full_history.append({"role": "user",      "content": user_message})
    full_history.append({"role": "assistant", "content": assistant_message})

    # Limitar historial guardado
    history_to_save = full_history[-(HISTORY_WINDOW * 2 + 2):]
    await db.db_save_history(user_phone, bot_number, history_to_save)

    return {"message": assistant_message}


async def reset_conversation(user_phone: str):
    await db.db_delete_conversation(user_phone)