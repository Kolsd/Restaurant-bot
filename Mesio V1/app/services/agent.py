import re
import uuid
from anthropic import Anthropic
#from app.data.restaurant import RESTAURANT_INFO, MENU, get_top_dishes
from app.services.orders import add_to_cart, remove_from_cart, cart_summary, create_order, clear_cart
from app.services import database as db

client = Anthropic()

async def build_system_prompt(restaurant: dict, menu: dict, top_dishes: list, table_context: dict = None) -> str:
    """
    Construye el prompt del sistema usando datos dinámicos de la DB, instrucciones de Efecto Espejo, 
    recordatorio de preguntar alergias antes de confirmar, y respeta el menú actual de la DB.
    """
    # Construir menú
    menu_text = ""
    for category, dishes in menu.items():
        available_dishes = []
        for dish in dishes:
            if not dish.get("available", True):
                continue
            available_dishes.append(dish)
        if not available_dishes:
            continue
        menu_text += f"\n### {category}\n"
        for dish in available_dishes:
            veg = "🌱" if dish.get("vegetarian") else ""
            price = dish.get('price', '?')
            desc = dish.get('description', '')
            menu_text += f"- **{dish['name']}** {veg} - ${price} MXN\n"
            menu_text += f"  {desc}\n"

    # Top dishes (con filtro de disponibilidad)
    top_show = [d for d in top_dishes if d.get("available", True)][:5]
    top_text = "\n".join([f"- {d['name']} ({d.get('orders', 0)} pedidos)" for d in top_show])

    # Horarios
    hours = restaurant.get("hours", {})
    hours_text = "\n".join([f"- {day.capitalize()}: {h}" for day, h in hours.items()])

    # Contexto mesa
    table_section = ""
    if table_context:
        table_section = f"""
## 🪑 MODO MESA — PEDIDO EN RESTAURANTE
El cliente está físicamente en **{table_context['name']}** del restaurante.
- NO pidas dirección de domicilio
- NO cobres cargo por domicilio
- Los pedidos van directo a cocina para servir en la mesa
- Usa la tag [PEDIDO_MESA: items|notas] para enviar el pedido a cocina
- Ejemplo: [PEDIDO_MESA: 2x Margherita, 1x Carbonara|sin gluten en la pasta]
- Antes de confirmar el pedido pregunta explícitamente por alergias alimentarias.
- Confirma el pedido y dile al cliente que llegará a su mesa en breve
"""

    # Efecto Espejo y alergias
    efecto_espejo_instruccion = (
        "**EFECTO ESPEJO:** Imita brevemente el tono o formalidad del cliente en tus mensajes, "
        "pero siempre manteniendo profesionalismo y amabilidad."
    )
    alergias_instruccion = (
        "**ANTES DE CONFIRMAR UN PEDIDO o RESERVACION**, pregunta si hay alguna alergia o restricción alimenticia importante."
    )

    return f"""{efecto_espejo_instruccion}

Eres el asistente virtual de WhatsApp de **{restaurant['name']}**, un restaurante italiano en {restaurant.get('city','Ciudad de México')}.

Tu personalidad es: cálida, amable, profesional y un poco italiana 🇮🇹. Usas emojis ocasionalmente para dar cercanía.

{alergias_instruccion}

## TU MISIÓN
Ayudar a los clientes con:
1. **Información del menú** - precios, ingredientes, opciones
2. **Reservaciones** - tomar datos y confirmar
3. **Pedidos** - domicilio, para recoger, o en mesa
4. **Horarios y ubicación**
5. **Recomendaciones** - basadas en los platos más populares
6. **Escalar a humano** - cuando no puedas ayudar
{table_section}

---

## INFORMACIÓN DEL RESTAURANTE

📍 **Dirección:** {restaurant.get('address', '')}
📞 **Teléfono:** {restaurant.get('phone', '')}
📸 **Instagram:** {restaurant.get('instagram', '')}

### Horarios:
{hours_text}

---

## MENÚ DISPONIBLE HOY
{menu_text}

**IMPORTANTE:** Solo puedes ofrecer y agregar al carrito los platos que aparecen en este menú. Si un cliente pide algo que no está en la lista, dile amablemente que ese plato no está disponible hoy y ofrece una alternativa del menú.

---

## 🔥 PLATOS MÁS PEDIDOS
{top_text}

---

## REGLAS

### RESERVACIONES:
- Necesitas: nombre completo, fecha, hora, número de personas, teléfono
- Máximo 15 personas por WhatsApp (más → llamar)
- Una vez que tengas TODOS los datos di: [RESERVACION: nombre|fecha|hora|personas|telefono|notas]

### PEDIDOS (domicilio o para recoger):
1. Confirma si es domicilio o para recoger
2. Toma los platos. Para agregar: [AGREGAR: nombre_plato|cantidad]
3. Para eliminar: [ELIMINAR: nombre_plato]
4. Para mostrar carrito: [VER_CARRITO]
5. Para crear la orden:
   - Domicilio: pide dirección → [CREAR_ORDEN: domicilio|dirección|notas]
   - Recoger: [CREAR_ORDEN: recoger||notas]
Domicilio tiene costo de $5,000 COP adicional.

### ESCALAR:
- Di [ESCALAR: motivo] cuando no puedas resolver algo

### TONO:
- Respuestas CORTAS (WhatsApp, no email)
- Máximo 3-4 líneas
- Nunca seas robótico
"""

async def process_agent_response(response_text: str, user_phone: str, table_context: dict = None) -> dict:  # noqa: C901
    actions = []
    clean_response = response_text
    process_agent_response._table_context = table_context

    # Reservación
    res_match = re.search(r'\[RESERVACION: ([^\]]+)\]', response_text)
    if res_match:
        datos = res_match.group(1).split('|')
        if len(datos) >= 5:
            reservation = await db.db_add_reservation(
                name=datos[0].strip(), date=datos[1].strip(),
                time=datos[2].strip(), guests=int(datos[3].strip()),
                phone=datos[4].strip(), notes=datos[5].strip() if len(datos) > 5 else ""
            )
            actions.append({"type": "reservation_created", "data": reservation})
        clean_response = re.sub(r'\[RESERVACION: [^\]]+\]', '', clean_response).strip()

    # Escalar
    escalar_match = re.search(r'\[ESCALAR: ([^\]]+)\]', response_text)
    if escalar_match:
        actions.append({"type": "escalate_to_human", "reason": escalar_match.group(1), "user_phone": user_phone})
        clean_response = re.sub(r'\[ESCALAR: [^\]]+\]', '', clean_response).strip()

    # Agregar al carrito
    for match in re.finditer(r'\[AGREGAR: ([^\]]+)\]', response_text):
        parts = match.group(1).split('|')
        dish_name = parts[0].strip()
        quantity = int(parts[1].strip()) if len(parts) > 1 else 1

        # Verificar disponibilidad antes de agregar
        # NOTA: en este contexto ya no se usa MENU ni availability de archivo, 
        # sino que se debe consultar la DB en cada llamada que requiera menú.
        availability = await db.db_get_menu_availability()
        if availability.get(dish_name, True) is False:
            clean_response = re.sub(r'\[AGREGAR: [^\]]+\]',
                f"Lo siento, {dish_name} no está disponible hoy. ¿Te ofrezco algo más del menú?",
                clean_response).strip()
        else:
            result = add_to_cart(user_phone, dish_name, quantity)
            actions.append({"type": "add_to_cart", "result": result})
            clean_response = re.sub(r'\[AGREGAR: [^\]]+\]', '', clean_response).strip()

    # Eliminar del carrito
    for match in re.finditer(r'\[ELIMINAR: ([^\]]+)\]', response_text):
        result = remove_from_cart(user_phone, match.group(1).strip())
        actions.append({"type": "remove_from_cart", "result": result})
        clean_response = re.sub(r'\[ELIMINAR: [^\]]+\]', '', clean_response).strip()

    # Ver carrito
    if '[VER_CARRITO]' in response_text:
        summary = cart_summary(user_phone)
        clean_response = re.sub(r'\[VER_CARRITO\]', summary, clean_response).strip()
        actions.append({"type": "view_cart"})

    # Pedido de mesa (dine-in)
    mesa_match = re.search(r'\[PEDIDO_MESA: ([^\]]+)\]', response_text)
    if mesa_match:
        parts = mesa_match.group(1).split('|')
        items_text = parts[0].strip()
        notes = parts[1].strip() if len(parts) > 1 else ""
        if hasattr(process_agent_response, '_table_context') and process_agent_response._table_context:
            tc = process_agent_response._table_context
            order_id = f"MESA-{tc['id'][:8].upper()}-{uuid.uuid4().hex[:4].upper()}"
            items = [{"name": item.strip(), "quantity": 1} for item in items_text.split(',') if item.strip()]
            table_order = {
                "id": order_id,
                "table_id": tc['id'],
                "table_name": tc['name'],
                "phone": user_phone,
                "items": items,
                "notes": notes,
                "status": "recibido",
                "total": 0
            }
            await db.db_save_table_order(table_order)
            actions.append({"type": "table_order_created", "order": table_order})
            confirm_msg = f"✅ ¡Pedido recibido! Llegará a {tc['name']} en breve 🍽️"
            clean_response = re.sub(r'\[PEDIDO_MESA: [^\]]+\]', confirm_msg, clean_response).strip()
        else:
            clean_response = re.sub(r'\[PEDIDO_MESA: [^\]]+\]', '', clean_response).strip()

    # Crear orden
    crear_match = re.search(r'\[CREAR_ORDEN: ([^\]]+)\]', response_text)
    if crear_match:
        parts = crear_match.group(1).split('|')
        order_type = parts[0].strip()
        address = parts[1].strip() if len(parts) > 1 else None
        notes = parts[2].strip() if len(parts) > 2 else ""
        result = create_order(user_phone, order_type, address, notes)
        if result["success"]:
            order = result["order"]
            await db.db_save_order(order)
            payment_msg = (
                f"\n\n✅ *Pedido {order['id']} creado*\n"
                f"Total: ${order['total']:,} COP\n"
                f"{'🛵 Domicilio a: ' + order['address'] if order['order_type'] == 'domicilio' else '🏃 Para recoger en el restaurante'}\n\n"
                f"💳 *Paga aquí:*\n{order['payment_url']}\n\n"
                f"_Una vez confirmado el pago comenzamos tu pedido_ 🍽️"
            )
            clean_response = re.sub(r'\[CREAR_ORDEN: [^\]]+\]', payment_msg, clean_response).strip()
            actions.append({"type": "order_created", "order": order})
        else:
            clean_response = re.sub(r'\[CREAR_ORDEN: [^\]]+\]', f"❌ {result['error']}", clean_response).strip()

    return {"message": clean_response, "actions": actions}


async def detect_table_context(message: str, phone: str) -> dict:
    """Detecta si el mensaje viene de un QR de mesa."""
    import re as _re
    history = await db.db_get_history(phone)
    for msg in reversed(history[-6:]):
        if msg.get('role') == 'user':
            m = _re.search(r'(?:estoy en|mesa|table)[\s-]*(\d+)', msg['content'], _re.IGNORECASE)
            if m:
                table_id = f"mesa-{m.group(1)}"
                table = await db.db_get_table_by_id(table_id)
                if table:
                    return table
    m = _re.search(r'(?:estoy en|mesa|table)[\s-]*(\d+)', message, _re.IGNORECASE)
    if m:
        table_id = f"mesa-{m.group(1)}"
        table = await db.db_get_table_by_id(table_id)
        if table:
            return table
    return None


async def chat(user_phone: str, user_message: str, bot_number: str) -> dict:
    """
    Ahora recibe bot_number, busca el restaurante en la DB usando ese número, y usa el menú actualizado de la DB.
    """
    # Obtener restaurante por bot_number
    restaurant = await db.db_get_restaurant_by_bot_number(bot_number)
    if not restaurant:
        return {"message": "❌ Restaurante no encontrado para este número.", "actions": []}
    
    # Validación de suscripción vencida o bloqueada
    if restaurant.get('subscription_status') == 'past_due':
        return {"message": "Lo sentimos, el servicio de atención virtual de este establecimiento se encuentra temporalmente inactivo. Por favor, contacta a un mesero directamente.", "actions": []}

    # Checar si la conversación está pausada para este usuario
    conversation_details = await db.db_get_conversation_details(user_phone)
    if conversation_details and conversation_details.get('bot_paused') is True:
        return None

    # Detectar contexto de mesa
    table_context = await detect_table_context(user_message, user_phone)

    # Menú y platos más vendidos desde DB
    menu = await db.db_get_menu(restaurant['id'])
    # Añadir info disponibilidad a cada plato del menú según DB si aplica
    menu_availability = await db.db_get_menu_availability(restaurant['id'])
    for category, dishes in menu.items():
        for dish in dishes:
            dish['available'] = menu_availability.get(dish['name'], True)

    top_dishes = await db.db_get_top_dishes(restaurant['id'])
    # Añadir info disponibilidad a cada top dish según DB
    for dish in top_dishes:
        dish['available'] = menu_availability.get(dish['name'], True)

    # Historial (máx 20)
    history = await db.db_get_history(user_phone)
    history.append({"role": "user", "content": user_message})

    # Construir el system prompt
    system_prompt = await build_system_prompt(restaurant, menu, top_dishes, table_context)

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=system_prompt,
        messages=history[-20:]
    )

    assistant_message = response.content[0].text
    history.append({"role": "assistant", "content": assistant_message})

    await db.db_save_history(user_phone, history)
    return await process_agent_response(assistant_message, user_phone, table_context)


async def reset_conversation(user_phone: str):
    await db.db_delete_conversation(user_phone)
