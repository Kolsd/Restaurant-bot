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
            "NO usar para domicilio ni recoger."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "notes": {"type": "string", "description": "Notas para cocina: alergias, termino de coccion, etc (opcional)"}
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
            "Usalo SIEMPRE que el cliente se despida: "
            "'hasta luego', 'ya me voy', 'gracias por todo', 'fue todo', 'chao', 'nos vemos'."
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
    # Fuente de verdad: sesión activa en DB
    session = await db.db_get_active_session(phone, bot_number)
    if session and session.get("table_id"):
        table = await db.db_get_table_by_id(session["table_id"])
        if table:
            await db.db_touch_session(phone, bot_number)
            return table

    # Detectar por regex en el mensaje
    import re as _re
    m = _re.search(r'Mesa\s+(\d+)', message, _re.IGNORECASE)
    if not m:
        m = _re.search(r'(?:estoy en|mesa|table)[\s-]*(\d+)', message, _re.IGNORECASE)

    if m:
        number = m.group(1)
        branch_match = _re.search(r'\[branch=(\d+)\]', message)
        if branch_match:
            table_id = f"b{branch_match.group(1)}-mesa-{number}"
        else:
            table_id = f"mesa-{number}"

        table = await db.db_get_table_by_id(table_id)
        if table:
            await db.db_create_table_session(phone, bot_number, table["id"], table["name"])
            return table

    return None


async def build_system_prompt(phone: str, bot_number: str, table_context: dict | None) -> str:
    availability = await db.db_get_menu_availability()
    menu         = await db.db_get_menu(bot_number) or {}
    cart_text    = await orders.cart_summary(phone, bot_number)

    menu_text = ""
    for category, dishes in menu.items():
        av_dishes = [d for d in dishes if availability.get(d['name'], True)]
        if not av_dishes:
            continue
        menu_text += f"\n### {category}\n"
        for dish in av_dishes:
            menu_text += f"- **{dish['name']}** — ${dish['price']:,}\n  {dish['description']}\n"

    table_section = ""
    if table_context:
        table_section = f"""
## 🪑 MODO MESA — DINE-IN
El cliente está en **{table_context['name']}** del restaurante.
- NO pidas dirección de entrega.
- Para mandar el pedido a cocina: usa `create_table_order` (NUNCA `create_order`).
- Si el cliente pide la cuenta, quiere pagar o necesita ayuda física: usa `call_waiter` INMEDIATAMENTE.
- Si el cliente se despide: usa `end_session`.
"""

    return f"""Eres Mesio, el asistente de IA para restaurantes. Eres cálido, eficiente y directo.
{table_section}

### MENÚ DISPONIBLE
{menu_text if menu_text else "Sin menú configurado."}

### CARRITO ACTUAL DEL CLIENTE
{cart_text}

### REGLAS
- Cuando el cliente pida platos → usa `add_to_cart` (una llamada por cada plato distinto).
- Cuando confirme su pedido en mesa → usa `create_table_order`.
- Cuando confirme pedido domicilio/recoger → usa `create_order`.
- Cuando pida la cuenta o al mesero → usa `call_waiter`.
- Cuando se despida → usa `end_session`.
- Después de cada herramienta, confirma brevemente al cliente.
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
            order_id = f"MESA-{uuid.uuid4().hex[:6].upper()}"
            total    = await orders.get_cart_total(phone, bot_number)
            await db.db_save_table_order({
                "id":         order_id,
                "table_id":   table_context["id"],
                "table_name": table_context["name"],
                "phone":      phone,
                "items":      cart["items"],
                "notes":      tool_input.get("notes", ""),
                "total":      total,
                "status":     "recibido"
            })
            await orders.clear_cart(phone, bot_number)
            await db.db_session_mark_order(phone, bot_number)
            items_summary = ", ".join(f"{i['quantity']}x {i['name']}" for i in cart["items"])
            return f"OK: Pedido {order_id} enviado a cocina. Items: {items_summary}. Total: ${total:,} COP."

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
            print(f"👋 Sesión cerrada por cliente: {phone}", flush=True)
            return f"OK: Sesión finalizada. {farewell}"

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

    # add_to_cart en serie (evita race condition en el carrito)
    for block in cart_blocks:
        bid    = _block_attr(block, "id")    or ""
        inp    = _block_attr(block, "input") or {}
        result = await execute_tool("add_to_cart", inp, phone, bot_number, table_context)
        results_map[bid] = result
        print(f"🛒 add_to_cart '{inp.get('dish_name')}' → {result}", flush=True)

    # otras tools en paralelo
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

async def chat(user_phone: str, user_message: str, bot_number: str) -> dict:
    table_context = await detect_table_context(user_message, user_phone, bot_number)
    history       = await db.db_get_history(user_phone, bot_number)
    history.append({"role": "user", "content": user_message})
    sys_prompt    = await build_system_prompt(user_phone, bot_number, table_context)

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

        tool_blocks = [b for b in response.content if _block_attr(b, "type") == "tool_use"]
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