# 🍽️ Proyecto Mesio - Mapa de Arquitectura para IA

**Descripción del Proyecto:** Mesio es un bot y plataforma backend basada en Inteligencia Artificial para la gestión de restaurantes en Colombia. Maneja toma de pedidos por WhatsApp, atención al cliente con IA, facturación electrónica, sistema CRM, reservas y control de mesas.

**Stack Tecnológico:** Python, FastAPI, PostgreSQL (asyncpg), Integración con Meta (WhatsApp API). No se usa ORM (SQL directo con asyncpg). Frontend en Vanilla JS, HTML y CSS.

**Versión actual:** Mesio v6.0 (Zonas Horarias Universales & Funnel de Ventas Activo)

---

## 📂 Estructura de Archivos y Lógica Principal

### 1. `app/routes/` (Endpoints y Controladores)

| Archivo | Descripción y Lógica Crítica |
|---|---|
| `chat.py` | Recibe Webhooks de Meta. **Lógica vital:** Verifica firma `META_APP_SECRET`. Usa `get_dashboard_filters` (desempaqueta 4 valores). Separa el tráfico: si es `CRM_PHONE_NUMBER_ID` va al CRM, si es otro, va a `agent.py`. |
| `dashboard.py` | Endpoints del panel (`/api/dashboard/*`). **Lógica vital:** Usa `get_dashboard_filters(request, period, custom_start, custom_end, tz_offset)` para devolver `(branch_id, bot_number, start_date, end_date)`. Convierte zonas horarias dinámicamente y añade `Z` a las fechas. |
| `nps.py` | Sistema de Net Promoter Score. Flujo disparado al cerrar mesa. |
| `inventory.py` | Control de stock vinculado al menú. Desactiva platos si stock es 0. |

### 2. `app/services/` (Lógica de Negocio y Base de Datos)

| Archivo | Descripción y Lógica Crítica |
|---|---|
| `database.py` | Pool asyncpg. Todas las consultas SQL raw. Tablas principales: `restaurants`, `orders`, `tables`, `table_orders`, `conversations`, `sessions`. |
| `agent.py` | **Cerebro de Claude IA.** Contiene un **Embudo de Ventas Estricto** para clientes externos (Catálogo -> Modalidad -> Dirección -> Método de Pago -> Orden). Protege contra *órdenes fantasma* bloqueando la acción `order` si falta el tag `[MESA]`. Inyecta `[LINK_MENU]` y `[MÉTODOS_DE_PAGO]` dinámicos. |

### 3. `app/static/` (Frontend - Dashboard)

| Archivo | Descripción y Lógica Crítica |
|---|---|
| `dashboard.html` | UI principal. Dividido en **Tiempo Real** (Monitores de cocina/domicilios) e **Histórico Completo** (Tabla universal de pedidos con selector de fechas `custom`). |
| `dashboard-core.js` | Sincronización global (`refreshAll`). **Lógica vital:** Envía `new Date().getTimezoneOffset()` al backend. Renderiza gráficas y formatea la tabla histórica unificada leyendo el sufijo `Z` de las fechas. |
| `dashboard-features.js` | Funciones operativas. Maneja la vista "Mesas en Salón" (Monitor POS), control multirol de la sucursal y la tabla de sesiones de mesas inactivas. |

---

## 🧠 Contexto para IA — Reglas de Negocio Estrictas

1. **Zonas Horarias:** El Backend confía ciegamente en el `tz_offset` (minutos) que envía el Frontend para calcular el "Hoy" y los rangos de fechas (Local -> UTC -> Local).
2. **Desempaque en Dashboard:** Todas las consultas en `dashboard.py` o `chat.py` que usen filtros devuelven 4 variables: `_, bot_number, start_date, end_date = await get_dashboard_filters(...)`.
3. **Modos de la IA (`agent.py`):**
   * **Modo Salón:** Si existe `table_context`, inyecta `[MESA: X]`. La IA permite `"action": "order"`.
   * **Modo Externo:** Si no hay mesa, inyecta `[ALERTA: MESA NO DETECTADA]`. La IA bloquea `"order"` y obliga a usar el Embudo de Ventas (Pidiendo Dirección y Método de Pago) y terminando con `"action": "domicilio"` o `"recoger"`.
4. **Configuración del Restaurante:** Los métodos de pago, links de Google Maps y configuraciones del bot (activar/desactivar upsell) se guardan en la columna `features` (JSONB) de la tabla `restaurants`.