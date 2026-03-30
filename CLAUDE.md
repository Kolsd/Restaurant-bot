# Mesio Restaurant Bot — v6.2 (Arquitectura Multi-Sucursal y Flujo Asíncrono)

## Entorno y Comandos

```bash
Server:  uvicorn app.main:app --reload --port 8000
Tests:   pytest | pytest tests/test_file.py -v
Deploy:  Railway — alembic upgrade head && uvicorn ... --workers 4 --loop uvloop
Variables de entorno críticas: DATABASE_URL, ANTHROPIC_API_KEY, META_APP_SECRET, ADMIN_KEY, META_ACCESS_TOKEN, WOMPI_PUBLIC_KEY, WOMPI_INTEGRITY_SECRET

Estructura del Proyecto
Plaintext
Restaurant-bot/
├── app/
│   ├── main.py                    # FastAPI entry point, monta rutas y static
│   ├── routes/                    # Capa HTTP — solo validación y respuesta
│   │   ├── deps.py                # Dependencias compartidas: auth, get_current_restaurant
│   │   ├── chat.py                # Webhook Meta, procesamiento de mensajes WA, y Proxy de Imágenes (/api/media)
│   │   ├── dashboard.py           # Settings, auth, páginas HTML, team, branches
│   │   ├── stats.py               # Métricas, conversaciones, gráficas
│   │   ├── tables.py              # POS, órdenes de mesa, split checks
│   │   ├── orders.py              # Endpoints de órdenes externas (domicilio/recoger) y Webhook Wompi
│   │   ├── billing.py             # DIAN, facturación electrónica
│   │   ├── staff.py               # Personal, turnos, propinas
│   │   ├── crm.py                 # Clientes, prospectos, campañas
│   │   ├── inventory.py           # Inventario, recetas (escandallos)
│   │   └── loyalty.py             # Sistema de puntos y recompensas
│   ├── services/                  # Lógica de negocio core
│   │   ├── database.py            # Queries asyncpg puros. (Sin ORM)
│   │   ├── agent.py               # Prompt engineering y llamadas a Anthropic (Claude)
│   │   ├── auth.py                # JWT y passwords
│   │   └── orders.py              # Lógica de carrito, creación de órdenes y pagos
│   └── static/                    # Frontend nativo (HTML/CSS/JS)
│       ├── html/                  # dashboard, caja, cocina, mesero, settings, catalog
│       ├── js/                    # dashboard-core.js, roles.js, sw.js
│       └── css/                   # dashboard.css, styles.css
Arquitectura de Base de Datos y Reglas Core
Tablas principales: restaurants, users, orders, table_orders, table_sessions, conversations, carts, staff, fiscal_invoices, inventory, dish_recipes.

Carts (carts): La columna cart_data es JSONB. Alimenta el carrito de compras. Para la geolocalización de domicilios, el bot inyecta silenciosamente las llaves "latitude" y "longitude" directamente en este JSONB al recibir el GPS de WhatsApp, asegurando un guardado a prueba de errores sin crear nuevas columnas SQL.

Conversations (conversations): Almacena el historial de chat y posee un campo branch_id. Cuando un pedido externo se triangula a una sucursal, el chat se reasigna a ese branch_id para que desaparezca de la Matriz y aparezca en la Caja de la sucursal.

Jerarquía de Sucursales: Las sucursales tienen parent_restaurant_id apuntando a la Casa Matriz. La Matriz tiene parent_restaurant_id IS NULL. Las sucursales heredan el número de WhatsApp usando un sufijo interno (_b[TIMESTAMP]) para evitar colisiones únicas, pero la atención fluye por el número principal.

Flujo Operativo de Domicilios y Pagos Asíncronos
El sistema implementa un flujo estricto y seguro para pedidos externos (Domicilio/Recoger) que evita que la cocina prepare pedidos falsos o no pagados:

Triangulación GPS: El cliente envía su dirección o Pin GPS. agent.py geocodifica (si es manual) y triangula matemáticamente la sucursal más cercana (radio default 5km).

Generación del Pedido: La orden se crea en la base de datos con estado pendiente.

Instrucciones de Pago: Si el cliente paga por transferencia (Nequi, Daviplata), la IA responde con las payment_instructions del JSON de features y pide el comprobante de pago (📸).

Proxy de Comprobantes: Las imágenes de Meta llegan encriptadas. chat.py tiene un proxy en /api/media/{media_id} que las descarga usando el Token y las sirve al Frontend usando follow_redirects=True.

Validación en "Súper Caja": El Cajero abre la pestaña "WhatsApp" en caja.html, verifica el comprobante visualmente, y luego va a la pestaña "Domicilios" y presiona "✅ Confirmar".

Despacho a Cocina: Al confirmar, el estado cambia a confirmado, y el pedido aparece instantáneamente en el KDS (kitchen.html) de la sucursal asignada.

Reglas de Seguridad
SQL: PROHIBIDO f-strings para inyectar datos en queries. Siempre parámetros posicionales ($1, $2, ...)

Auth: JWT 72h. Passwords bcrypt. Usuarios (email/pass) vs Staff (nombre/PIN).

XSS: En JS usar textContent, nunca innerHTML al mostrar datos ingresados por usuarios.

JSONB: Se auto-codifica en asyncpg. No serializar manualmente con json.dumps() si la conexión ya tiene el codec configurado (excepto donde el driver lo requiera explícitamente).

NULL en SQL: Usar IS NULL o IS NOT NULL. Nunca WHERE col = NULL.

Staff, POS y Operaciones
Roles: owner, admin, gerente, mesero, caja, cocina, bar, domiciliario.

Caja (Súper Caja): Concentra 3 vistas: Mesas (POS local), Domicilios Pendientes, y Chats (para validar comprobantes).

Split Checks: Fase 5. table_checks permite pagos mixtos y dividir cuentas. Si la caja cierra un check, se emite una factura individual. La mesa completa cambia a factura_entregada cuando todos sus checks están pagos.

Turnos: Una sola fila activa por staff (clock_out IS NULL).

Propinas: 10% voluntario en table_checks.tip_amount. No gravable para DIAN. Pool por período, distribuido % por rol.

Frontend
Assets: /static/html/, /static/js/, /static/css/, /static/img/

Moneda y Locale: Formateador Universal Inteligente (Intl.NumberFormat) que lee locale y currency desde rb_restaurant en localStorage. Soporta monedas sin decimales (ej. COP, CLP).

Contexto Multi-Sucursal: Para dueños/admins, el header X-Branch-ID dicta qué datos leer. Si el valor es "all", los endpoints (ej. dashboard.py) deben implementar cláusulas SQL para retornar datos de la Matriz + Sucursales (parent_restaurant_id).

Service Worker: sw.js para caché offline en operaciones críticas de salón y cocina.

Instrucciones para Claude Code
NO leas la carpeta tests/ ni cachés, a menos que se te pida explícitamente.

NO expliques en exceso los cambios realizados, ve al grano.

Al modificar flujos asíncronos o de concurrencia, cuida siempre los Diccionarios de Locks (_cart_locks en orders.py) para evitar Race Conditions con los múltiples workers del servidor en producción.