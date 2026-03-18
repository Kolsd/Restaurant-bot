import uuid
import json
from anthropic import Anthropic
from app.data.restaurant import RESTAURANT_INFO, get_top_dishes
from app.services import orders, database as db

client = Anthropic()

TOOLS = [
    {
        "name": "add_to_cart",
        "description": "Agrega un plato al carrito. Úsalo cuando el cliente pida explícitamente algo del menú.",
        "input_schema": {
            "type": "object",
            "properties": {
                "dish_name": {"type": "string", "description": "Nombre exacto del plato"},
                "quantity": {"type": "integer", "description": "Cantidad a ordenar"}
            },
            "required": ["dish_name", "quantity"]
        }
    },
    {
        "name": "create_order",
        "description": "Crea la orden de domicilio o recogida y genera el link de pago.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_type": {"type": "string", "enum": ["domicilio", "recoger"]},
                "address": {"type": "string", "description": "Dirección completa. Vacío si es recoger."},
                "notes": {"type": "string"}
            },
            "required": ["order_type"]
        }
    },
    {
        "name": "create_table_order",
        "description": "Crea una orden para mesa (Dine-in). Envía directo a cocina sin cobrar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "items_summary": {"type": "string", "description": "Resumen de lo pedido. Ej: 2x Pizza, 1x Coca"},
                "notes": {"type": "string"}
            },
            "required": ["items_summary"]
        }
    },
    {
        "name": "create_reservation",
        "description": "Guarda una reservación en el restaurante.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "time": {"type": "string", "description": "HH:MM"},
                "guests": {"type": "integer"},
                "notes": {"type": "string"}
            },
            "required": ["name", "date", "time", "guests"]
        }
    }
]

async def detect_table_context(message: str, phone: str, bot_number: str) -> dict:
    import re as _re
    history = await db.db_get_history(phone, bot_number)
    for msg in reversed(history[-6:]):
        if msg.get('role') == 'user':
            content = msg.get('content', '')
            if isinstance(content, str):
                m = _re.search(r'(?:estoy en|mesa|table)[\s-]*(\d+)', content, _re.IGNORECASE)
                if m:
                    table = await db.db_get_table_by_id(f"mesa-{m.group(1)}")
                    if table: return table
    
    m = _re.search(r'(?:estoy en|mesa|table)[\s-]*(\d+)', message, _re.IGNORECASE)
    if m:
        table = await db.db_get_table_by_id(f"mesa-{m.group(1)}")
        if table: return table
    return None

async def build_system_prompt(phone: str, bot_number: str, table_context: dict = None) -> str:
    availability = await db.db_get_menu_availability()
    menu = await db.db_get_menu(bot_number) or {}
    
    cart_text = await orders.cart_summary(phone, bot_number)
    
    menu_text = ""
    for category, dishes in menu.items():
        av_dishes = [d for d in dishes if availability.get(d['name'], True)]
        if not av_dishes: continue
        menu_text += f"\n### {category}\n"
        for dish in av_dishes:
            menu_text += f"- **{dish['name']}** - ${dish['price']}\n  {dish['description']}\n"

    table_section = ""
    if table_context:
        table_section = f"""
## 🪑 MODO MESA — PEDIDO EN RESTAURANTE
El cliente está físicamente en **{table_context['name']}**.
- NO pidas dirección de entrega.
- Usa EXCLUSIVAMENTE la herramienta 'create_table_order' para mandar el pedido a cocina, NO uses 'create_order'.
"""

    return f"""Eres Mesio, IA de ventas para restaurantes. Eres cálido y directo.
{table_section}

### MENÚ DISPONIBLE:
{menu_text}

### ESTADO DEL CARRITO DEL CLIENTE:
{cart_text}

### REGLAS:
- Si el cliente pide algo, usa la herramienta `add_to_cart`.
- Tras agregar al carrito, avisa qué agregaste, diles el TOTAL ACTUAL basándote en el carrito y pregunta si desea algo más.
- Para cobrar (pedidos externos), usa `create_order`.
- Para pedidos en la mesa del restaurante, usa `create_table_order`.
"""

async def execute_tool(tool_name: str, tool_input: dict, phone: str, bot_number: str, table_context: dict):
    if tool_name == "add_to_cart":
        res = await orders.add_to_cart(phone, tool_input["dish_name"], tool_input["quantity"], bot_number)
        return "Éxito: Plato agregado" if res["success"] else f"Error: {res.get('error')}"
    
    elif tool_name == "create_order":
        res = await orders.create_order(phone, tool_input["order_type"], tool_input.get("address",""), tool_input.get("notes",""), bot_number)
        if res["success"]:
            order = res["order"]
            await db.db_save_order(order)
            return f"Orden {order['id']} creada. Link de pago: {order['payment_url']}"
        return f"Error: {res['error']}"

    elif tool_name == "create_table_order" and table_context:
        order_id = f"MESA-{uuid.uuid4().hex[:6].upper()}"
        items = [{"name": item.strip(), "quantity": 1} for item in tool_input["items_summary"].split(',') if item.strip()]
        await db.db_save_table_order({
            "id": order_id, "table_id": table_context['id'], "table_name": table_context['name'],
            "phone": phone, "items": items, "notes": tool_input.get("notes",""), "total": 0, "status": "recibido"
        })
        await orders.clear_cart(phone, bot_number)
        return "Pedido enviado a cocina exitosamente. Carrito vaciado."

    elif tool_name == "create_reservation":
        await db.db_add_reservation(tool_input["name"], tool_input["date"], tool_input["time"], tool_input["guests"], phone, bot_number, tool_input.get("notes",""))
        return "Reservación confirmada en sistema."
        
    return "Herramienta desconocida o contexto inválido"

async def chat(user_phone: str, user_message: str, bot_number: str) -> dict:
    table_context = await detect_table_context(user_message, user_phone, bot_number)
    history = await db.db_get_history(user_phone, bot_number)
    history.append({"role": "user", "content": user_message})

    sys_prompt = await build_system_prompt(user_phone, bot_number, table_context)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=sys_prompt,
        messages=history[-20:],
        tools=TOOLS
    )

    if response.stop_reason == "tool_use":
        safe_content = [block.model_dump() if hasattr(block, 'model_dump') else block for block in response.content]
        history.append({"role": "assistant", "content": safe_content})
        
        tool_results = []
        
        for block in response.content:
            # Manejo robusto: verificamos si block es un diccionario o un objeto
            block_type = block.get('type') if isinstance(block, dict) else block.type
            
            if block_type == "tool_use":
                block_name = block.get('name') if isinstance(block, dict) else block.name
                block_input = block.get('input') if isinstance(block, dict) else block.input
                block_id = block.get('id') if isinstance(block, dict) else block.id
                
                result_str = await execute_tool(block_name, block_input, user_phone, bot_number, table_context)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block_id,
                    "content": result_str
                })
        
        history.append({"role": "user", "content": tool_results})
        
        final_response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=sys_prompt,
            messages=history[-20:],
            tools=TOOLS
        )
        
        assistant_message = ""
        for block in final_response.content:
            block_type = block.get('type') if isinstance(block, dict) else block.type
            if block_type == "text":
                assistant_message = block.get('text') if isinstance(block, dict) else block.text
                break
        
        if not assistant_message:
            assistant_message = "Tu orden ha sido procesada."
            
        history.append({"role": "assistant", "content": assistant_message})
    else:
        # Manejo si la respuesta inicial no es tool_use (es un texto directo)
        assistant_message = ""
        for block in response.content:
            block_type = block.get('type') if isinstance(block, dict) else block.type
            if block_type == "text":
                assistant_message = block.get('text') if isinstance(block, dict) else block.text
                break
        history.append({"role": "assistant", "content": assistant_message})

    await db.db_save_history(user_phone, bot_number, history)
    return {"message": assistant_message}

async def reset_conversation(user_phone: str):
    await db.db_delete_conversation(user_phone)