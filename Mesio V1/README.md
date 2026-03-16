# 🍽️ Restaurant AI Bot

Agente de WhatsApp con IA para restaurantes. Responde preguntas del menú, toma reservaciones, da recomendaciones y escala a humano cuando es necesario.

## ✨ Funciones

- 🍕 **Menú completo** - Precios, ingredientes, opciones vegetarianas
- 📅 **Reservaciones** - Toma datos y confirma automáticamente
- ⏰ **Horarios y ubicación**
- 🔥 **Recomendaciones** - Basadas en platos más pedidos
- 👤 **Escalar a humano** - Cuando no puede resolver algo
- 💬 **Memoria de conversación** - Recuerda el contexto del chat

---

## 🚀 Instalación

### 1. Clonar e instalar dependencias

```bash
cd restaurant-bot
pip install -r requirements.txt
```

### 2. Configurar variables de entorno

```bash
cp .env.example .env
# Editar .env con tu ANTHROPIC_API_KEY
```

### 3. Personalizar el restaurante

Editar `app/data/restaurant.py` con:
- Nombre, dirección, teléfono del restaurante
- Horarios reales
- Menú completo con precios

### 4. Ejecutar el servidor

```bash
uvicorn app.main:app --reload --port 8000
```

El bot estará en: `http://localhost:8000`
Documentación interactiva: `http://localhost:8000/docs`

---

## 🧪 Probar el bot localmente

```bash
# Enviar un mensaje de prueba
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"phone": "+5215512345678", "message": "Hola, ¿qué platos recomiendan?"}'
```

---

## 📱 Conectar WhatsApp Real

### Opción A: Twilio (más fácil para empezar)

1. Crear cuenta en [twilio.com](https://twilio.com)
2. Activar WhatsApp Sandbox
3. Configurar webhook: `https://tu-dominio.com/api/webhook/twilio`
4. Agregar credenciales en `.env`

### Opción B: Meta Cloud API (producción)

1. Crear app en [developers.facebook.com](https://developers.facebook.com)
2. Agregar producto "WhatsApp"
3. Configurar webhook: `https://tu-dominio.com/api/webhook/meta`
4. Token de verificación: `MI_TOKEN_SECRETO` (cambiar en `.env`)

---

## ☁️ Deploy en producción

### Railway (recomendado, ~$5/mes)

```bash
npm install -g @railway/cli
railway login
railway init
railway up
```

### Variables de entorno en Railway:
- `ANTHROPIC_API_KEY` = tu API key

---

## 📊 Endpoints disponibles

| Método | URL | Descripción |
|--------|-----|-------------|
| POST | `/api/chat` | Chat directo (testing) |
| POST | `/api/webhook/twilio` | Webhook de Twilio |
| POST | `/api/webhook/meta` | Webhook de Meta |
| GET  | `/api/webhook/meta` | Verificación de Meta |
| GET  | `/api/reservations` | Ver reservaciones |
| POST | `/api/reset` | Reiniciar conversación |

---

## 💰 Costos estimados

| Servicio | Costo |
|----------|-------|
| Anthropic API | ~$5-15/mes por restaurante |
| Railway (servidor) | $5/mes |
| Twilio WhatsApp | $0.005 por mensaje |
| **Total** | **~$15-25/mes** |

Cobrando $99-149/mes por restaurante = **$74-124 de ganancia por cliente** 🚀

---

## 🗺️ Roadmap

- [ ] Dashboard web para ver conversaciones y reservaciones
- [ ] Integración con Google Calendar para reservaciones
- [ ] Notificaciones por email al dueño
- [ ] Base de datos PostgreSQL (reemplazar memoria)
- [ ] Panel de configuración del restaurante sin código
- [ ] Soporte multi-idioma
