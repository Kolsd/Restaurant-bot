# ─────────────────────────────────────────────
# VARIABLES DE ENTORNO - Copiar a .env y llenar
# ─────────────────────────────────────────────

# API Key de Anthropic (OBLIGATORIO)
# Obtener en: https://console.anthropic.com
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxx

# Token secreto para verificar webhook de Meta
META_VERIFY_TOKEN=MI_TOKEN_SECRETO

# Token de acceso de Meta Cloud API (para enviar mensajes)
META_ACCESS_TOKEN=

# Twilio (alternativa a Meta)
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886

# Wompi — https://dashboard.wompi.co/developers
WOMPI_PUBLIC_KEY=pub_test_xxxxxxxxxxxx
WOMPI_PRIVATE_KEY=prv_test_xxxxxxxxxxxx
WOMPI_INTEGRITY_SECRET=tu_secreto_integridad
WOMPI_EVENTS_SECRET=tu_secreto_eventos
