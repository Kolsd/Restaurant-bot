# Mesio Frontend-Backend Audit
**Date:** 2026-04-19  
**Scope:** 20 HTML pages + JS page files vs. FastAPI routes  
**Method:** Static analysis + fixes applied 2026-04-19  

---

## Legend

- ✅ Fully wired end-to-end
- 🟡 Read-only wired (endpoint exists, data renders via static HTML, no live rendering)
- 🔴 Placeholder / missing endpoint
- ⚠️ Broken wiring (wrong URL or wrong HTTP method)

---

## dashboard.html

**Purpose:** Admin summary dashboard with KPI metrics, live orders, revenue chart, and inventory alerts.  
**Auth:** owner / admin / gerente  
**JS file:** `app/static/js/pages/dashboard.js`

| Element | Type | Wiring status | Backend endpoint | Write path? | Notes |
|---|---|---|---|---|---|
| Period selector (Hoy/7d/Mes/…) | Action | ✅ | `GET /api/dashboard/sync?period=` | — | Refreshes all 8 data loads |
| AI insight banner | Panel | ✅ | `GET /api/stats/daily-insight` | — | Hidden if `enabled:false` |
| Metrics row (Ingresos / Pedidos / Ticket / Chats) | Metric | ✅ | `GET /api/dashboard/sync` | — | Delta % vs previous period |
| Revenue bar chart | Panel | ✅ | `GET /api/stats/by-channel?compare=true` + `GET /api/dashboard/sync` | — | Builds day-by-day bars |
| Ventas por canal card | Panel | ✅ | `GET /api/stats/by-channel` | — | |
| Pedidos en vivo list | Panel | ✅ | `GET /api/stats/live-orders?limit=5` | (writes via caja) | |
| Estado de pagos donut | Panel | ✅ | `GET /api/stats/payment-status` | — | |
| Top platos table | Panel | ✅ | `GET /api/stats/top-dishes` | — | `margin_pct` shown if available |
| Inventario crítico table | Panel | ✅ | `GET /api/stats/inventory-critical` | — | |
| Sidebar at-risk badge | Metric | ✅ | `GET /api/stats/customers-at-risk?limit=1` | — | |
| "Ver todos →" link to /caja | Nav | ✅ | (navigation) | — | |
| "Menu Engineering →" link | Nav | ✅ | (navigation) | — | |
| Logout button | Action | ✅ | `POST /api/auth/logout` via mesioLogout | — | |
| Bot 24/7 status indicator | Metric | 🟡 | (static, reads localStorage) | — | No live ping to bot health |
| 30s auto-refresh | Action | ✅ | (calls `_loadAll()` via mesioInterval) | — | |

---

## settings.html

**Purpose:** Admin configuration page for restaurant info, hours, payment methods, notifications, integrations, and danger zone.  
**Auth:** owner / admin  
**JS file:** `app/static/js/pages/settings.js`

| Element | Type | Wiring status | Backend endpoint | Write path? | Notes |
|---|---|---|---|---|---|
| Load config (GET) | Action | 🟡 | `GET /api/settings` | — | Returns `name`, `address`, `features`. Does NOT return `nit`, `city`, `cuisine_type`, `opening_hours`, `notifications` |
| Guardar cambios button | Action | ⚠️ | `PATCH /api/settings` | (writes) | Server only has `POST /api/settings`; HTTP method mismatch. Also server doesn't accept `name`, `nit`, `city`, `cuisine_type` fields |
| Horarios toggles + time inputs | Action | 🔴 | (no endpoint for opening_hours save) | — | Collected in `collectFormData()` under `features.opening_hours` but server `POST /api/settings` doesn't list `opening_hours` in updatable fields |
| Métodos de pago toggles | Action | ✅ | `POST /api/settings` (features.payment_methods) | (writes) | Method mismatch aside, `payment_methods` is in updatable list |
| Notificaciones toggles | 🔴 | (no endpoint) | — | Collected under `features.notifications` but server ignores it | |
| DIAN provider display | Panel | 🟡 | `GET /api/settings` (features.dian_provider) | via billing.html | Read-only display from features JSONB |
| DIAN range display | Panel | 🟡 | `GET /api/settings` (features.dian_numeracion) | via billing.html | |
| DIAN auto-invoice toggle | Action | 🔴 | (no write endpoint for this flag) | — | Collected but server doesn't persist it |
| Integrations section (Rappi, Alegra, Zapier, Google Calendar) | Panel | 🔴 | — | 🔴 MISSING — no integration endpoints | All labeled "Próximamente" |
| WhatsApp Business API status | Panel | 🟡 | (static badge "Activa") | — | No live status check |
| Bold "Conectar" button | Action | 🔴 | — | 🔴 MISSING | toast not even shown |
| Transferir propiedad button | Action | 🔴 | — | 🔴 MISSING | `mesioToast("Endpoint pendiente")` |
| Pausar restaurante button | Action | 🔴 | — | 🔴 MISSING | `mesioToast("Endpoint pendiente")` |
| Eliminar restaurante button | Action | 🔴 | — | 🔴 MISSING | `mesioToast("Endpoint pendiente")` |

---

## billing.html

**Purpose:** DIAN electronic invoicing configuration, provider selection, invoice log, and manual emission.  
**Auth:** owner / admin (session-based, `ADMIN_KEY` checked client-side)  
**JS file:** (inline script in billing.html — no separate JS page file)

| Element | Type | Wiring status | Backend endpoint | Write path? | Notes |
|---|---|---|---|---|---|
| Provider picker (Siigo/Alegra/Loggro) | Action | ✅ | `GET /api/billing/providers` | (writes via save) | |
| Guardar config button | Action | ✅ | `POST /api/billing/config` | (writes) | |
| Test connection button | Action | ✅ | `POST /api/billing/test-connection` | — | |
| Load billing config | Panel | ✅ | `GET /api/billing/config` | — | |
| Invoice log table | Panel | ✅ | `GET /api/billing/log?limit=200` | — | |
| Emit invoice modal | Modal | ✅ | `POST /api/billing/emit` | (writes) | |
| Reload log button | Action | ✅ | `GET /api/billing/log?limit=100` | — | |

---

## floorplan.html

**Purpose:** Live floor plan showing table status with zone filters; admin editor for layouts.  
**Auth:** owner / admin / gerente  
**JS file:** `app/static/js/pages/floorplan.js`

| Element | Type | Wiring status | Backend endpoint | Write path? | Notes |
|---|---|---|---|---|---|
| Floor plan tile render | Panel | ⚠️ | `GET /api/floor-plan` ← **WRONG URL** | — | Actual endpoint is `GET /api/tables/floor-plan`. JS uses wrong path — floor plan never loads |
| Zone filter buttons (Todas/Terraza/Salón/Privado) | Action | ✅ | (client-side filter on loaded data) | — | Wired but depends on floor-plan load |
| ↻ Sync button | Action | ✅ | (re-calls loadFloorPlan) | — | Calls same broken URL |
| Editar layout button | Action | 🔴 | — | 🔴 MISSING | HTML comment: "requires PATCH /api/floor-plan (v2)" |
| + Reserva button | Action | ✅ | (navigates to /reservaciones) | — | |
| Table detail panel (ticket, invoice, split, close) | Panel | 🟡 | (depends on floor-plan load) | via caja.html | Wired but blocked by URL bug |
| Auto-refresh every 15s | Action | ⚠️ | (calls broken /api/floor-plan) | — | |

---

## equipo.html

**Purpose:** Admin team overview with shift grid, tip distribution, and member table.  
**Auth:** owner / admin  
**JS file:** `app/static/js/pages/equipo.js`

| Element | Type | Wiring status | Backend endpoint | Write path? | Notes |
|---|---|---|---|---|---|
| Metrics row (Total equipo / En turno / Horas semana / Propinas / Costo laboral) | Metric | ✅ | `GET /api/staff`, `GET /api/staff/schedules`, `GET /api/stats/tips-pool` | — | Costo laboral is `—` placeholder |
| Shift grid 2-week view | Panel | ✅ | `GET /api/staff/schedules?week_start=` | — | Shows up to 6 staff rows |
| "+ Turno" button | Action | 🔴 | — | 🔴 MISSING | No handler attached; no endpoint |
| Semana / Día view toggle | Action | 🟡 | — | — | Client-side only, no day view implemented |
| Tip distribution card | Panel | ✅ | `GET /api/stats/tips-pool` | — | Shows `entries[]` with amounts |
| AI suggestion on tips | Panel | 🔴 | (shows `pool.ai_note` if present) | 🔴 MISSING — no write path | Backend `tips-pool` doesn't return `ai_note` |
| Reglas button | Action | 🔴 | — | 🔴 MISSING | `mesioToast` not wired |
| Members table | Panel | ✅ | `GET /api/staff` | — | Renders roster |
| Role filter (Todos/Meseros/…) | Action | ✅ | (client-side filter) | — | |
| "Ventas / propinas" column | Metric | 🔴 | — | — | Hard-coded `—` (no per-staff sales endpoint used here) |
| "Desempeño" sparkline | Metric | 🔴 | — | — | Static hard-coded heights |
| ⋯ member actions menu | Action | 🔴 | — | 🔴 MISSING | `mesioToast("próximamente")` |
| + Invitar miembro modal | Modal | ✅ | `POST /api/staff` | (writes) | Creates staff record |
| ↓ Exportar buttons | Action | 🔴 | — | 🔴 MISSING | No export endpoint |
| Segment filter (Hoy/Semana/Mes) | Action | 🔴 | — | — | Only UI toggle, no data reload |
| Auto-refresh 60s | Action | ✅ | (calls loadAll) | — | |

---

## pedidos.html

**Purpose:** Admin order monitor with real-time view and historical log.  
**Auth:** owner / admin / gerente  
**JS file:** `app/static/js/pages/pedidos.js`

| Element | Type | Wiring status | Backend endpoint | Write path? | Notes |
|---|---|---|---|---|---|
| Tiempo real tab — live orders panel | Panel | 🟡 | `GET /api/stats/live-orders` | — | Data fetched but not rendered into DOM (static HTML placeholder remains) |
| ↻ Recargar button | Action | 🟡 | `GET /api/stats/live-orders` | — | Calls loadLiveOrders() but no render logic |
| Histórico tab — orders table | Panel | 🟡 | `GET /api/orders?period=` | — | Fetched but not rendered |
| Channel filter chips (WhatsApp/POS/Domicilio/QR) | Action | ✅ | (client-side DOM filter on static rows) | — | Works on static HTML rows only |
| Period filter (Hoy/7d/Mes/Personalizado) | Action | 🔴 | — | — | Not wired to date range params |
| ↓ Exportar CSV button | Action | 🔴 | — | 🔴 MISSING | `mesioToast("próximamente")` |
| Pagination buttons (← Anteriores / Siguientes →) | Action | 🔴 | — | 🔴 MISSING | Not implemented |
| "Ver mapa de mesas →" link | Nav | ✅ | (navigation to /floorplan) | — | |

---

## reservaciones.html

**Purpose:** Admin reservation manager with day navigation, list/timeline/table views.  
**Auth:** owner / admin / gerente  
**JS file:** `app/static/js/pages/reservaciones.js`

| Element | Type | Wiring status | Backend endpoint | Write path? | Notes |
|---|---|---|---|---|---|
| Day navigation (‹ / ›) + Today | Action | 🟡 | `GET /api/reservations?date=` | — | Data fetched, not rendered into DOM |
| + Nueva reserva button | Action | 🔴 | — | 🔴 MISSING | `mesioToast("próximamente")` |
| Reservation card clicks | Action | 🔴 | — | 🔴 MISSING | `mesioToast` with name only |
| View toggle (Lista / Timeline / Por mesa) | Action | 🟡 | (client-side only) | — | No render logic for Timeline/Por mesa |
| Filtrar button | Action | 🔴 | — | 🔴 MISSING | No handler |
| Confirm/Reject/Cancel reservation actions | Action | 🔴 | (`PUT /api/reservations/{id}/status` exists) | — | No JS handler calling those endpoints |
| Assign table action | Action | 🔴 | (`PUT /api/reservations/{id}/assign-table` exists) | — | No JS handler |
| Stats panel (availability, no-show rate) | Panel | 🔴 | `GET /api/stats/turn-time`, `GET /api/stats/occupancy`, `GET /api/stats/no-show-rate` all exist | — | Not loaded by reservaciones.js |

---

## menu-admin.html

**Purpose:** Admin menu manager: dish availability toggles, inventory view, and recipe (escandallo) CRUD.  
**Auth:** owner / admin  
**JS file:** `app/static/js/pages/menu-admin.js`

| Element | Type | Wiring status | Backend endpoint | Write path? | Notes |
|---|---|---|---|---|---|
| Menu load (dishes, categories) | Panel | ⚠️ | `GET /api/menu` ← **MISSING** | — | Endpoint doesn't exist. Should be `GET /api/dashboard/menu` or `GET /api/pos/menu`. Static HTML remains |
| Dish availability toggle | Action | 🔴 | `PATCH /api/menu/availability` (commented out in JS) | — | JS comment: "TODO: replace with actual dish ID" |
| Inventory tab load | Panel | ✅ | `GET /api/inventory` | — | Loaded but not rendered (static HTML remains) |
| Ordenar compra button | Action | 🔴 | — | 🔴 MISSING | No handler |
| + Agregar producto (inventory) | Action | 🔴 | — | 🔴 MISSING | `mesioToast("próximamente")` |
| Repone / Editar (inventory rows) | Action | 🔴 | (`PUT /api/inventory/{id}`, `/adjust` exist) | — | `mesioToast` only, no endpoint call |
| Escandallos tab | Panel | 🟡 | `GET /api/inventory/recipes` exists | — | Not loaded by menu-admin.js |
| + Nuevo escandallo button | Action | 🔴 | (`POST /api/inventory/recipes` exists) | — | No handler calling it |
| Editar escandallo button | Action | 🔴 | (`PUT`/`DELETE /api/inventory/recipes/{name}` exist) | — | `mesioToast` only |
| 🔄 Sincronizar sucursales button | Action | 🟡 | `POST /api/menu/sync-branches` exists | — | Shows toast but doesn't call endpoint |
| Editar carta button | Action | 🔴 | — | 🔴 MISSING | No endpoint for menu item CRUD from admin |

---

## menu-engineering.html

**Purpose:** Menu performance quadrant (Stars/Puzzles/Plowhorses/Dogs) with cost vs. popularity analysis.  
**Auth:** owner / admin / gerente  
**JS file:** `app/static/js/pages/menu-engineering.js`

| Element | Type | Wiring status | Backend endpoint | Write path? | Notes |
|---|---|---|---|---|---|
| Engineering matrix (quadrant chart) | Panel | 🔴 | `GET /api/menu/analytics` exists (correct data source) | — | JS calls wrong URL: commented-out TODO says `/api/menu-analytics/engineering`. Static bubble chart shown |
| Period filter (7d/30d/90d) | Action | 🔴 | (calls `loadEngineering()` which has no fetch) | — | No-op after period change |
| Quadrant filter (Stars/Puzzles/…) | Action | ✅ | (client-side DOM filter) | — | Works on static rows |
| Engineering table | Panel | 🔴 | — | — | All static HTML |
| ↓ Exportar button | Action | 🔴 | — | 🔴 MISSING | `mesioToast("próximamente")` |
| Column sort | Action | 🟡 | (adds `.active` class only) | — | No actual sort logic |

---

## nps.html

**Purpose:** NPS score dashboard, review feed with filter, and AI-assisted reply actions.  
**Auth:** owner / admin  
**JS file:** `app/static/js/pages/nps.js`

| Element | Type | Wiring status | Backend endpoint | Write path? | Notes |
|---|---|---|---|---|---|
| NPS metrics (score, promoters, passives, detractors) | Panel | ⚠️ | `GET /api/stats/nps` ← **WRONG URL** | — | Actual endpoint is `GET /api/nps/stats`. Fetched but not rendered |
| Period filter (7d/30d/90d/1a) | Action | ⚠️ | (calls `loadNPS()` with wrong URL) | — | |
| Reviews feed | Panel | ✅ | `GET /api/reviews` | — | Loaded but rendered from static HTML, not response data |
| Feed filter (Todas/Pendientes/Urgentes/5★/1-3★) | Action | ✅ | (client-side DOM filter) | — | Works on static HTML rows |
| "✨ Sugerir respuesta" buttons | Action | 🔴 | (`POST /api/ai/proxy` exists) | — | `mesioToast("Generando respuesta…")` only, no actual AI call |
| "Responder manual" button | Action | 🔴 | (`PUT /api/reviews/{id}/reply` exists) | — | `mesioToast("próximamente")` |
| "Asignar a…" button | Action | 🔴 | — | 🔴 MISSING | `mesioToast("próximamente")` |
| "Compensar cliente" button | Action | 🔴 | — | 🔴 MISSING | No loyalty adjustment endpoint wired |
| "Escalar a Carolina" button | Action | 🔴 | — | 🔴 MISSING | Hardcoded name, no endpoint |
| "Enviar encuesta" button | Action | 🔴 | — | 🔴 MISSING | `mesioToast("próximamente")` |
| Publish/hide review toggle | Action | 🔴 | (`PUT /api/reviews/{id}/publish` exists) | — | No JS handler wiring it |

---

## fidelizacion.html

**Purpose:** Loyalty program management: customer segments, campaigns, and program configuration.  
**Auth:** owner / admin  
**JS file:** `app/static/js/pages/fidelizacion.js`

| Element | Type | Wiring status | Backend endpoint | Write path? | Notes |
|---|---|---|---|---|---|
| Loyalty customers load | Panel | ⚠️ | `GET /api/loyalty/customers` ← **MISSING** | — | `/api/loyalty` has `/balance`, `/ledger`, `/stats` but NOT `/customers`. Static placeholder remains |
| Program metrics (members, points, redemption rate) | Panel | 🔴 | — | — | Static HTML |
| Segment cards (VIP, Regulares, Inactivos) | Panel | 🔴 | — | — | Static HTML; `mesioToast` on click |
| "Ver todos" segments button | Action | 🔴 | — | 🔴 MISSING | No handler |
| Campaign list | Panel | 🔴 | — | — | Static HTML |
| Campaign toggle switches | Action | 🔴 | — | 🔴 MISSING | `mesioToast` only |
| Campaign status filter (Activas/Pausadas/Borradores) | Action | 🟡 | (client-side toggle) | — | No render logic for filter |
| + Nueva campaña button | Action | 🔴 | — | 🔴 MISSING | `mesioToast("próximamente")` |
| Configurar programa button | Action | 🔴 | — | 🔴 MISSING | `mesioToast("próximamente")` |

---

## clientes-riesgo.html

**Purpose:** At-risk customer list with churn scoring, AI offer actions, and bulk campaigns.  
**Auth:** owner / admin  
**JS file:** `app/static/js/pages/clientes-riesgo.js`

| Element | Type | Wiring status | Backend endpoint | Write path? | Notes |
|---|---|---|---|---|---|
| At-risk customer list load | Panel | 🟡 | `GET /api/stats/customers-at-risk?limit=50` | — | Fetched but rendered from static HTML rows |
| Sort filter (Churn score/LTV/Última visita) | Action | 🟡 | (client-side toggle only) | — | No re-fetch or DOM sort |
| "Llamar" button per customer | Action | 🔴 | — | 🔴 MISSING | `mesioToast("Iniciando contacto…")` only |
| "✨ Oferta IA" button per customer | Action | 🔴 | — | 🔴 MISSING | `mesioToast` then TODO comment. No campaign send endpoint wired |
| "✨ Campaña masiva IA" bulk button | Action | 🔴 | — | 🔴 MISSING | `mesioToast("próximamente")` |
| "Enviar oferta masiva" medium-risk button | Action | 🔴 | (`POST /api/marketing/send-reengagement` exists) | — | `mesioToast` only, endpoint not called |
| Exportar CSV button | Action | 🔴 | — | 🔴 MISSING | `mesioToast("próximamente")` |

---

## nomina.html

**Purpose:** Payroll calculation, tips summary, and payroll run processing.  
**Auth:** owner / admin  
**JS file:** `app/static/js/pages/nomina.js`

| Element | Type | Wiring status | Backend endpoint | Write path? | Notes |
|---|---|---|---|---|---|
| Payroll period selector + load | Panel | 🟡 | `GET /api/staff/payroll/calculate?period_start=&period_end=` | — | Fetched, not rendered into DOM |
| Tips auto summary load | Panel | 🟡 | `GET /api/staff/tips/auto?period_start=&period_end=` | — | Fetched, not rendered |
| Procesar nómina button | Action | ✅ | `POST /api/staff/payroll/runs` | (writes) | Works end-to-end |
| Exportar PDF button | Action | 🔴 | — | 🔴 MISSING | `mesioToast("próximamente")` |
| Role filter (Todos/Meseros/…) | Action | ✅ | (client-side DOM filter) | — | Works on static rows |
| Row click (individual desglose) | Action | 🔴 | — | 🔴 MISSING | `mesioToast("próximamente")` |
| Configurar % propinas button | Action | 🟡 | (`PATCH /api/staff/tip-distribution` exists) | — | `mesioToast` redirects to "Equipo → Nómina" — no direct modal |

---

## sucursales.html

**Purpose:** Multi-location overview with per-branch KPIs and comparison table.  
**Auth:** owner / admin  
**JS file:** `app/static/js/pages/sucursales.js`

| Element | Type | Wiring status | Backend endpoint | Write path? | Notes |
|---|---|---|---|---|---|
| Branch list load | Panel | ⚠️ | `GET /api/org/locations` ← **MISSING**, fallback `GET /api/restaurants/branches` ← **ALSO MISSING** | — | Correct endpoint is `GET /api/team/branches`. Static HTML always shown |
| Period filter (Hoy/7d/Mes) | Action | 🔴 | (calls `loadLocations()` with broken URLs) | — | |
| + Nueva sucursal button | Action | 🔴 | (`POST /api/team/branches` exists) | — | `mesioToast("próximamente")` |
| Dashboard per-branch button | Action | 🔴 | — | 🔴 MISSING | `mesioToast` with branch name |
| Settings (gear) per-branch button | Action | 🔴 | — | 🔴 MISSING | `mesioToast("próximamente")` |
| Comparison table | Panel | 🔴 | — | — | Static HTML |
| Exportar comparison button | Action | 🔴 | — | 🔴 MISSING | `mesioToast("próximamente")` |

---

## caja.html

**Purpose:** POS dark-mode cashier interface: table orders, pickup queue, delivery proposals, payment, and invoicing.  
**Auth:** caja / admin / owner (staff token)  
**JS file:** `app/static/js/pages/caja.js`

| Element | Type | Wiring status | Backend endpoint | Write path? | Notes |
|---|---|---|---|---|---|
| Billing config load | Panel | ⚠️ | `GET /api/settings/billing` ← **MISSING** | — | Actual endpoint is `GET /api/billing/config`. `_billingConfig` stays null |
| Menu load (categories + dishes) | Panel | ⚠️ | `GET /api/menu` ← **MISSING** | — | Actual endpoints: `GET /api/pos/menu` or `GET /api/dashboard/menu`. Menu never loads |
| Table status grid | Panel | ✅ | `GET /api/pos/tables-status` | — | Wired and renders |
| Customer card lookup | Panel | ✅ | `GET /api/caja/customer/{phone}` | — | |
| POST new table order | Action | ⚠️ | `POST /api/table-orders` ← **MISSING** | — | Actual endpoint is `POST /api/pos/order`. Orders from POS never commit |
| Pickup orders (Para Recoger tab) | Panel | ✅ | `GET /api/orders?type=recoger&status=listo` | — | |
| Mark pickup as delivered | Action | ⚠️ | `PATCH /api/orders/{id}/status` ← **WRONG METHOD** | — | Server has `POST /api/orders/{order_id}/status` (in settings_routes.py). HTTP method mismatch |
| Domicilios tab orders | Panel | ✅ | `GET /api/orders?type=domicilio&status=…` | — | |
| Pay modal (Cobrar F12) | Modal | ✅ | `POST /api/table-orders/{id}/checks/{check_id}/pay` | (writes) | Full split-check flow preserved |
| Quick invoice modal | Modal | ✅ | `POST /api/pos/quick-invoice` | (writes) | |
| Pre-cuenta button | Action | ✅ | (builds check ticket client-side) | — | |
| Enviar a cocina (Cmd+Enter) | Action | ⚠️ | (calls POST /api/table-orders — MISSING) | — | Blocked by wrong order-creation endpoint |
| AI upsell suggestion card | Panel | 🔴 | — | 🔴 MISSING | Static HTML area; no endpoint called |
| Bar-code scanner button | Action | 🔴 | — | 🔴 MISSING | `title="próximamente"` |

---

## kitchen.html

**Purpose:** Kitchen Display System (KDS) for cook — ticket wall with elapsed timers, listo button, and keyboard shortcuts.  
**Auth:** cocina / caja / admin (staff token)  
**JS file:** `app/static/js/pages/kitchen.js`

| Element | Type | Wiring status | Backend endpoint | Write path? | Notes |
|---|---|---|---|---|---|
| Orders load (ticket wall) | Panel | ⚠️ | `GET /api/kitchen/orders` ← **MISSING** | — | Endpoints are `GET /api/table-orders` and `GET /api/kitchen/delivery-orders`. Nothing shows on KDS |
| Station tabs (Activos/Retrasados/Listos) | Action | ⚠️ | (client-side filter after broken load) | — | |
| "Listo" button per ticket | Action | ✅ | `POST /api/table-orders/{id}/status` with `{status:"listo"}` | (writes) | Correct endpoint |
| "+2 min" button | Action | 🔴 | — | 🔴 MISSING | Local visual only; JS comment: "TODO: backend endpoint for server-side timer extension" |
| Item-level done check | Action | 🟡 | (local visual toggle only) | — | No per-item persistence |
| Avg time / queue / delayed stats | Metric | ⚠️ | (computed from broken load data) | — | Will show 0 always |
| Live clock | Metric | ✅ | (client-side `setInterval`) | — | |
| Auto-refresh 15s | Action | ⚠️ | (calls broken `/api/kitchen/orders`) | — | |

---

## bar.html

**Purpose:** Bar Display System — beverage queue with timers and listo button.  
**Auth:** bar / caja / admin (staff token)  
**JS file:** `app/static/js/pages/bar.js`

| Element | Type | Wiring status | Backend endpoint | Write path? | Notes |
|---|---|---|---|---|---|
| Beverage queue load | Panel | ⚠️ | `GET /api/kitchen/orders` ← **MISSING** | — | Same broken endpoint as kitchen.js |
| "Listo" button | Action | ✅ | `POST /api/table-orders/{id}/status` | (writes) | Correct |
| "+2 min" button | Action | 🔴 | — | 🔴 MISSING | Local visual only |
| Inventory low-stock alert | Panel | ✅ | `GET /api/stats/inventory-critical` | — | Shows at bottom of bar page |
| Auto-refresh 15s | Action | ⚠️ | (calls broken URL) | — | |

---

## mesero.html

**Purpose:** Waiter floor view — table grid by zone, waiter alert banner, shift stats.  
**Auth:** mesero / admin (staff token)  
**JS file:** `app/static/js/pages/mesero.js`

| Element | Type | Wiring status | Backend endpoint | Write path? | Notes |
|---|---|---|---|---|---|
| Table grid render | Panel | ✅ | `GET /api/pos/tables-status` | — | Renders live table tiles |
| Zone filter tabs | Action | ✅ | (client-side filter on loaded data) | — | |
| Waiter alert banner | Panel | ✅ | `GET /api/waiter-alerts?resolved=false` | — | |
| "Ver todas" alerts button | Action | ✅ | (renders from already-loaded data) | — | `mesioToast` with alert list |
| Table click → navigate to caja | Action | ✅ | (sessionStorage + location.href = /caja) | via caja.html | Correct pass-through |
| Shift stats bar (ventas/mesas/ticket/propinas) | Metric | ✅ | `GET /api/stats/staff-performance`, `GET /api/stats/tips-pool` | — | |
| Auto-refresh 20s | Action | ✅ | (`mesioInterval(loadTables, 20000)`) | — | |

---

## domiciliario.html

**Purpose:** Delivery driver mobile app — active delivery, queue, history, and status updates.  
**Auth:** domiciliario / admin (staff token)  
**JS file:** `app/static/js/pages/domiciliario.js`

| Element | Type | Wiring status | Backend endpoint | Write path? | Notes |
|---|---|---|---|---|---|
| Orders load (all tabs) | Panel | ✅ | `GET /api/delivery/orders` | — | Fully rendered client-side |
| Active delivery card (navigate/mark delivered) | Action | ✅ | `PATCH /api/delivery/orders/{id}/status` | (writes) | |
| Up-next queue | Panel | ✅ | (derived from same orders load) | — | |
| Historial tab | Panel | ✅ | (filtered from same orders load) | — | |
| "En camino" → "En puerta" → "Entregado" flow | Action | ✅ | `PATCH /api/delivery/orders/{id}/status` | (writes) | Full status machine |
| WhatsApp / call customer links | Action | ✅ | (client-side `wa.me/tel:` links) | — | |
| Waze navigation link | Action | ✅ | (client-side URL build) | — | |
| Hash-check polling every 5s | Action | ✅ | `GET /api/delivery/check-updates` | — | Efficient delta polling |
| Tips panel | Metric | 🔴 | — | 🔴 MISSING | Code comment: "Tips come from a separate endpoint" |
| Profile tab | Panel | 🟡 | (static from localStorage) | — | No backend profile fetch |
| Mapa tab | Panel | 🔴 | — | 🔴 MISSING | Empty tab, no map integration |

---

## staff-hq.html

**Purpose:** Employee clock-in/out portal (staff-clock design) with biometrics, timecard preview, checklist, and shift schedule.  
**Auth:** Any staff (JWT `staff:<uuid>`)  
**JS file:** `app/static/js/pages/staff-clock.js`

| Element | Type | Wiring status | Backend endpoint | Write path? | Notes |
|---|---|---|---|---|---|
| Profile load (name, role, avatar) | Panel | ✅ | `GET /api/staff/self/profile` | — | |
| Clock-in button | Action | ✅ | `POST /api/staff/self/clock-in` | (writes) | PIN or biometric auth modal |
| Clock-out button | Action | ✅ | `POST /api/staff/self/clock-out` | (writes) | |
| Break start/end buttons | Action | ✅ | `POST /api/staff/self/break-start`, `break-end` | (writes) | |
| Timecard preview (weekly hours, deductions) | Panel | ✅ | `GET /api/staff/self/timecard?week_start=` | — | |
| Biometric register button | Action | ✅ | `POST /api/staff/webauthn/register-options` + `register-complete` | (writes) | |
| Biometric auth (clock flow) | Action | ✅ | `POST /api/staff/webauthn/auth-options` + `auth-complete` | (writes) | |
| Upcoming shifts panel | Panel | 🔴 | — | 🔴 MISSING | JS comment: "TODO: /api/staff/self/upcoming-shifts doesn't exist". Falls back to static |
| Propinas · hoy panel | Metric | 🔴 | — | 🔴 MISSING | JS comment: "TODO: GET /api/staff/self/tips not implemented". Shows "Datos próximamente" |
| Checklist del turno | Panel | 🔴 | — | 🔴 MISSING — who creates tasks? | JS comment: "TODO: GET /api/staff/self/tasks not yet implemented". Admin has NO page to create tasks |
| Admin announcements panel | Panel | 🔴 | — | 🔴 MISSING — admin write path absent | JS TODO: "GET /api/staff/announcements". No admin UI to write announcements either |
| Shift swap request | Action | 🔴 | — | 🔴 MISSING | JS comment: "GET /api/staff/self/shift-swap (not yet implemented)" |
| Performance stats (NPS, tips) | Panel | 🔴 | — | 🔴 MISSING | JS comment: "GET /api/staff/self/performance" |
| PIN auth modal | Modal | ✅ | (passes pin param to clock endpoints) | — | |
| WebAuthn biometric auth modal | Modal | ✅ | (WebAuthn browser API + backend) | — | |

---

## menu.html

**Purpose:** Public QR catalog for customers — browse menu and initiate WhatsApp order.  
**Auth:** Public (no login required)  
**JS file:** `app/static/js/catalog-v2.js`

| Element | Type | Wiring status | Backend endpoint | Write path? | Notes |
|---|---|---|---|---|---|
| Full menu load (by table_id or bot_number) | Panel | ✅ | `GET /api/public/menu-context/{table_id}` or `GET /api/public/menu/{bot_number}` | — | Full restaurant + menu + features |
| Category sticky nav | Nav | ✅ | (rendered from menu load) | — | Scroll-spy |
| Hero carousel (featured dishes) | Panel | ✅ | (rendered from `featured:true` dishes) | — | |
| Dish card with image | Panel | ✅ | (Cloudinary URL from `image_url` field) | — | Feature-gated by `catalog_v2_enabled` |
| "Pedir por WhatsApp" button | Action | ✅ | (generates `wa.me` link with cart text) | — | No backend call; direct WA deep link |
| Cart footer | Panel | ✅ | (client-side state) | — | |
| Allergen / tag badges | Panel | ✅ | (from dish JSON fields) | — | |
| View tracking beacon | Action | ✅ | `POST /api/public/menu/track` | — | `navigator.sendBeacon` |
| Retry button on error | Action | ✅ | (re-calls `fetchMenu()`) | — | |
| Category filter (sticky nav clicks) | Action | ✅ | (client-side scroll) | — | |

---

---

## Summary — Gaps by Theme

### Theme 1: KDS (Kitchen + Bar) — Critical Operational Gap

**Affected pages:** `kitchen.html`, `bar.html`  
**Issue:** Both KDS pages call `GET /api/kitchen/orders` which **does not exist**. The real endpoints are `GET /api/table-orders` (POS salon orders) and `GET /api/kitchen/delivery-orders` (WhatsApp delivery). The KDS shows a blank ticket wall on every load.  
**Backend work:** Small — add `GET /api/kitchen/orders` as an alias/aggregation of `GET /api/table-orders?status=recibido,en_preparacion,listo` filtered per branch. Or update the JS to call the correct URL.  
**Priority:** CRITICAL — blocks daily kitchen operation.

### Theme 2: Admin↔Staff Communication — All endpoints missing

**Affected pages:** `staff-hq.html`  
**Missing endpoints:**
- `GET /api/staff/announcements` — admin announcements for staff
- `POST /api/staff/announcements` — admin creates announcement (no admin UI page exists)
- `GET /api/staff/self/tasks` — shift checklist tasks
- `POST /api/staff/self/tasks/{id}/complete` — complete task
- `GET /api/staff/self/performance` — NPS + tip performance
- `GET /api/staff/self/tips` — today/week tip accumulation
- `POST /api/staff/self/shift-swap` — shift swap request

**Write path gap:** There is **no admin page** to create announcements or checklist tasks. Even if the staff endpoint were built, admins have no UI to write the data.  
**Backend work:** Medium (endpoints + DB table for announcements/tasks) + Medium (admin UI in equipo.html or settings.html).  
**Priority:** High — staff UX feature shown prominently on clock page but non-functional.

### Theme 3: Broken URL Wirings (URL Path Mismatches)

| Page | JS calls | Actual endpoint | Fix |
|---|---|---|---|
| `floorplan.html` | `GET /api/floor-plan` | `GET /api/tables/floor-plan` | Update JS URL |
| `caja.html` | `GET /api/menu` | `GET /api/pos/menu` | Update JS URL |
| `caja.html` | `GET /api/settings/billing` | `GET /api/billing/config` | Update JS URL |
| `caja.html` | `POST /api/table-orders` | `POST /api/pos/order` | Update JS URL |
| `kitchen.html` | `GET /api/kitchen/orders` | (create alias or fix URL) | Create endpoint |
| `bar.html` | `GET /api/kitchen/orders` | (same) | Same fix |
| `nps.html` | `GET /api/stats/nps` | `GET /api/nps/stats` | Update JS URL |
| `menu-engineering.html` | `GET /api/menu-analytics/engineering` | `GET /api/menu/analytics` | Update JS URL |
| `sucursales.html` | `GET /api/org/locations` + fallback `GET /api/restaurants/branches` | `GET /api/team/branches` | Update JS URL |
| `fidelizacion.html` | `GET /api/loyalty/customers` | (does not exist, create) | Add endpoint |

**Backend work:** Small (mostly JS fixes) except `loyalty/customers` and `kitchen/orders`.  
**Priority:** High — several pages are completely broken due to wrong URL.

### Theme 4: HTTP Method Mismatches

| Page | JS method | Server method | Fix |
|---|---|---|---|
| `settings.html` | `PATCH /api/settings` | `POST /api/settings` | Either add `PATCH` route alias or fix JS |
| `caja.html` | `PATCH /api/orders/{id}/status` | `POST /api/orders/{order_id}/status` | Fix JS method |

**Additionally:** `settings.html` sends `name`, `nit`, `city`, `cuisine_type`, `opening_hours`, `notifications` fields but the server's `POST /api/settings` only persists `payment_methods`, `opening_hours`, `notifications` are silently ignored because they aren't in the `updatable` list. A separate endpoint (or expansion of the existing one) is needed to update `locations.name`, `locations.address`, etc.  
**Backend work:** Small for method alias; Medium for full settings field expansion.  
**Priority:** High — settings page can't save core restaurant info.

### Theme 5: Static Placeholders with No Render Logic

**Affected pages:** `pedidos.html`, `reservaciones.html`, `menu-admin.html`, `nomina.html`, `nps.html`, `fidelizacion.html`, `clientes-riesgo.html`, `sucursales.html`  
**Issue:** These pages fetch data from the backend (endpoints mostly exist) but the JS never renders the response into the DOM — static HTML placeholder rows remain visible always.  
**Backend work:** Zero (endpoints exist). **Frontend work:** Medium for each page — need render functions that replace static HTML with live data.  
**Priority:** Medium — data exists but users see stale hardcoded content.

### Theme 6: Settings Danger Zone — No Endpoints

**Affected pages:** `settings.html`  
**Missing:** Transfer ownership, pause restaurant, delete restaurant actions all hit `mesioToast("Endpoint pendiente")`.  
**Backend work:** Medium (pause = toggle `bot_active=false`; delete = admin-level operation; transfer = ownership change in `users` table).  
**Priority:** Low (admin-facing, not daily operation), but "pause" has legitimate use case.

### Theme 7: Menu Engineering — Data Exists But Wrong URL

**Affected pages:** `menu-engineering.html`  
**Issue:** `GET /api/menu/analytics` exists and returns engineering matrix data (`menu_analytics_repo.get_menu_engineering_matrix`). The page JS has the correct data source commented out with a wrong URL (`/api/menu-analytics/engineering`). The static bubble chart is never replaced with real data.  
**Backend work:** Zero. **Frontend work:** Small — uncomment + fix URL + add render function.  
**Priority:** Medium — high-value analytics page that's completely static.

### Theme 8: Loyalty — Missing Customer List Endpoint

**Affected pages:** `fidelizacion.html`  
**Issue:** `/api/loyalty` has `/balance`, `/ledger`, `/stats`, `/redeem`, `/adjust` but no `/customers` (list all loyalty members). `fidelizacion.js` calls the missing URL.  
**Backend work:** Small — add `GET /api/loyalty/customers` calling `loyalty_repo.db_get_loyalty_customers()` (likely already exists in the repo).  
**Priority:** Medium.

### Theme 9: NPS — AI Reply + Publish Actions Unwired

**Affected pages:** `nps.html`  
**Issue:** "Sugerir respuesta IA" buttons exist on every review card but only show a toast — they don't call `POST /api/ai/proxy`. "Responder manual" and "Publicar" actions also not wired despite endpoints existing.  
**Backend work:** Zero (endpoints exist). **Frontend work:** Small — wire buttons to existing `PUT /api/reviews/{id}/reply`, `PUT /api/reviews/{id}/publish`, and `POST /api/ai/proxy`.  
**Priority:** Medium — visible feature that does nothing.

### Theme 10: Reservaciones — Read Works, Write Does Not

**Affected pages:** `reservaciones.html`  
**Issue:** Reading reservations works (`GET /api/reservations?date=`). But "Nueva reserva", "Confirmar", "Cancelar", "Asignar mesa" buttons are all `mesioToast` stubs despite backend endpoints (`PUT /api/reservations/{id}/status`, `PUT /api/reservations/{id}/assign-table`) existing.  
**Backend work:** Zero. **Frontend work:** Medium — reservation creation form + wire action buttons.  
**Priority:** Medium-High — reservation management is core to Apparta feature set.

---

## Top 5 Urgent Fixes

### 1. ✅ FIXED — `/api/kitchen/orders` (kitchen.html + bar.html)
`kitchen.js` and `bar.js` now call `GET /api/table-orders` instead of the non-existent `/api/kitchen/orders`. The response shape is identical — `{orders: [...]}` — and the JS filter logic already handles status discrimination client-side.

### 2. ✅ FIXED — Caja POS broken endpoints (caja.html)
All three broken URLs corrected in `caja.js`:
- `GET /api/menu` → `GET /api/pos/menu`
- `POST /api/table-orders` → `POST /api/pos/order` (body shape also fixed: added `table_name` and `total` required by `ManualOrderRequest`)
- `GET /api/settings/billing` → `GET /api/billing/config`
- Bonus: `PATCH /api/orders/{id}/status` → `POST /api/orders/{id}/status` (method fix in `confirmPickup`)

### 3. ✅ FIXED — floorplan.html URL
`floorplan.js` now calls `GET /api/tables/floor-plan`. Response is a direct list `[...]`, handled correctly by the existing `Array.isArray(data) ? data : (data.tables || [])` guard.

### 4. ✅ FIXED — settings.html method mismatch
`settings.js` now calls `POST /api/settings` (was `PATCH`). Note: the backend `POST /api/settings` only persists `features.*` fields (payment_methods, currency, locale, etc.). The `name`, `nit`, `address`, `city`, `cuisine_type`, `opening_hours`, `notifications` fields sent by the form are still silently ignored by the backend — a future backend task to call `db_update_restaurant_fields` for those.

### 5. Wire NPS reply + reservaciones actions — **Medium-High, quick wins** (remaining)
Both pages have fully-implemented backend endpoints that are never called by the frontend. The reply/publish wiring in `nps.html` (3 endpoints) and the confirm/cancel/assign-table wiring in `reservaciones.html` (2 endpoints) represent the highest ratio of impact-to-effort of all remaining gaps. **Effort: 2–3 hours each** (frontend-only, no backend work).

## Additional fixes applied (not in original Top 5)

- **nps.js**: `GET /api/stats/nps` → `GET /api/nps/stats`
- **sucursales.js**: Two-step failing fallback (`/api/org/locations` → `/api/restaurants/branches`) replaced with correct `GET /api/team/branches`
- **menu-admin.js**: `GET /api/menu` → `GET /api/dashboard/menu`; dish availability toggle un-commented and wired to `POST /api/menu/availability` using `dish_name`
- **menu-engineering.js**: TODO comment unblocked; calls `GET /api/menu/analytics?days=N` (correct endpoint)
- **fidelizacion.js**: `GET /api/loyalty/customers` (non-existent) → `GET /api/loyalty/stats`

---

*Audit generated 2026-04-19. Fixes applied same day.*
