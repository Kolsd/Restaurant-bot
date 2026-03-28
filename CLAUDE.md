# Mesio Restaurant Bot — v6.1

## Entorno y Comandos

```
Server:  uvicorn app.main:app --reload --port 8000
Tests:   pytest | pytest tests/test_file.py -v
Deploy:  Railway — alembic upgrade head && uvicorn ... --workers 4 --loop uvloop
```

Variables de entorno críticas: `DATABASE_URL`, `ANTHROPIC_API_KEY`, `META_APP_SECRET`, `ADMIN_KEY`, `META_ACCESS_TOKEN`, `WOMPI_PUBLIC_KEY`, `WOMPI_INTEGRITY_SECRET`

---

## Estructura del Proyecto

```
Restaurant-bot/
├── app/
│   ├── main.py                    # FastAPI entry point, monta rutas y static
│   ├── routes/                    # Capa HTTP — solo validación y respuesta
│   │   ├── deps.py                # Dependencias compartidas: auth, get_current_restaurant
│   │   ├── chat.py                # Webhook Meta + procesamiento de mensajes WA
│   │   ├── dashboard.py           # Settings, auth, páginas HTML, team, branches
│   │   ├── stats.py               # Métricas, conversaciones, gráficas
│   │   ├── tables.py              # POS, órdenes de mesa, caja, NPS
│   │   ├── orders.py              # Endpoints de órdenes externas (domicilio/recoger)
│   │   ├── billing.py             # DIAN, facturación electrónica
│   │   ├── staff.py               # Personal, turnos, propinas
│   │   ├── crm.py                 # Clientes, prospectos, campañas
│   │   ├── inventory.py           # Inventario y recetas
│   │   ├── loyalty.py             # Programa de fidelización
│   │   ├── nps.py                 # Endpoints NPS
│   │   └── sync.py                # Sincronización offline
│   ├── services/                  # Capa de negocio — lógica y orquestación
│   │   ├── database.py            # TODO el SQL. asyncpg puro. Cero ORMs
│   │   ├── agent.py               # IA (Claude), detect_table_context, NPS flow
│   │   ├── auth.py                # JWT tokens 72h, bcrypt passwords
│   │   ├── billing.py             # Adaptadores DIAN (Mesio Native, Alegra, etc.)
│   │   ├── orders.py              # Lógica de carrito y creación de órdenes
│   │   ├── loyalty.py             # Acumulación y canje de puntos
│   │   └── scheduler.py           # Jobs de background (inactividad, NPS timeout)
│   └── static/
│       ├── html/                  # Todas las páginas HTML
│       │   ├── dashboard.html     # Panel principal owner/admin/gerente
│       │   ├── mesero.html        # POS de salón
│       │   ├── caja.html          # Cierre de cuentas y cobro
│       │   ├── kitchen.html       # Pantalla de cocina
│       │   ├── bar.html           # Pantalla de bar
│       │   ├── domiciliario.html  # Vista del repartidor
│       │   ├── billing.html       # Gestión de facturas
│       │   ├── settings.html      # Configuración del restaurante/sucursal
│       │   ├── staff-portal.html  # Portal de fichaje del personal
│       │   ├── catalog.html       # Catálogo público del menú
│       │   ├── menu.html          # Vista de mesa (QR)
│       │   ├── crm.html           # CRM de clientes
│       │   ├── login.html         # Login owner/staff
│       │   ├── landing.html       # Landing pública de Mesio
│       │   └── ...otros           # superadmin, demo-chat, privacidad, terminos, etc.
│       ├── js/                    # JavaScript separado
│       │   ├── dashboard-core.js
│       │   ├── dashboard-components.js
│       │   ├── dashboard-features.js
│       │   ├── dashboard-nps-inventory.js
│       │   ├── crm.js
│       │   ├── landing.js
│       │   ├── offline-sync.js
│       │   ├── roles.js
│       │   └── sw.js              # Service Worker
│       ├── css/
│       │   ├── dashboard.css
│       │   ├── crm.css
│       │   └── landing.css
│       └── img/
│           └── logo.png
├── alembic/                       # Migraciones de base de datos (versioning)
│   └── versions/                  # 0001_initial_schema → 0004_staff_roles
├── scripts/                       # Utilidades y scripts de mantenimiento
│   ├── run_sandbox_invoice.py     # Testing de facturación
│   └── db/                        # Migraciones manuales de datos
│       ├── billing_migration.py
│       └── crm_migrations.py
├── tests/                         # Suite de pruebas pytest
├── requirements.txt
├── railway.toml                   # Deploy config: 4 workers, uvloop
└── alembic.ini
```

---

## Arquitectura de Capas (Estricta)

| Capa | Ubicación | Responsabilidad |
|------|-----------|-----------------|
| **Routes** | `app/routes/` | Validación HTTP, autenticación, retorno de respuesta. **Sin SQL, sin lógica de negocio.** |
| **Services** | `app/services/` | Orquestación, Claude AI, Wompi, lógica compleja. Retorna `dict/list`. |
| **Database** | `app/services/database.py` | Único archivo con SQL. `asyncpg` puro. Pool máx 20 conexiones. |
| **Frontend** | `app/static/` | Vanilla JS. Sin frameworks. Llama a la API REST. |

---

## Multi-tenancy y Sucursales

- **Identificación**: `bot_number` = `restaurants.whatsapp_number`
- **Jerarquía**: `restaurants.parent_restaurant_id` — `NULL` = casa matriz, `NOT NULL` = sucursal
- **`db_get_all_restaurants()`**: devuelve SOLO matrices (`parent_restaurant_id IS NULL`)
- **`db_get_restaurant_by_bot_number(n)`**: busca en TODOS (matriz + sucursales)
- **Resolución de contexto** (`deps.py → get_current_restaurant`):
  1. Staff de sucursal → su propia sucursal
  2. Owner con header `X-Branch-ID` → sucursal indicada
  3. Owner sin header → casa matriz
- **Conversaciones**: siempre almacenadas con el `bot_number` del WA físico (normalmente la matriz). Usar `_get_effective_bot_number()` en stats.py para filtrar correctamente.
- **branch_id en NULL**: `WHERE branch_id = $1` con `$1 = None` NUNCA hace match en PostgreSQL. Siempre usar lógica de 3 casos o `IS NULL`/`IS NOT NULL`.

---

## Flujo de Mesa (Salón)

```
QR scan → /menu?t={table_id} → WA pre-fill con [t:{table_id}]
  → bot detecta [t:...] → crea table_session (status=active)
  → mesero POS → table_orders (recibido → en_preparacion → listo → entregado)
  → mesero "Generar Factura" → status=factura_generada + WA al cliente
  → caja crea table_checks → paga checks
  → último check pagado → status=factura_entregada
  → _farewell_and_nps() → WA despedida + NPS enviado
  → session status=nps_pending (mesa sigue ocupada)
  → cliente califica NPS → session status=closed (mesa libre)
  → sin respuesta 10min → db_get_closeable_sessions() auto-cierra
```

**Nota**: El tag `[t:table_id]` en el pre-fill WA se elimina de `user_message_clean` en `agent.py` antes de pasar al LLM o guardar en historial.

---

## Flujo de Domicilio (Delivery)

```
Cliente envía ubicación GPS → lat/lon parseado en webhook (chat.py)
  → execute_action("delivery") en agent.py
  → db_find_nearest_branch(lat, lon, parent_id) — Haversine formula en SQL
  → sucursal dentro de delivery_radius_km → bot_number = branch.whatsapp_number
  → create_order(..., bot_number=branch_bot) → orden asociada a sucursal
  → sin cobertura → mensaje "fuera de cobertura"
```

Cobertura configurada en `features.delivery_radius_km` (default 5km) por sucursal.

---

## Reglas de Seguridad

- **SQL**: PROHIBIDO f-strings. Siempre parámetros posicionales (`$1, $2, ...`)
- **Auth**: JWT 72h. Passwords bcrypt. Usuarios (email/pass) vs Staff (nombre/PIN)
- **XSS**: En JS usar `textContent`, nunca `innerHTML`
- **JSONB**: Se auto-codifica en asyncpg. No serializar manualmente antes de insertar
- **NULL en SQL**: `WHERE col = NULL` → nunca hace match. Usar `IS NULL` o `IS NOT NULL`

---

## Staff y Operaciones

- **Roles**: `owner`, `admin`, `gerente`, `mesero`, `caja`, `cocina`, `bar`, `domiciliario`
- **Turnos**: una sola fila activa por staff (`clock_out IS NULL`, índice único)
- **Propinas**: 10% voluntario en `table_checks.tip_amount`. No gravable para DIAN
- **Reparto**: pool por período, distribuido % por rol

---

## Frontend

- **Assets**: `/static/html/`, `/static/js/`, `/static/css/`, `/static/img/`
- **Moneda**: `Intl.NumberFormat` con `rb_restaurant` de localStorage
- **Branch context**: header `X-Branch-ID` para owner que accede a una sucursal
- **Service Worker**: `sw.js` para caché offline (rutas de operación crítica)

---

## Instrucciones para Claude Code

- NO leas la carpeta `tests/` ni cachés
- NO expliques los cambios realizados
- Ve directo al archivo mencionado
- NO generes código duplicado — siempre verifica si la función ya existe en `database.py`
- Ante SQL con branch_id nullable, usa siempre lógica de 3 casos explícita
- Valida que el servidor arranque después de modificar rutas o modelos
