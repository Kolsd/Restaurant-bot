import json
import re
from anthropic import Anthropic
from app.data.restaurant import RESTAURANT_INFO, MENU, get_top_dishes, add_reservation, reservations

client = Anthropic()

# Historial de conversaciones por usuario (en producción usar Redis o DB)
conversation_history: dict[str, list] = {}

def build_system_prompt() -> str:
    """Construye el system prompt con toda la info del restaurante."""
    
    menu_text = ""
    for category, dishes in MENU.items():
        menu_text += f"\n### {category}\n"
        for dish in dishes:
            veg = "🌱" if dish["vegetarian"] else ""
            menu_text += f"- **{dish['name']}** {veg} - ${dish['price']} MXN\n"
            menu_text += f"  {dish['description']}\n"

    top_dishes = get_top_dishes(5)
    top_text = "\n".join([f"- {d['name']} ({d['orders']} pedidos)" for d in top_dishes])

    hours_text = "\n".join([f"- {day.capitalize()}: {hours}" for day, hours in RESTAURANT_INFO["hours"].items()])

    return f"""Eres el asistente virtual de WhatsApp de **{RESTAURANT_INFO['name']}**, un restaurante italiano en Ciudad de México.

Tu personalidad es: cálida, amable, profesional y un poco italiana 🇮🇹. Usas emojis ocasionalmente para dar cercanía.

## TU MISIÓN
Ayudar a los clientes con:
1. **Información del menú** - precios, ingredientes, opciones
2. **Reservaciones** - tomar datos y confirmar
3. **Horarios y ubicación**
4. **Recomendaciones personalizadas** - basadas en los platos más populares
5. **Escalar a humano** - cuando no puedas ayudar

---

## INFORMACIÓN DEL RESTAURANTE

📍 **Dirección:** {RESTAURANT_INFO['address']}
📞 **Teléfono:** {RESTAURANT_INFO['phone']}
📸 **Instagram:** {RESTAURANT_INFO['instagram']}
🗺️ **Maps:** {RESTAURANT_INFO['google_maps']}

### Horarios:
{hours_text}

---

## MENÚ COMPLETO
{menu_text}

---

## 🔥 PLATOS MÁS PEDIDOS (para recomendar)
{top_text}

---

## REGLAS IMPORTANTES

### Para RESERVACIONES:
- Necesitas obtener: nombre completo, fecha, hora, número de personas, teléfono
- Horario de reservas: solo dentro del horario de apertura
- Máximo 15 personas por reservación vía WhatsApp (más, llamar)
- Una vez que tengas TODOS los datos, di: [RESERVACION: nombre|fecha|hora|personas|telefono|notas]
  Ejemplo: [RESERVACION: Juan García|2025-03-20|20:00|4|5512345678|cumpleaños]

### Para ESCALAR A HUMANO:
- Si el cliente pregunta algo que no puedes resolver
- Si hay una queja grave
- Si solicita algo muy específico (alergias severas, eventos corporativos)
- Di: [ESCALAR: motivo breve]
  Ejemplo: [ESCALAR: cliente tiene alergia severa a nueces]

### Para RECOMENDACIONES:
- Siempre menciona los platos más populares
- Pregunta si es vegetariano o tiene alguna preferencia
- Sugiere maridaje (vino con pasta/pizza, café con postre)

### Tono:
- Respuestas CORTAS y directas (WhatsApp, no email)
- Máximo 3-4 líneas por mensaje
- Si hay mucha info, usa listas cortas con emojis
- Nunca seas robótico

### Lo que NO puedes hacer:
- Aceptar pedidos para delivery (solo reservaciones y preguntas)
- Dar precios con descuento sin autorización
- Hacer promesas sobre tiempos de espera exactos
"""

def process_agent_response(response_text: str, user_phone: str) -> dict:
    """Procesa la respuesta del agente y ejecuta acciones especiales."""
    
    actions = []
    clean_response = response_text

    # Detectar reservación
    reservacion_match = re.search(r'\[RESERVACION: ([^\]]+)\]', response_text)
    if reservacion_match:
        datos = reservacion_match.group(1).split('|')
        if len(datos) >= 5:
            reservation = add_reservation(
                name=datos[0].strip(),
                date=datos[1].strip(),
                time=datos[2].strip(),
                guests=int(datos[3].strip()),
                phone=datos[4].strip(),
                notes=datos[5].strip() if len(datos) > 5 else ""
            )
            actions.append({"type": "reservation_created", "data": reservation})
        clean_response = re.sub(r'\[RESERVACION: [^\]]+\]', '', clean_response).strip()

    # Detectar escalamiento
    escalar_match = re.search(r'\[ESCALAR: ([^\]]+)\]', response_text)
    if escalar_match:
        motivo = escalar_match.group(1)
        actions.append({"type": "escalate_to_human", "reason": motivo, "user_phone": user_phone})
        clean_response = re.sub(r'\[ESCALAR: [^\]]+\]', '', clean_response).strip()

    return {"message": clean_response, "actions": actions}


def chat(user_phone: str, user_message: str) -> dict:
    """
    Procesa un mensaje del usuario y retorna la respuesta del agente.
    
    Args:
        user_phone: Número de teléfono del usuario (identificador único)
        user_message: Mensaje enviado por el usuario
    
    Returns:
        dict con 'message' (respuesta) y 'actions' (acciones ejecutadas)
    """
    
    # Inicializar historial si es nuevo usuario
    if user_phone not in conversation_history:
        conversation_history[user_phone] = []

    # Agregar mensaje del usuario al historial
    conversation_history[user_phone].append({
        "role": "user",
        "content": user_message
    })

    # Limitar historial a últimos 20 mensajes (evitar tokens excesivos)
    history = conversation_history[user_phone][-20:]

    # Llamar a Claude API
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=build_system_prompt(),
        messages=history
    )

    assistant_message = response.content[0].text

    # Guardar respuesta en historial
    conversation_history[user_phone].append({
        "role": "assistant",
        "content": assistant_message
    })

    # Procesar acciones especiales (reservaciones, escalamiento)
    return process_agent_response(assistant_message, user_phone)


def reset_conversation(user_phone: str):
    """Reinicia la conversación de un usuario."""
    if user_phone in conversation_history:
        del conversation_history[user_phone]
