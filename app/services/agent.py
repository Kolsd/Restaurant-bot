import uuid
import json
import traceback
import asyncio
from anthropic import Anthropic
from app.services import orders, database as db

client = Anthropic()

TOOLS = [
    {
        "name": "add_to_cart",
        "description": (
            "Agrega un plato al carrito del cliente. "
            "Usalo SIEMPRE que el cliente mencione querer pedir algo del menu. "
            "Llama esta herramienta una vez por cada plato diferente que pida el cliente."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dish_name": {"type": "string", "description": "Nombre del plato tal como aparece en el menu"},
                "quantity":  {"type": "integer", "description": "Cantidad a ordenar (minimo 1)"}
            },
            "required": ["dish_name", "quantity"]
        }
    },
    {
        "name": "create_order",
        "description": (
            "Crea la orden de domicilio o para recoger y genera el link de pago Wompi. "
            "Usalo SOLO cuando el cliente confirme que quiere procesar su pedido de domicilio/recoger. "
            "NO usar en modo mesa (dine-in)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_type": {"type": "string", "enum": ["domicilio", "recoger"], "description": "Tipo de pedido"},
                "address":    {"type": "string", "description": "Direccion completa de entrega. Dejar vacio si es para recoger."},
                "notes":      {"type": "string", "description": "Notas adicionales del pedido"}
            },
            "required": ["order_type"]
        }
    },
    {
        "name": "create_table_order",
        "description": (
            "Envia el pedido a cocina para clientes en mesa (dine-in). "
            "Usalo SIEMPRE cuando el cliente en mesa confirme su pedido. "
            "Los items se leen automaticamente del carrito. "
            "NO usar para domicilio ni recoger. "
            "Si el cliente ya tiene un pedido activo en cocina, los nuevos items se agregan a ese mismo pedido (misma cuenta). "
            "SOLO usa separate_bill=true si el cliente pide explicitamente cuenta separada, cuenta aparte, o cobrar por separado."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "notes":         {"type": "string",  "description": "Notas para cocina: alergias, termino de coccion, etc (opcional)"},
                "separate_bill": {"type": "boolean", "description": "true SOLO si el cliente pide explicitamente cuenta separada o cobrar aparte. Por defecto false."}
            },
            "required": []
        }
    },
    {
        "name": "create_reservation",
        "description": "Guarda una reservacion en el restaurante.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name":   {"type": "string",  "description": "Nombre completo del cliente"},
                "date":   {"type": "string",  "description": "Fecha en formato YYYY-MM-DD"},
                "time":   {"type": "string",  "description": "Hora en formato HH:MM (24h)"},
                "guests": {"type": "integer", "description": "Numero de personas"},
                "notes":  {"type": "string",  "description": "Notas especiales (opcional)"}
            },
            "required": ["name", "date", "time", "guests"]
        }
    },
    {
        "name": "call_waiter",
        "description": (
            "Llama al mesero fisicamente o solicita la cuenta. "
            "Usalo SIEMPRE que el cliente diga: 'la cuenta', 'me cobra', 'quiero pagar', "
            "'llama al mesero', 'necesito al mesero', 'necesito ayuda', 'me traes algo', "
            "'pueden venir a mi mesa', o cualquier solicitud que requiera presencia fisica."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "alert_type": {
                    "type": "string", "enum": ["bill", "waiter"],
                    "description": "'bill' cuando el cliente pide la cuenta, 'waiter' para cualquier otra asistencia"
                },
                "message": {"type": "string", "description": "Descripcion breve de lo que necesita el cliente"}
            },
            "required": ["alert_type", "message"]
        }
    },
    {
        "name": "end_session",
        "description": (
            "Finaliza la sesion del cliente y limpia el historial. "
            "SOLO usar cuando el cliente claramente se despide y YA NO HAY NADA PENDIENTE. "
            "Ejemplos VALIDOS: 'hasta luego', 'ya me voy', 'nos vemos', 'chao', 'bye', "
            "'hasta pronto', 'que tengas buen dia', 'gracias por todo', 'muchas gracias fue todo'. "
            "NUNCA usar si el cliente tiene un pedido en cocina que aun no ha sido entregado. "
            "NUNCA usar si el cliente recibio su comida pero aun no le han entregado la factura. "
            "NUNCA usar si el cliente solo dice 'eso es todo' para terminar de pedir — "
            "eso significa que no quiere agregar mas platos, NO que se va a retirar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "farewell_message": {"type": "string", "description": "Mensaje de despedida calido (opcional)"}
            },
            "required": []
        }
    }
]


def _block_attr(block, attr: str):
    if isinstance(block, dict):
        return block.get(attr)
    return getattr(block, attr, None)


def _serialize_content(content_blocks) -> list:
    result = []
    for block in content_blocks:
        if isinstance(block, dict):
            result.append(block)
        elif hasattr(block, "model_dump"):
            result.append(block.model_dump())
        elif hasattr(block, "__dict__"):
            result.append(dict(block.__dict__))
        else:
            try:
                result.append(json.loads(json.dumps(block, default=str)))
            except Exception:
                pass
    return result


async def detect_table_context(message: str, phone: str, bot_number: str) -> dict | None:
    # Fuente de verdad #1: sesión activa en DB
    session = await db.db_get_active_session(phone, bot_number)
    if session and session.get("table_id"):
        table = await db.db_get_table_by_id(session["table_id"])
        if table:
            await db.db_touch_session(phone, bot_number)
            return table

    # Fuente de verdad #2: detectar del mensaje
    import re as _re

    branch_id = None
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
            table = await db.db_get_table_by_id(table_id)
            if table:
                await db.db_create_table_session(phone, bot_number, table["id"], table["name"])
                return table
        table_id = f"mesa-{number}"
        table = await db.db_get_table_by_id(table_id)
        if table:
            await db.db_create_table_session(phone, bot_number, table["id"], table["name"])
            return table
        # Fallback: buscar por número
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


async def build_system_prompt(phone: str, bot_number: str, table_context: dict | None) -> str:
    availability  = await db.db_get_menu_availability()
    menu          = await db.db_get_menu(bot_number) or {}
    cart_text     = await orders.cart_summary(phone, bot_number)
    session_state = await get_session_state(phone, bot_number)

    # ── Menú SIN formato markdown (sin asteriscos) ──────────────────
    menu_lines = []
    for category, dishes in menu.items():
        av_dishes = [d for d in dishes if availability.get(d['name'], True)]
        if not av_dishes:
            continue
        menu_lines.append(f"\n{category}:")
        for dish in av_dishes:
            price = f"${dish['price']:,}" if dish.get('price') else ""
            desc  = dish.get('description', '')
            menu_lines.append(f"  - {dish['name']} {price}  {desc}")
    menu_text = "\n".join(menu_lines) if menu_lines else "Sin menu configurado."

    # ── Contexto de mesa ────────────────────────────────────────────
    table_section = ""
    session_section = ""

    if table_context:
        has_order       = session_state.get("has_order", False)
        order_delivered = session_state.get("order_delivered", False)

        if has_order and not order_delivered:
            session_section = (
                "\nESTADO: El cliente tiene un pedido en cocina que AUN NO ha sido entregado. "
                "Si dice 'eso es todo', 'gracias', 'listo' → NO cierres la sesion. "
                "Solo di que su pedido esta en camino. "
                "Solo usa end_session cuando se despida definitivamente despues de recibir todo Y de que le hayan entregado la factura."
            )
        elif has_order and order_delivered:
            session_section = (
                "\nESTADO: El pedido ya fue entregado. El mesero todavia NO ha traido la factura. "
                "Si el cliente se despide → dile amablemente que espere la factura antes de irse, o que llame al mesero si la necesita. "
                "NO uses end_session hasta que la factura haya sido entregada."
            )

        table_section = f"""
MODO MESA (dine-in): El cliente esta en {table_context['name']}.
- No pidas direccion de entrega.
- Para enviar a cocina usa create_table_order (nunca create_order).
- Si pide la cuenta o quiere pagar: usa call_waiter con alert_type="bill" de inmediato.
- Si necesita al mesero: usa call_waiter con alert_type="waiter".{session_section}
"""

    return f"""Eres Mesio, asistente de un restaurante. Responde de forma natural, amigable y concisa, como un buen mesero virtual. No uses asteriscos, no uses formato markdown, no pongas categorias del menu en negrita. Escribe como si fuera una conversacion de WhatsApp normal.

Cuando saludes al cliente que llega a una mesa, di simplemente "Hola, bienvenido. ¿Que se te antoja hoy?" — no repitas el nombre de la mesa en el saludo.
{table_section}
MENU DISPONIBLE:
{menu_text}

CARRITO ACTUAL:
{cart_text}

REGLAS:
- add_to_cart: cuando el cliente pida un plato (una llamada por plato).
- create_table_order: cuando confirme su pedido en mesa.
- create_order: para domicilio o recoger.
- call_waiter: cuando pida la cuenta o necesite al mesero.
- end_session: solo cuando se despida claramente y no haya pedidos pendientes.
"""


async def execute_tool(
    tool_name: str,
    tool_input: dict,
    phone: str,
    bot_number: str,
    table_context: dict | None
) -> str:
    try:
        if tool_name == "add_to_cart":
            dish_name = tool_input.get("dish_name", "")
            quantity  = int(tool_input.get("quantity", 1))
            if not dish_name:
                return "Error: Se necesita el nombre del plato."
            res = await orders.add_to_cart(phone, dish_name, quantity, bot_number)
            if res["success"]:
                return f"OK: '{res['dish']['name']}' x{quantity} agregado al carrito."
            return f"Error al agregar '{dish_name}': {res.get('error', 'Plato no encontrado.')}"

        elif tool_name == "create_order":
            order_type = tool_input.get("order_type", "recoger")
            address    = tool_input.get("address", "")
            notes      = tool_input.get("notes", "")
            if order_type == "domicilio" and not address:
                return "Error: Se necesita la dirección de entrega."
            res = await orders.create_order(phone, order_type, address, notes, bot_number)
            if res["success"]:
                order = res["order"]
                await db.db_save_order(order)
                return f"OK: Orden {order['id']} creada. Total: ${order['total']:,} COP. Link de pago: {order['payment_url']}"
            return f"Error al crear orden: {res.get('error', 'Error desconocido.')}"

        elif tool_name == "create_table_order":
            if not table_context:
                return "Error: Esta herramienta es solo para clientes en mesa."
            cart = await db.db_get_cart(phone, bot_number)
            if not cart or not cart.get("items"):
                return "Error: El carrito está vacío."

            cart_total    = await orders.get_cart_total(phone, bot_number)
            cart_items    = cart["items"]
            extra_notes   = tool_input.get("notes", "")
            separate_bill = tool_input.get("separate_bill", False)
            items_summary = ", ".join(f"{i['quantity']}x {i['name']}" for i in cart_items)

            # Buscar orden en status 'recibido' para acumular
            active_order = await db.db_get_active_table_order(phone, table_context["id"])

            # CASO 1: cliente pidió cuenta separada → siempre orden nueva
            if separate_bill:
                active_order = None
                print(f"🧾 Cuenta separada solicitada — creando orden nueva", flush=True)

            # CASO 2: hay orden en 'recibido' → acumular en ella
            if active_order:
                await db.db_add_items_to_table_order(
                    active_order["id"], cart_items, cart_total, extra_notes
                )
                await orders.clear_cart(phone, bot_number)
                await db.db_session_mark_order(phone, bot_number)
                new_total = (active_order.get("total") or 0) + cart_total
                print(f"➕ Orden {active_order['id']} actualizada con: {items_summary}", flush=True)
                return (
                    f"OK: Items agregados al pedido existente {active_order['id']}. "
                    f"Nuevos items: {items_summary}. "
                    f"Total acumulado: ${new_total:,} COP."
                )

            # CASO 3: no hay orden en 'recibido' → crear orden nueva
            # (incluye: primera orden, post-entrega, cuenta separada, o pedido adicional
            #  cuando la orden anterior ya está en_preparacion/listo)
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
            reason = "cuenta separada" if separate_bill else "nueva orden"
            print(f"🆕 {reason} {order_id}: {items_summary}", flush=True)
            return (
                f"OK: Pedido {order_id} enviado a cocina. "
                f"Items: {items_summary}. "
                f"Total: ${cart_total:,} COP."
            )

        elif tool_name == "create_reservation":
            name   = tool_input.get("name", "")
            date   = tool_input.get("date", "")
            time   = tool_input.get("time", "")
            guests = int(tool_input.get("guests", 1))
            notes  = tool_input.get("notes", "")
            if not all([name, date, time]):
                return "Error: Faltan datos. Se necesita nombre, fecha y hora."
            await db.db_add_reservation(name, date, time, guests, phone, bot_number, notes)
            return f"OK: Reservación confirmada para {name}, {guests} personas el {date} a las {time}."

        elif tool_name == "call_waiter":
            alert_type = tool_input.get("alert_type", "waiter")
            message    = tool_input.get("message", "El cliente necesita asistencia.")
            if alert_type not in ("bill", "waiter"):
                alert_type = "waiter"
            table_id   = table_context["id"]   if table_context else ""
            table_name = table_context["name"] if table_context else ""
            await db.db_create_waiter_alert(
                phone=phone, bot_number=bot_number,
                alert_type=alert_type, message=message,
                table_id=table_id, table_name=table_name,
            )
            if alert_type == "bill":
                return "OK: Alerta enviada al mesero — irá a cobrar la cuenta en un momento."
            return "OK: Alerta enviada al mesero — viene en camino."

        elif tool_name == "end_session":
            # Guardia 1: pedido en cocina no entregado
            session_state = await get_session_state(phone, bot_number)
            if session_state.get("has_order") and not session_state.get("order_delivered"):
                print(f"⚠️ end_session bloqueado — pedido en cocina aun no entregado para {phone}", flush=True)
                return (
                    "BLOQUEADO: El cliente tiene un pedido en cocina que aun no fue entregado. "
                    "No cierres la sesion. Dile al cliente que su pedido esta en camino."
                )
            # Guardia 2: comida entregada pero factura pendiente
            if session_state.get("order_delivered"):
                has_pending = await db.db_has_pending_invoice(phone)
                if has_pending:
                    print(f"⚠️ end_session bloqueado — factura pendiente para {phone}", flush=True)
                    return (
                        "BLOQUEADO: El cliente recibio su comida pero aun no le han entregado la factura. "
                        "Dile que espere un momento, que el mesero le trae la factura enseguida."
                    )
            farewell = tool_input.get("farewell_message", "")
            await db.db_close_session(
                phone=phone, bot_number=bot_number,
                reason="client_goodbye", closed_by_username=""
            )
            pool = await db.get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM conversations WHERE phone=$1 AND bot_number=$2",
                    phone, bot_number
                )
            print(f"👋 Sesion cerrada por cliente: {phone}", flush=True)
            return f"OK: Sesion finalizada. {farewell}"

        else:
            return f"Error: Herramienta '{tool_name}' no reconocida."

    except Exception as e:
        print(f"❌ execute_tool({tool_name}) error: {traceback.format_exc()}", flush=True)
        return f"Error interno al ejecutar '{tool_name}': {str(e)}"


async def execute_tool_blocks(
    tool_blocks: list,
    phone: str,
    bot_number: str,
    table_context: dict | None
) -> list:
    cart_blocks  = [b for b in tool_blocks if _block_attr(b, "name") == "add_to_cart"]
    other_blocks = [b for b in tool_blocks if _block_attr(b, "name") != "add_to_cart"]
    results_map: dict[str, str] = {}

    for block in cart_blocks:
        bid    = _block_attr(block, "id")    or ""
        inp    = _block_attr(block, "input") or {}
        result = await execute_tool("add_to_cart", inp, phone, bot_number, table_context)
        results_map[bid] = result
        print(f"🛒 add_to_cart '{inp.get('dish_name')}' → {result}", flush=True)

    async def run_other(block):
        name   = _block_attr(block, "name")  or ""
        inp    = _block_attr(block, "input") or {}
        bid    = _block_attr(block, "id")    or ""
        result = await execute_tool(name, inp, phone, bot_number, table_context)
        return bid, result

    if other_blocks:
        pairs = await asyncio.gather(*[run_other(b) for b in other_blocks])
        for bid, result in pairs:
            results_map[bid] = result

    return [
        {
            "type":        "tool_result",
            "tool_use_id": _block_attr(b, "id") or "",
            "content":     results_map.get(_block_attr(b, "id") or "", "Error: resultado no encontrado"),
        }
        for b in tool_blocks
    ]


MAX_TOOL_ITERATIONS = 8

async def chat(user_phone: str, user_message: str, bot_number: str, meta_phone_id: str = "") -> dict:
    table_context = await detect_table_context(user_message, user_phone, bot_number)
    history       = await db.db_get_history(user_phone, bot_number)
    history.append({"role": "user", "content": user_message})
    sys_prompt    = await build_system_prompt(user_phone, bot_number, table_context)

    # Guardar phone_id de Meta en la sesión activa para envíos proactivos
    if meta_phone_id and table_context:
        await db.db_touch_session_with_phone_id(user_phone, bot_number, meta_phone_id)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=sys_prompt,
        messages=history[-20:],
        tools=TOOLS
    )

    iterations = 0
    while response.stop_reason == "tool_use" and iterations < MAX_TOOL_ITERATIONS:
        iterations += 1
        safe_content = _serialize_content(response.content)
        history.append({"role": "assistant", "content": safe_content})

        tool_blocks  = [b for b in response.content if _block_attr(b, "type") == "tool_use"]
        tool_results = await execute_tool_blocks(tool_blocks, user_phone, bot_number, table_context)
        history.append({"role": "user", "content": tool_results})

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=sys_prompt,
            messages=history[-20:],
            tools=TOOLS
        )

    assistant_message = ""
    for block in response.content:
        if _block_attr(block, "type") == "text":
            assistant_message = _block_attr(block, "text") or ""
            if assistant_message:
                break

    if not assistant_message:
        assistant_message = "Listo, tu solicitud fue procesada."

    history.append({"role": "assistant", "content": assistant_message})
    await db.db_save_history(user_phone, bot_number, history)
    return {"message": assistant_message}


async def reset_conversation(user_phone: str):
    await db.db_delete_conversation(user_phone)