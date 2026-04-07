# Mesio Restaurant Bot — v8.0 (Staff HQ, Nómina, Turnos, Propinas Automáticas)

## Entorno y Comandos

```bash
Server:  uvicorn app.main:app --reload --port 8000
Migrate: alembic upgrade head          # SIEMPRE antes de arrancar en producción
Tests:   pytest | pytest tests/test_file.py -v
Deploy:  Railway — alembic upgrade head && uvicorn ... --workers 4 --loop uvloop

Variables de entorno críticas:
  DATABASE_URL, ANTHROPIC_API_KEY, META_APP_SECRET, ADMIN_KEY,
  META_ACCESS_TOKEN, WOMPI_PUBLIC_KEY, WOMPI_INTEGRITY_SECRET, APP_DOMAIN
```

## Estructura del Proyecto

```
Restaurant-bot/
├── app/
│   ├── main.py                      # FastAPI entry point, monta rutas y static
│   ├── routes/                      # Capa HTTP — solo validación y respuesta
│   │   ├── deps.py                  # Dependencias: auth, get_current_restaurant, require_module
│   │   ├── chat.py                  # Webhook Meta, mensajes WA, proxy imágenes (/api/media)
│   │   ├── dashboard.py             # Settings, auth, páginas HTML, team, branches
│   │   ├── stats.py                 # Métricas, conversaciones, gráficas
│   │   ├── tables.py                # POS, órdenes de mesa, split checks, tip_amount al pagar
│   │   ├── orders.py                # Órdenes externas (domicilio/recoger) y Webhook Wompi
│   │   ├── billing.py               # DIAN, facturación electrónica
│   │   ├── staff.py                 # Personal, turnos, propinas, nómina, contratos, overtime
│   │   ├── staff_webauthn.py        # Autenticación biométrica FIDO2 para clock-in/out
│   │   ├── crm.py                   # Clientes, prospectos, campañas
│   │   ├── inventory.py             # Inventario, recetas (escandallos)
│   │   └── loyalty.py               # Sistema de puntos y recompensas
│   ├── services/
│   │   ├── database.py              # Queries asyncpg puros (sin ORM)
│   │   ├── agent.py                 # Prompt engineering y llamadas a Anthropic
│   │   ├── auth.py                  # JWT y passwords
│   │   └── orders.py                # Lógica de carrito, órdenes y pagos
│   └── static/
│       ├── html/                    # dashboard, staff-hq, staff-portal, caja, cocina, etc.
│       ├── js/                      # dashboard-core.js, dashboard-components.js, roles.js, sw.js
│       └── css/
├── alembic/versions/
│   ├── 0001_initial_schema.py
│   ├── 0002_staff_tips.py           # staff_shifts, staff_schedules, table_checks.tip_amount
│   ├── 0003_...
│   ├── 0004_...
│   ├── 0005_...
│   ├── 0006_staff_hq_deductions.py  # staff.document_number, staff_deduction_items,
│   │                                #   attendance_deductions, payroll_runs
│   └── 0007_payroll_contracts.py    # contract_templates, overtime_requests,
│                                    #   staff.{contract_template_id, contract_overrides, contract_start}
```

## Arquitectura de Base de Datos

### Tablas principales
`restaurants`, `users`, `orders`, `table_orders`, `table_sessions`, `table_checks`,
`conversations`, `carts`, `staff`, `fiscal_invoices`, `inventory`, `dish_recipes`

### Tablas del módulo Staff & Nómina
| Tabla | Propósito |
|-------|-----------|
| `staff` | Empleados. Columnas clave: `role`, `roles[]`, `pin` (bcrypt), `hourly_rate`, `document_number`, `contract_template_id`, `contract_overrides`, `contract_start` |
| `staff_shifts` | Turnos reales: `clock_in/clock_out TIMESTAMPTZ`. Partial unique index: solo 1 turno abierto por staff |
| `staff_schedules` | Horarios planificados semanales: `day_of_week` (0=Lun…6=Dom), `start_time`, `end_time` |
| `staff_breaks` | Breaks dentro de un turno |
| `staff_deduction_items` | Deducciones manuales por empleado (fixed o percentage) |
| `attendance_deductions` | Deducciones automáticas generadas en clock-in/out (tardiness, early_departure). Tolerancia 5 min |
| `payroll_runs` | Corridas de nómina guardadas como borrador/aprobadas |
| `contract_templates` | Plantillas de contrato: `weekly_hours`, `monthly_salary`, `pay_period` (monthly/biweekly/weekly), `transport_subsidy`, `arl_pct`, `health_pct`, `pension_pct`, `breaks_billable`, `lunch_billable`, `lunch_minutes` |
| `overtime_requests` | Solicitudes de overtime semanal: `status` (pending/approved/rejected). UNIQUE (staff_id, week_start) |
| `tip_distributions` | Histórico de cortes de propinas (legacy, ya no se usa para cálculo activo) |
| `webauthn_challenges` | Challenges FIDO2 single-use, expiran en 5 min |
| `webauthn_credentials` | Credenciales biométricas registradas por empleado |

### Propinas (flujo actual — automático por tiempo)
- `table_checks.tip_amount` se escribe al pagar un check (`POST /api/table-orders/.../checks/{id}/pay`, campo `tip_amount` en body).
- `db_calculate_tips_by_attendance`: por cada check pagado en el período, busca qué staff tenía `clock_in <= paid_at AND (clock_out IS NULL OR clock_out >= paid_at)`, filtra por roles en `features.tip_distribution`, y reparte proporcional.
- Si un rol configurado no tiene a nadie en turno, su % se redistribuye entre los roles presentes.
- `unallocated` = propinas de checks sin staff de ningún rol válido en turno.
- **NO hay corte manual**: se eliminó el endpoint `POST /tip-cut`.

### Deducciones automáticas en clock-in/out
- En `db_clock_in`: si la hora real > scheduled_start + 5 min → inserta `attendance_deductions` tipo `tardiness`.
- En `db_clock_out`: si la hora real < scheduled_end - 5 min → inserta `early_departure`.
- Usa `hourly_rate` de la fila de staff para calcular `deduction_amount`.

## Flujo Operativo de Domicilios y Pagos Asíncronos

1. **Triangulación GPS**: agent.py geocodifica y asigna la sucursal más cercana (radio 5km).
2. **Generación del Pedido**: estado `pendiente`.
3. **Comprobante**: cliente envía foto. Proxy `/api/media/{media_id}` descarga con token Meta.
4. **Súper Caja**: cajero valida comprobante → confirma → KDS de la sucursal recibe el pedido.

## Módulo Staff HQ (`/staff-hq`)

Portal operativo unificado para todo el staff no-admin. Reemplaza las páginas de rol separadas.

- **Login**: `staff-portal.html` con PIN → redirige a `/staff-hq` (operativos) o `/dashboard` (admin/gerente).
- **Auth token**: JWT con claim `staff:<uuid>`. Se almacena en `localStorage` como `rb_staff_token` y también como alias `rb_token` para compatibilidad.
- **Secciones**: Clock card (entrada/salida/break), Timecard semanal con badges de deducción, Biometría (registro/gestión credenciales FIDO2).

### Biometría WebAuthn (`staff_webauthn.py`)
- Registro: requiere Bearer token de staff → `POST /api/staff/webauthn/register-options` + `register-complete`.
- Clock-in/out biométrico (kiosco público): `POST /api/staff/webauthn/auth-options` + `auth-complete`.
- `auth-complete` acepta `action: clock_in | clock_out | break` — incluye lógica de break toggle.
- `RP_ID` se lee de `APP_DOMAIN` env var o del hostname del request.

## Dashboard Admin (`/dashboard`)

### Navegación principal
| Sección | Nav key | Loader |
|---------|---------|--------|
| Equipo | `staff` | `loadStaffSection()` |
| Nómina y Propinas | `payroll` | `loadPayrollSection()` |
| Menú | `menu` | — |
| Estadísticas | `stats` | — |
| ... | ... | ... |

### Sección Equipo — sub-tabs
- **Equipo**: roster de empleados con búsqueda, filtros por rol, cards con estado activo/en turno.
- **Turnos**: editor visual semanal (`_renderShiftsEditor`).
  - Grilla: filas = empleados, columnas = Lun–Dom con la fecha.
  - Click celda vacía → modal crear turno. Click pill → modal editar/borrar.
  - Selección múltiple (checkboxes) → modal aplicar turno masivo.
  - Botón "Copiar semana anterior" → `POST /api/staff/schedules/bulk`.
  - Badges de cumplimiento en días pasados: ✓ verde / ⚠ tardanza / ✗ ausente.

### Sección Nómina — sub-tabs
- **Nómina**: período + preset buttons → `GET /api/staff/payroll/calculate`. Tabla por empleado (Nombre, Documento, Horas, Salario Base, Propinas, Ded. Auto, Ded. Manual, Neto). Panel colapsable de config % propinas por rol (`PATCH /api/staff/tip-distribution`). Card de propinas automáticas (`GET /api/staff/tips/auto`). Guardar borrador / aprobar run.
- **Overtime**: lista de solicitudes pendientes con Aprobar/Rechazar (`PATCH /api/staff/payroll/overtime/{id}`).
- **Contratos**: CRUD de plantillas (`GET/POST/PATCH/DELETE /api/staff/payroll/contracts`). Formulario completo: horas, salario, periodicidad, subsidio transporte, ARL/salud/pensión, breaks billable, lunch billable + duración.

## Endpoints Staff (`/api/staff/...`)

```
# Roster
GET    /api/staff                        → lista staff
POST   /api/staff                        → crear staff
PATCH  /api/staff/{id}                   → editar staff
DELETE /api/staff/{id}                   → desactivar

# Self (staff operativo con Bearer token staff:<uuid>)
GET    /api/staff/self/profile
POST   /api/staff/self/clock-in
POST   /api/staff/self/clock-out
POST   /api/staff/self/break-start
POST   /api/staff/self/break-end
GET    /api/staff/self/timecard          → ?week_start=YYYY-MM-DD

# Turnos y horarios
GET    /api/staff/open-shifts
GET    /api/staff/shifts                 → ?date_from=&date_to=
POST   /api/staff/clock-in              → admin (body: staff_id)
POST   /api/staff/clock-out             → admin (body: staff_id)
GET    /api/staff/schedules
POST   /api/staff/schedules             → crear/actualizar horario
POST   /api/staff/schedules/bulk        → body: {entries: [{staff_id, day_of_week, start_time, end_time}]}
DELETE /api/staff/schedules/{id}

# Propinas
GET    /api/staff/tips/auto             → ?period_start=&period_end=&branch_id=
PATCH  /api/staff/tip-distribution      → body: {config: {rol: pct}}
GET    /api/staff/tip-distributions     → histórico (legacy)

# Deducciones manuales
GET    /api/staff/{id}/deductions
POST   /api/staff/{id}/deductions
PATCH  /api/staff/deductions/{item_id}
DELETE /api/staff/deductions/{item_id}

# Nómina
GET    /api/staff/payroll/calculate     → ?period_start=&period_end=
POST   /api/staff/payroll/runs
GET    /api/staff/payroll/runs
GET    /api/staff/payroll/overtime      → ?week_start=&status=
PATCH  /api/staff/payroll/overtime/{id} → body: {status: approved|rejected, notes}
GET    /api/staff/payroll/contracts
POST   /api/staff/payroll/contracts
PATCH  /api/staff/payroll/contracts/{id}
DELETE /api/staff/payroll/contracts/{id}
PATCH  /api/staff/{id}/contract         → body: {template_id, overrides, contract_start}

# WebAuthn biométrico
POST   /api/staff/webauthn/register-options
POST   /api/staff/webauthn/register-complete
POST   /api/staff/webauthn/auth-options    → body: {restaurant_id, action}
POST   /api/staff/webauthn/auth-complete   → body: {action, credential_id, ...}
GET    /api/staff/webauthn/credentials
DELETE /api/staff/webauthn/credentials/{id}
```

## Reglas de Seguridad

- **SQL**: PROHIBIDO f-strings para inyectar datos. Siempre `$1, $2, ...` posicionales. Excepción aceptada: f-string solo para construir cláusulas `SET col=$n` dinámicas en updates (ver `db_update_deduction_item`), nunca para valores de usuario.
- **Auth**: JWT 72h. Passwords bcrypt. Usuarios (email/pass) vs Staff (nombre+PIN). Staff token = `staff:<uuid>`.
- **XSS**: En JS usar `textContent` para datos de usuario, nunca `innerHTML`. `innerHTML` solo para strings estáticos sin datos externos.
- **JSONB**: asyncpg auto-codifica. No usar `json.dumps()` excepto donde el driver lo requiera explícitamente (e.g. pasar un dict como `$n::jsonb`).
- **NULL en SQL**: `IS NULL` / `IS NOT NULL`. Nunca `WHERE col = NULL`.
- **Fetch en JS**: Usar siempre `_staffFetch(path, method, body)` en lugar de `fetch()` raw. `_staffFetch` incluye auth headers via `_apiHeaders()` y lanza Error con el detalle del servidor si `!res.ok`.

## Frontend — Patrones y Convenciones

### `_staffFetch(path, method='GET', body=null)`
Wrapper sobre `fetch` que:
- Prefija `/api/staff` al path.
- Usa `_apiHeaders()` (lee token de `localStorage.rb_token` y branch ID del selector global).
- Lanza `Error(detail || 'HTTP NNN')` si la respuesta no es 2xx.
- **Siempre usar esto** en lugar de `fetch` directo para endpoints de staff.

### MesioComponent
Factory para componentes con estado reactivo. Patrón:
```javascript
const MiComponent = MesioComponent({
  state: { loading: true, data: [] },
  render(state, el) { ... },
  async onMount(self) { ... },
});
MiComponent.mount('#selector');
MiComponent.setState({ data: [...] });
```

### `_staffFmt(n)` y moneda
Formateador universal que lee `rb_restaurant` de localStorage para obtener `locale` y `currency`. Soporta monedas sin decimales (COP, CLP).

### Días de semana
`day_of_week`: 0=Lunes, 1=Martes, ..., 6=Domingo. (`_DAY_NAMES = ['Lun','Mar','Mié','Jue','Vie','Sáb','Dom']`)

## Staff, POS y Operaciones

- **Roles válidos**: `owner`, `admin`, `gerente`, `mesero`, `caja`, `cocina`, `bar`, `domiciliario`, `otro`.
- **Caja (Súper Caja)**: 3 vistas: Mesas (POS local), Domicilios Pendientes, Chats (validar comprobantes).
- **Split Checks**: `table_checks` permite pagos mixtos. Al cerrar un check → factura individual. Mesa completa → `factura_entregada` cuando todos los checks están en `invoiced/cancelled`.
- **Propinas en checks**: `table_checks.tip_amount` se escribe al pagar. `paid_at = NOW()` se setea automáticamente. Validación: `tip_amount <= subtotal * 0.5`.
- **Turnos**: partial unique index garantiza 1 sola fila abierta por staff (`clock_out IS NULL`).
- **Overtime**: se detecta comparando `billable_minutes` de la semana vs `contract_templates.weekly_hours`. Se crea `overtime_request` con status `pending` para aprobación del admin.

## Contexto Multi-Sucursal

- Header `X-Branch-ID` dicta qué datos leer. Si es `"all"`, retornar Matriz + Sucursales.
- `get_current_restaurant` en `deps.py` resuelve el restaurante del token JWT admin.
- Para staff operativo: `restaurant_id` viene del propio registro de staff en BD.
- `db_calculate_tips_by_attendance` y `db_calculate_payroll` respetan `branch_id` via `ANY($n::int[])`.

## Jerarquía de Sucursales

- Matriz: `parent_restaurant_id IS NULL`.
- Sucursal: `parent_restaurant_id` apunta a la Matriz.
- WhatsApp: sucursales usan sufijo `_b[TIMESTAMP]` en `whatsapp_number` para evitar colisiones.

## Instrucciones para Claude Code

- NO leer `tests/` ni cachés sin que se pida explícitamente.
- NO explicar en exceso, ir al grano.
- Al modificar flujos asíncronos, cuidar `_cart_locks` en `orders.py` (Race Conditions con múltiples workers).
- Al agregar endpoints a `staff.py`, verificar que `Field` esté importado de pydantic (error frecuente).
- Migraciones: siempre usar `IF NOT EXISTS` en `CREATE TABLE/INDEX` y `ADD COLUMN IF NOT EXISTS`.
- La función `db_update_deduction_item` (y similares) usa f-strings SOLO para construir la cláusula `SET` dinámica — esto es intencional, no es una violación de seguridad (los nombres de columna vienen de un `allowed` set hardcodeado).
- Cuando se modifique `_renderShiftsEditor`, recordar que usa `_staffFetch` (no `fetch` raw) para todos los endpoints.
- `day_of_week` en schedules: 0=Lunes (ISO weekday - 1). El JS usa `(d.getDay() + 6) % 7` para convertir JS Sunday=0 → Monday=0.
