# 🍽️ Proyecto Mesio - Mapa de Arquitectura para IA

**Descripción del Proyecto:** Mesio es un bot y plataforma backend basada en Inteligencia Artificial para la gestión de restaurantes en Colombia. Maneja toma de pedidos por WhatsApp, atención al cliente con IA, facturación electrónica, sistema CRM, reservas y control de mesas.

**Stack Tecnológico:** Python, FastAPI, PostgreSQL (asyncpg), Integración con Meta (WhatsApp API) / Twilio, Pasarela Wompi. No se usa ORM (SQL directo con asyncpg).

**Versión actual:** Mesio v5.9

---

## 📂 Estructura de Directorios y Archivos Principales

### 1. Raíz del Proyecto
- `requirements.txt` — Dependencias de Python.
- `railway.toml` / `Procfile` — Configuración de despliegue en Railway/Heroku.

---

### 2. `app/` (Código Principal)

#### `main.py`
Punto de entrada de la aplicación. Configura FastAPI, Middlewares, CORS, inicializa la base de datos al arranque y monta todos los routers.

**Imports críticos (orden correcto):**
```python
from app.services import database as db
from app.routes import chat, crm, dashboard, billing, orders, tables, stats, nps, inventory
```

**Startup sequence:**
1. `db.init_db()` — tablas principales
2. `db.db_init_nps_inventory()` — tablas NPS e Inventario
3. Scheduler de tareas en background

**Logs de arranque correcto:**
```
✅ Tablas NPS e Inventario listas
🚀 Mesio v5.9 iniciado
```

---

### 2.1 `app/routes/` (Endpoints y Controladores)

| Archivo | Descripción |
|---|---|
| `chat.py` | Webhooks de Meta y Twilio. Rate limiting, verificación de firmas, respuesta IA en background. |
| `crm.py` | CRM de ventas/prospectos. CRUD, notas, CSV, kanban, envíos masivos WhatsApp Templates. |
| `dashboard.py` | Login, creación de restaurantes/sucursales, gestión de equipo, renderizado de vistas HTML, procesamiento de menús PDF/Imagen. |
| `billing.py` | Credenciales y emisión de facturas electrónicas (Siigo, Alegra, Loggro). |
| `orders.py` | Gestión de pedidos, carritos, webhook Wompi para confirmación de pagos. |
| `tables.py` | Pedidos en mesa (flujo presencial POS con meseros). |
| `stats.py` | Estadísticas y métricas del restaurante (ingresos, pedidos, conversaciones). |
| `nps.py` ⭐ **NUEVO** | Sistema de Net Promoter Score vía WhatsApp. Ver detalle abajo. |
| `inventory.py` ⭐ **NUEVO** | Control de inventario vinculado al menú. Ver detalle abajo. |

---

#### `app/routes/nps.py` ⭐ NUEVO

Sistema de satisfacción del cliente disparado automáticamente al cerrar mesa.

**Endpoints:**
| Método | Ruta | Descripción |
|---|---|---|
| `POST` | `/api/nps/response` | Recibe respuesta del cliente (score 1-5 + comentario) vía webhook WA |
| `GET` | `/api/nps/stats` | Estadísticas NPS del período (promotores, detractores, score, distribución) |
| `GET` | `/api/nps/responses` | Lista de respuestas recientes paginadas |
| `GET` | `/api/nps/google-maps-url` | Obtiene el link de Google Maps configurado |
| `POST` | `/api/nps/google-maps-url` | Guarda/actualiza el link de Google Maps |

**Flujo NPS:**
1. Mesa se cierra → `trigger_nps()` → cliente recibe pregunta 1-5 por WhatsApp
2. Score 1-3 → bot pide comentario adicional
3. Score 4-5 → bot envía enlace de Google Maps para dejar reseña

**Estado en producción:** `/api/nps/responses` → 200 ✅ | `/api/nps/stats` → pendiente fix GroupingError SQL

---

#### `app/routes/inventory.py` ⭐ NUEVO

Control de stock vinculado a platos del menú. Al llegar a 0, el plato se desactiva automáticamente.

**Endpoints:**
| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/api/inventory` | Lista todos los productos del inventario |
| `POST` | `/api/inventory` | Crea nuevo producto de inventario |
| `PUT` | `/api/inventory/{id}` | Edita nombre, unidad, stock mínimo, costo, platos vinculados |
| `DELETE` | `/api/inventory/{id}` | Elimina producto |
| `POST` | `/api/inventory/{id}/adjust` | Ajusta stock (compra, merma, ajuste manual) |
| `GET` | `/api/inventory/{id}/history` | Historial de movimientos de un producto |
| `GET` | `/api/inventory/alerts` | Productos con stock bajo o agotado |
| `GET` | `/api/inventory/menu-items` | Lista de platos del menú para vincular al inventario |

**Estado en producción:** `/api/inventory` → 200 ✅ | `/api/inventory/alerts` → 200 ✅

---

### 2.2 `app/services/` (Lógica de Negocio y Base de Datos)

| Archivo | Descripción |
|---|---|
| `database.py` | **Archivo crucial.** Pool asyncpg + TODAS las consultas SQL. Ver sección de funciones abajo. |
| `agent.py` | Conecta con Anthropic/Claude u OpenAI para procesar lenguaje natural y generar respuestas del bot. |
| `auth.py` | Hashing de contraseñas, creación y verificación de tokens JWT. |
| `billing.py` | Clientes HTTP para Siigo, Alegra y Loggro. |
| `orders.py` | Funciones auxiliares: cálculo de subtotales de carritos, limpieza. |
| `scheduler.py` | Tareas recurrentes en background (limpieza de DB, recordatorios automáticos). |

---

#### `app/services/database.py` — Funciones SQL

**Tablas gestionadas:** `restaurants`, `orders`, `tables`, `reservations`, `users`, `conversations`, `sessions`, `billing_config`, `nps_responses` ⭐, `inventory` ⭐, `inventory_history` ⭐

##### Funciones NPS ⭐ NUEVAS (añadir después de la función existente `db_init_nps_inventory` — ⚠️ existe duplicado en línea ~931 que debe eliminarse):

| Función | Descripción |
|---|---|
| `db_init_nps_inventory()` | Crea tablas `nps_responses`, `inventory`, `inventory_history` si no existen |
| `db_save_nps_response(phone, bot_number, score, comment)` | Guarda respuesta NPS de un cliente |
| `db_get_nps_stats(bot_number, period)` | Devuelve total, promotores, detractores, NPS score, avg_score, distribución por score |
| `db_get_nps_responses(bot_number, limit)` | Lista respuestas recientes |
| `db_get_google_maps_url(bot_number)` | Obtiene URL de Google Maps guardada |
| `db_set_google_maps_url(bot_number, url)` | Guarda URL de Google Maps (columna directa, no JSONB) |

**⚠️ Bug conocido — `db_get_nps_stats`:** La query original tenía `SELECT score, COUNT(*), comment` sin `GROUP BY score` → `asyncpg.exceptions.GroupingError`. Fix correcto:
```python
rows = await conn.fetch(
    f"""SELECT score, COUNT(*) AS count
        FROM nps_responses
        WHERE bot_number = $1
          AND created_at >= NOW() - INTERVAL '{interval}'
        GROUP BY score
        ORDER BY score""",
    bot_number
)
```

##### Funciones Inventario ⭐ NUEVAS:

| Función | Descripción |
|---|---|
| `db_get_inventory(restaurant_id)` | Lista productos con stock actual, mínimo, alertas |
| `db_create_inventory_item(restaurant_id, name, unit, current_stock, min_stock, linked_dishes, cost_per_unit)` | Crea producto |
| `db_update_inventory_item(item_id, ...)` | Edita producto (nombre, unidad, stock mínimo, costo, platos vinculados) |
| `db_delete_inventory_item(item_id)` | Elimina producto |
| `db_adjust_inventory_stock(item_id, quantity_delta, reason)` | Ajusta stock y registra en historial |
| `db_get_inventory_history(item_id, limit)` | Historial de movimientos de un producto |
| `db_get_inventory_alerts(restaurant_id)` | Productos con `current_stock <= min_stock` |
| `db_deduct_inventory_for_order(restaurant_id, items)` | Al confirmar orden: descuenta stock de ingredientes vinculados |
| `_sync_dish_availability(conn, restaurant_id)` | Interna: desactiva platos cuando su ingrediente llega a 0 |

##### Funciones existentes con bugs conocidos:

| Función | Bug | Estado |
|---|---|---|
| `db_get_all_conversations` | Tenía `OR bot_number=''` trayendo conversaciones huérfanas | ✅ Fix aplicado |
| `db_init_nps_inventory` | Definida dos veces (línea ~179 y ~931) | ⚠️ Pendiente eliminar duplicado |
| Migrations array | Falta coma entre `billing_config` y `created_at` | ⚠️ Pendiente fix |

##### Schema de tablas nuevas:
```sql
CREATE TABLE nps_responses (
    id          SERIAL PRIMARY KEY,
    phone       TEXT,
    bot_number  TEXT DEFAULT '',
    score       INTEGER CHECK (score BETWEEN 1 AND 5),
    comment     TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE TABLE inventory (
    id              SERIAL PRIMARY KEY,
    restaurant_id   INTEGER,
    name            TEXT,
    unit            TEXT DEFAULT 'unidades',
    current_stock   NUMERIC(10,2),
    min_stock       NUMERIC(10,2),
    linked_dishes   JSONB DEFAULT '[]',
    cost_per_unit   NUMERIC(10,2),
    created_at      TIMESTAMP,
    updated_at      TIMESTAMP
);

CREATE TABLE inventory_history (
    id              SERIAL PRIMARY KEY,
    inventory_id    INTEGER,
    quantity_delta  NUMERIC(10,2),
    stock_after     NUMERIC(10,2),
    reason          TEXT DEFAULT 'ajuste_manual',
    created_at      TIMESTAMP
);
```

---

### 2.3 `app/static/` (Frontend)

| Archivo | Descripción |
|---|---|
| `dashboard.html` | Panel principal del cliente. Estructura HTML maestra con todas las secciones. |
| `dashboard.css` | Estilos globales del dashboard cliente. |
| `dashboard-core.js?v=3` | Lógica principal: `showSection()`, `setPeriod()`, `renderChart()`, navegación, KPIs. |
| `dashboard-features.js?v=3` | Features secundarios: pedidos, mesas, menú, equipo, sesiones, conversaciones. |
| `dashboard-nps-inventory.js?v=2` | ⭐ **NUEVO** Lógica de secciones NPS e Inventario. Ver detalle abajo. |
| `superadmin.html` | ⭐ **RENOVADO** Panel de control interno Mesio HQ. Ver detalle abajo. |
| `crm.html` | Dashboard del CRM de prospectos. |
| `mesero.html` | Vista del mesero (POS presencial). |
| `caja.html` | Vista de caja/cajero. |
| `billing.html` | Vista de facturación electrónica. |
| `login.html` | Pantalla de login. |

---

#### `app/static/dashboard.html` — Estructura de secciones

**Secciones dentro de `<div class="main">`** (en este orden):
1. `#resumen` — KPIs + gráficos de ingresos
2. `#pedidos` — Monitor RT (domicilios + salón) + Histórico
3. `#reservaciones`
4. `#conversaciones`
5. `#menu`
6. `#pos` — POS con IA Analytics
7. `#mesas` — Mesas y QR
8. `#equipo` — Sucursales y multirol
9. `#sesiones` — Auditoría de sesiones
10. `#nps` ⭐ — NPS satisfacción cliente
11. `#inventario` ⭐ — Control de inventario

**Modales fuera de `.main`** (orden crítico — NO anidar):
```html
</div><!-- /main -->
<div class="chat-modal-overlay" ...></div>   <!-- Cierre obligatorio antes del siguiente -->
<div class="ses-modal-overlay" ...></div>
<div id="modal-inv-create" ...></div>        <!-- z-index: 2000 -->
<div id="modal-inv-edit" ...></div>
<div id="modal-inv-adjust" ...></div>
<div id="modal-inv-history" ...></div>
```

**⚠️ Bug histórico resuelto:** El `chat-modal-overlay` no tenía `</div>` de cierre, dejando los modales de inventario anidados dentro → botón "Agregar producto" no hacía nada. Fix: cerrar correctamente el overlay antes del siguiente.

---

#### `app/static/dashboard-core.js` — Notas importantes

**`showSection(id, btn)`:** Oculta/muestra secciones. Controla `period-bar` según sección activa.

**Secciones sin period-bar** (`hidePeriod` array — debe incluir):
```javascript
const hidePeriod = ['conversaciones', 'menu', 'equipo', 'sesiones', 'mesas', 'nps', 'inventario'];
```

**`renderChart()`:** Gráfico de ingresos + pedidos. Fix de orden de datasets para que la línea azul quede sobre las barras:
```javascript
datasets: [
  { label:'Pedidos', data:countData, type:'line', ..., order:1 },  // order menor = encima
  { label:'Ingresos', data:revData, type:'bar', ..., order:2 }
]
```

---

#### `app/static/dashboard-nps-inventory.js` ⭐ NUEVO (`?v=2`)

Maneja las secciones `#nps` e `#inventario` del dashboard cliente.

**Funciones NPS:**
| Función | Descripción |
|---|---|
| `loadNPS()` | Carga stats + respuestas + URL de Google Maps |
| `setNPSPeriod(period, btn)` | Cambia período y recarga |
| `renderNPSScore(stats)` | Renderiza la tarjeta del score con color según rango |
| `renderNPSChart(stats)` | Gráfico de barras con distribución de puntuaciones |
| `renderNPSResponses(responses)` | Tabla de respuestas recientes con estrellas |
| `saveGoogleMapsURL()` | Guarda URL de Google Maps vía `POST /api/nps/google-maps-url` |

**Funciones Inventario:**
| Función | Descripción |
|---|---|
| `loadInventory()` | Carga inventario + alertas + platos del menú |
| `filterInventory()` | Filtra tabla por texto del buscador |
| `openCreateInventoryModal()` | Abre modal de creación (muestra platos para vincular) |
| `submitCreateInventory()` | Envía `POST /api/inventory` |
| `openEditModal(item)` | Abre modal de edición con datos precargados |
| `submitEditInventory()` | Envía `PUT /api/inventory/{id}` |
| `openAdjustModal(item)` | Abre modal de ajuste de stock |
| `submitAdjustStock()` | Envía `POST /api/inventory/{id}/adjust` |
| `openHistoryModal(item)` | Abre modal de historial de movimientos |
| `closeCreateInventoryModal()` / `closeEditModal()` / `closeAdjustModal()` / `closeHistoryModal()` | Cierran sus respectivos modales |

**Bloque Init (IIFE — patrón correcto):**
```javascript
(function() {
  const origShowSection = window.showSection;
  window.showSection = function(id, btn) {
    if (typeof origShowSection === 'function') origShowSection(id, btn);
    if (id === 'nps')        loadNPS();
    if (id === 'inventario') loadInventory();
  };
  document.addEventListener('DOMContentLoaded', () => {
    const active = document.querySelector('.section.active');
    if (active) {
      if (active.id === 'nps')        loadNPS();
      if (active.id === 'inventario') loadInventory();
    }
  });
})();
```

---

#### `app/static/superadmin.html` ⭐ RENOVADO COMPLETO

Panel de control interno de Mesio HQ. Reemplaza la versión anterior minimalista.

**Diseño:** Dark theme, tipografía Syne + Inter + DM Mono, sidebar expandible al hover, sin gradientes, noise texture sutil.

**Secciones:**
| Tab | Contenido |
|---|---|
| Dashboard Global | 6 KPIs, gráfico de crecimiento clientes, donut de suscripciones, MRR por plan, pipeline de ventas, health scores A/B/C/D, actividad reciente, checklist operacional diario |
| Directorio Clientes | Tabla con búsqueda live, filtros (todos/activos/suspendidos), modal de detalle por cliente, acciones suspender/activar |
| Alta de Cliente | Formulario 3 pasos: datos básicos, plan + módulos (incluye NPS e Inventario), menú JSON con extracción IA |
| Usuarios & Accesos | Crear acceso admin + lista de accesos recientes actualizable |
| Finanzas & MRR | ARR proyectado, churn rate, LTV, gráfico histórico 12 meses, tabla desglose por cliente |
| Operaciones | Estado instancias WA, gráfico de actividad 24h, log de errores |

**API calls que usa:**
- `GET /api/admin/stats?admin_key=KEY`
- `GET /api/admin/restaurants?admin_key=KEY`
- `POST /api/admin/set-subscription`
- `POST /api/admin/create-restaurant`
- `POST /api/admin/create-user`
- `POST /api/admin/parse-menu?admin_key=KEY`

**Auth:** La ADMIN_KEY se guarda en `localStorage` para auto-restore al recargar.

---

### 2.4 Directorios Auxiliares

| Directorio | Descripción |
|---|---|
| `app/migrations/` | Scripts puntuales para añadir columnas/tablas. Incluye `crm_migrations.py`. |
| `tests/` | Pruebas automatizadas (`conftest.py`, `test_billing.py`, etc.). |

---

## 🔌 Estado de Endpoints en Producción

| Endpoint | Estado |
|---|---|
| `GET /api/inventory` | ✅ 200 OK |
| `GET /api/inventory/alerts` | ✅ 200 OK |
| `GET /api/nps/responses` | ✅ 200 OK |
| `GET /api/nps/stats` | ⚠️ 500 — GroupingError SQL (fix pendiente de deploy) |

---

## ⚠️ Pendientes Técnicos Conocidos

| Prioridad | Tarea | Archivo |
|---|---|---|
| 🔴 Alta | Fix `db_get_nps_stats` — agregar `GROUP BY score` | `database.py` |
| 🟡 Media | Eliminar segunda definición duplicada de `db_init_nps_inventory` (~línea 931) | `database.py` |
| 🟡 Media | Agregar coma faltante en `migrations[]` entre `billing_config` y `created_at` | `database.py` |
| 🟡 Media | Verificar `db_set_google_maps_url` usa columna directa (no JSONB) | `database.py` / `nps.py` |

---

## 🧠 Contexto para IA — Reglas de Negocio Importantes

- **Multi-restaurante:** Cada restaurante tiene su propio `bot_number` (número de WhatsApp). Todas las consultas deben filtrarse por `bot_number` o `restaurant_id`.
- **Sin ORM:** Todo SQL es raw con `asyncpg`. Nunca usar SQLAlchemy ni similares.
- **Autenticación:** JWT en dashboard cliente. ADMIN_KEY en header/query para superadmin.
- **Inventario → Menú:** Al llegar un ingrediente a stock 0, `_sync_dish_availability()` desactiva el plato automáticamente.
- **NPS trigger:** Se llama `trigger_nps()` al cerrar mesa, no al hacer un pedido.
- **Multirol:** Un usuario puede tener varios roles (admin, waiter, cashier, cook) en una misma sucursal.
- **Columna `created_at` en conversations:** Agregada vía migración (`ALTER TABLE conversations ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()`).