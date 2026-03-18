import uuid
import json
import traceback
from anthropic import Anthropic
from app.services import orders, database as db

client = Anthropic()

# ─────────────────────────────────────────────────────────────────────────────
# TOOLS
# Cada tool debe tener input_schema 100% válido (type/properties/required).
# La descripción usa comillas simples internas para evitar escape issues.
# ─────────────────────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "add_to_cart",
        "description": (
            "Agrega un plato al carrito del cliente. "
            "Usalo SIEMPRE que el cliente mencione querer pedir algo del menu. "
            "Puedes llamar esta herramienta varias veces en paralelo si el cliente pide varios platos."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dish_name": {
                    "type": "string",
                    "description": "Nombre del plato tal como aparece en el menu"
                },
                "quantity": {
                    "type": "integer",
                    "description": "Cantidad a ordenar (minimo 1)"
                }
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
                "order_type": {
                    "type": "string",
                    "enum": ["domicilio", "recoger"],
                    "description": "Tipo de pedido"
                },
                "address": {
                    "type": "string",
                    "description": "Direccion completa de entrega. Dejar vacio si es para recoger."
                },
                "notes": {
                    "type": "string",
                    "description": "Notas adicionales del pedido (alergias, instrucciones, etc)"
                }
            },
            "required": ["order_type"]
        }
    },
    {
        "name": "create_table_order",
        "description": (
            "Envia el pedido a cocina para clientes en mesa (dine-in). "
            "Usalo SIEMPRE cuando el cliente en mesa confirme su pedido. "
            "Los items se leen automaticamente del carrito, no los necesitas pasar. "
            "NO usar para domicilio ni recoger."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "notes": {
                    "type": "string",
                    "description": "Notas para cocina: alergias, termino de coccion, etc (opcional)"
                }
            },
            "required": []
        }
    },
    {
        "name": "create_reservation",
        "description": (
            "Guarda una reservacion en el restaurante. "
            "Usalo cuando el cliente quiera reservar mesa para una fecha y hora especifica."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Nombre completo del cliente para la reservacion"
                },
                "date": {
                    "type": "string",
                    "description": "Fecha en formato YYYY-MM-DD"
                },
                "time": {
                    "type": "string",
                    "description": "Hora en formato HH:MM (24h)"
                },
                "guests": {
                    "type": "integer",
                    "description": "Numero de personas"
                },
                "notes": {
                    "type": "string",
                    "description": "Notas especiales: ocasion especial, preferencias, etc (opcional)"
                }
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
            "'pueden venir a mi mesa', o cualquier solicitud que requiera presencia fisica. "
            "NO usar para agregar platos al carrito ni para procesar pagos digitales."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "alert_type": {
                    "type": "string",
                    "enum": ["bill", "waiter"],
                    "description": (
                        "Tipo de alerta: "
                        "'bill' cuando el cliente pide la cuenta o quiere pagar, "
                        "'waiter' para cualquier otra asistencia fisica del mesero"
                    )
                },
                "message": {
                    "type": "string",
                    "description": "Descripcion breve de lo que necesita el cliente, ej: 'Solicita la cuenta' o 'Necesita servilletas'"
                }
            },
            "required": ["alert_type", "message"]
        }
    }
]


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _block_attr(block, attr: str):
    """Lee un atributo de un bloque que puede ser dict o objeto SDK."""
    if isinstance(block, dict):
        return block.get(attr)
    return getattr(block, attr, None)


def _serialize_content(content_blocks) -> list:
    """
    Convierte la lista de bloques de respuesta del SDK de Anthropic en
    dicts planos seguros para guardar en historial y reenviar a la API.

    Soporta:
    - Objetos con .model_dump()  (anthropic>=0.25)
    - Objetos con .__dict__
    - Dicts planos
    """
    result = []
    for block in content_blocks:
        if isinstance(block, dict):
            result.append(block)
        elif hasattr(block, "model_dump"):
            result.append(block.model_dump())
        elif hasattr(block, "__dict__"):
            result.append(dict(block.__dict__))
        else:
            # fallback: intentar JSON round-trip
            try:
                result.append(json.loads(json.dumps(block, default=str)))
            except Exception:
                pass  # bloque corrupto: lo ignoramos, no rompemos
    return result


# ─────────────────────────────────────────────────────────────────────────────
# DETECT TABLE CONTEXT
# ─────────────────────────────────────────────────────────────────────────────

async def detect_table_context(message: str, phone: str, bot_number: str) -> dict | None:
    """
    Detecta si el cliente está en una mesa buscando en:
    1. Mensaje actual
    2. Historial completo (para que el contexto no se pierda con el tiempo)
    """
    import re as _re

    pattern = r'(?:estoy en|mesa|table)[\s-]*(\d+)'

    # Primero buscar en el mensaje actual
    m = _re.search(pattern, message, _re.IGNORECASE)
    if m:
        table = await db.db_get_table_by_id(f"mesa-{m.group(1)}")
        if table:
            return table

    # Luego en historial (todo, no solo los últimos N)
    history = await db.db_get_history(phone, bot_number)
    for msg in reversed(history):
        if msg.get('role') == 'user':
            content = msg.get('content', '')
            if isinstance(content, str):
                m = _re.search(pattern, content, _re.IGNORECASE)
                if m:
                    table = await db.db_get_table_by_id(f"mesa-{m.group(1)}")
                    if table:
                        return table

    return None


# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPT
# ─────────────────────────────────────────────────────────────────────────────

async def build_system_prompt(phone: str, bot_number: str, table_context: dict | None) -> str:
    availability = await db.db_get_menu_availability()
    menu         = await db.db_get_menu(bot_number) or {}
    cart_text    = await orders.cart_summary(phone, bot_number)

    # Construir texto del menu filtrando no disponibles
    menu_text = ""
    for category, dishes in menu.items():
        av_dishes = [d for d in dishes if availability.get(d['name'], True)]
        if not av_dishes:
            continue
        menu_text += f"\n### {category}\n"
        for dish in av_dishes:
            menu_text += f"- **{dish['name']}** — ${dish['price']:,}\n  {dish['description']}\n"

    # Sección extra si el cliente está en mesa
    table_section = ""
    if table_context:
        table_section = f"""
## 🪑 MODO MESA — DINE-IN
El cliente está en **{table_context['name']}** del restaurante.
Reglas específicas para este modo:
- NO pidas dirección de entrega.
- Para mandar el pedido a cocina: usa `create_table_order` (NUNCA `create_order`).
- Si el cliente pide la cuenta, quiere pagar o necesita ayuda física: usa `call_waiter` INMEDIATAMENTE.
- Puedes usar `add_to_cart` y `call_waiter` en la misma respuesta si el cliente pide platos y también llama al mesero.
"""

    return f"""Eres Mesio, el asistente de IA para restaurantes. Eres cálido, eficiente y directo.
{table_section}

### MENÚ DISPONIBLE
{menu_text if menu_text else "Sin menú configurado."}

### CARRITO ACTUAL DEL CLIENTE
{cart_text}

### REGLAS GENERALES
- Cuando el cliente pida platos → usa `add_to_cart` (puedes llamarla varias veces en paralelo).
- Cuando confirme su pedido en mesa → usa `create_table_order`.
- Cuando confirme pedido domicilio/recoger → usa `create_order`.
- Cuando pida la cuenta o al mesero → usa `call_waiter`.
- Puedes combinar herramientas en una sola respuesta cuando el contexto lo requiera.
- Después de usar una herramienta, confirma brevemente al cliente lo que hiciste.
"""


# ─────────────────────────────────────────────────────────────────────────────
# EXECUTE TOOL — cada tool tiene su propio try/except para no romper las demás
# ─────────────────────────────────────────────────────────────────────────────

async def execute_tool(
    tool_name: str,
    tool_input: dict,
    phone: str,
    bot_number: str,
    table_context: dict | None
) -> str:
    """
    Ejecuta una herramienta y SIEMPRE devuelve un string.
    Nunca lanza excepción — los errores se devuelven como string
    para que Claude pueda informar al cliente y continuar.
    """
    try:
        # ── add_to_cart ──────────────────────────────────────────────────────
        if tool_name == "add_to_cart":
            dish_name = tool_input.get("dish_name", "")
            quantity  = int(tool_input.get("quantity", 1))
            if not dish_name:
                return "Error: Se necesita el nombre del plato."
            res = await orders.add_to_cart(phone, dish_name, quantity, bot_number)
            if res["success"]:
                return f"OK: '{res['dish']['name']}' x{quantity} agregado al carrito."
            return f"Error al agregar '{dish_name}': {res.get('error', 'Plato no encontrado en el menú.')}"

        # ── create_order ─────────────────────────────────────────────────────
        elif tool_name == "create_order":
            order_type = tool_input.get("order_type", "recoger")
            address    = tool_input.get("address", "")
            notes      = tool_input.get("notes", "")
            if order_type == "domicilio" and not address:
                return "Error: Se necesita la dirección de entrega para pedidos a domicilio."
            res = await orders.create_order(phone, order_type, address, notes, bot_number)
            if res["success"]:
                order = res["order"]
                await db.db_save_order(order)
                return (
                    f"OK: Orden {order['id']} creada. "
                    f"Total: ${order['total']:,} COP. "
                    f"Link de pago: {order['payment_url']}"
                )
            return f"Error al crear orden: {res.get('error', 'Error desconocido.')}"

        # ── create_table_order ───────────────────────────────────────────────
        elif tool_name == "create_table_order":
            if not table_context:
                return "Error: Esta herramienta es solo para clientes en mesa."
            cart = await db.db_get_cart(phone, bot_number)
            if not cart or not cart.get("items"):
                return "Error: El carrito está vacío. El cliente debe agregar platos primero."
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
            items_summary = ", ".join(
                f"{i['quantity']}x {i['name']}" for i in cart["items"]
            )
            return (
                f"OK: Pedido {order_id} enviado a cocina. "
                f"Items: {items_summary}. Total: ${total:,} COP. "
                f"Carrito vaciado."
            )

        # ── create_reservation ───────────────────────────────────────────────
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

        # ── call_waiter ──────────────────────────────────────────────────────
        elif tool_name == "call_waiter":
            alert_type = tool_input.get("alert_type", "waiter")
            message    = tool_input.get("message", "El cliente necesita asistencia.")
            if alert_type not in ("bill", "waiter"):
                alert_type = "waiter"
            table_id   = table_context["id"]   if table_context else ""
            table_name = table_context["name"] if table_context else ""
            await db.db_create_waiter_alert(
                phone=phone,
                bot_number=bot_number,
                alert_type=alert_type,
                message=message,
                table_id=table_id,
                table_name=table_name,
            )
            if alert_type == "bill":
                return "OK: Alerta enviada al mesero — irá a cobrar la cuenta en un momento."
            return "OK: Alerta enviada al mesero — viene en camino."

        # ── tool desconocida ─────────────────────────────────────────────────
        else:
            return f"Error: Herramienta '{tool_name}' no reconocida."

    except Exception as e:
        # Capturamos CUALQUIER excepción para que no rompa el loop de tool_use
        print(f"❌ execute_tool({tool_name}) error: {traceback.format_exc()}", flush=True)
        return f"Error interno al ejecutar '{tool_name}': {str(e)}"


# ─────────────────────────────────────────────────────────────────────────────
# CHAT — loop blindado contra tools paralelas y errores intermedios
# ─────────────────────────────────────────────────────────────────────────────

MAX_TOOL_ITERATIONS = 8  # Tope de seguridad contra loops infinitos

async def chat(user_phone: str, user_message: str, bot_number: str) -> dict:
    table_context = await detect_table_context(user_message, user_phone, bot_number)
    history       = await db.db_get_history(user_phone, bot_number)
    history.append({"role": "user", "content": user_message})
    sys_prompt    = await build_system_prompt(user_phone, bot_number, table_context)

    # Primera llamada a la API
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

        # 1. Serializar el contenido del assistant (objetos SDK → dicts planos)
        safe_content = _serialize_content(response.content)
        history.append({"role": "assistant", "content": safe_content})

        # 2. Ejecutar TODAS las tools del turno en paralelo (asyncio.gather)
        #    Si Claude llama add_to_cart + call_waiter al mismo tiempo, ambas corren.
        import asyncio

        tool_blocks = [
            block for block in response.content
            if _block_attr(block, "type") == "tool_use"
        ]

        async def run_one(block):
            name  = _block_attr(block, "name")  or ""
            inp   = _block_attr(block, "input") or {}
            bid   = _block_attr(block, "id")    or ""
            # execute_tool nunca lanza excepción — siempre devuelve string
            result = await execute_tool(name, inp, user_phone, bot_number, table_context)
            return {
                "type":        "tool_result",
                "tool_use_id": bid,
                "content":     result,
            }

        tool_results = await asyncio.gather(*[run_one(b) for b in tool_blocks])

        # 3. Agregar los resultados al historial como turno "user"
        #    (requerimiento de la API de Anthropic)
        history.append({"role": "user", "content": list(tool_results)})

        # 4. Siguiente llamada con el historial actualizado
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=sys_prompt,
            messages=history[-20:],
            tools=TOOLS
        )

    # Extraer el texto final de la respuesta
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