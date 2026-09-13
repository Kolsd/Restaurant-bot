# Herramientas internas Mesio (HQ) y separación internal vs app

> Movido verbatim desde CLAUDE.md (2026-09-12) para no cargarlo en cada turno.

## Mesio HQ — operations center unificado (post-2026-05-08)

Las 5 secciones internas (analytics, monitoring, superadmin, crm, costs) ahora comparten un shell. El landing es `/internal` (homepage del HQ — "morning ritual" page con MRR + 5 KPIs + notification inbox + quick links). Detalle completo en [memory/project_hq_unified.md](C:\Users\miguel.diaz\.claude\projects\C--Users-miguel-diaz-Documents-Mesio-Restaurant-bot\memory\project_hq_unified.md).

### Componentes compartidos

- `app/static/js/internal/hq-shell.js` — sticky topbar + collapsible sidebar + global search input. Auto-inyecta en todas las páginas /internal/* (skip via `data-hq-shell="skip"` en body, sidebar opt-in via `data-hq-sidebar="true"`). Auth via `hq_session` en sessionStorage (un solo login).
- `app/static/js/internal/cmd-palette.js` — Cmd+K (Mac) / Ctrl+K (Win) palette. Lazy-loaded por el shell. Debounced fetch a `/api/internal/search`. Keyboard nav (↑↓ Enter Esc). Resultados agrupados por tipo (Tenants / Prospectos / Acciones).
- `app/static/css/internal/hq-shell.css` — design tokens compartidos.
- `app/static/html/internal/index.html` — landing del HQ.
- `app/services/audit_middleware.py` — captura toda mutación POST/PATCH/PUT/DELETE en `/api/internal/*` (status<400). 21 reglas de naming. Best-effort, lazy-import del repo, nunca rompe la API call.

### Endpoints internos nuevos (post-HQ)

| Endpoint | Propósito |
|---|---|
| `GET /api/internal/search?q=` | Cmd+K palette — busca tenants + prospects + acciones |
| `GET /api/internal/notifications` | Aggregator de 6 fuentes (dead letters, cost runaway, churn risk, new prospects, suspended tenants, plan caps) sorted by severity |
| `GET /api/internal/analytics/mrr` | MRR + paying/comp/free split + by_plan + MoM delta |
| `GET /api/internal/analytics/restaurants.csv` (también churn-risk.csv, activation.csv) | CSV exports — blob download via fetch + Authorization header |
| `GET /api/internal/costs/{org_id}/drilldown?period=` | Top-5 expensive days + cost-per-conv + margin recap por tenant |
| `GET /api/internal/admin/organizations/{org_id}/onboarding` | 5-stage checklist score (created, menu, staff, billing, first_convo) |
| `PATCH /api/internal/admin/organizations/{org_id}/plan` | Cambiar plan (Pricing v1) + comp_until opcional |
| `POST /api/internal/admin/users/{username}/reset-password` | Bcrypt hash + delete all sessions for that user |
| `GET /api/internal/admin/audit-log` | Filterable log de todas las mutaciones del equipo Mesio |
| `POST /api/internal/crm/prospects/{id}/convert` | Onboarding automation — crea org + admin user + welcome WA en 1 click |
| `GET /api/internal/crm/loss-reasons` | Top-10 razones de pérdida agregadas |

### Helpers de seguridad (nuevos, post-audit 2026-05-07)

- **`app/services/password_hash.py`** — bcrypt 4.x direct API. `hash_password()` pre-hashea long inputs con SHA-256+base64 para evitar truncation a 72 bytes. `verify_password()` es 3-path (nuevo pre-hashed, legacy passlib bcrypt, ancient SHA-256 hex) con upgrade oportunista al login. Reemplaza CryptContext/passlib en TODO el código.
- **`mask_phone()` en `app/services/logging.py`** — `"***" + phone[-4:]`. Aplicar en TODO `log.{info,warning,error,exception}(phone=...)`. Sentry `before_send` también scrub por nombre de key (phone/email/token/password/pin/secret/key) además de regex en valores.
- **NFKD injection check** — `_normalize_for_injection_check(text)` antes de testear `_INJECTION_RE`. El texto original sigue al LLM (no strip de acentos españoles); solo normaliza para el regex test. Cierra homoglyph bypass (`Ìgnora`, `Ꮖgnore`).
- **`bypass_tenant_scope` reason ≥ 8 chars** — todas las rutas internas DEBEN entrar a bypass si tocan datos cross-tenant. Nunca usar `pool.acquire()` directo en endpoints serving cross-tenant data.

### Patrones explícitamente prohibidos (post-audit)

1. F-string SQL con user input (excepción documentada: dynamic SET col=$N column lists desde whitelist).
2. `==` comparing secrets (usar `hmac.compare_digest`).
3. `pool.acquire()` directo contra tabla tenant-scoped (usar `tenant_connection()` + scope).
4. Logging `phone=user_phone` sin mask.
5. `detail=str(e)` en exception handlers (typed catches → generic `"Error interno"`).
6. `1; mode=block` en X-XSS-Protection (deprecated; usar `0`).
7. JWT in localStorage SIN `_escHtml` en cada user-data render path (XSS = full token theft).

### Migrations añadidas en HQ + audit

- `0073_perf_idx_blocklist_check` — perf indexes + phone_blocklist WITH CHECK tightening
- `0074_conv_turns_without_progress` — anti-conversational counter
- `0075_pending_plan_downgrade` — plan downgrade with 7d grace
- `0076_rls_restaurant_tables` — security audit RLS hot-fix
- `0077_hq_audit_log` — global audit log table
- `0078_crm_lost_reason` — sales analytics

## Separación Internal vs App

Las herramientas del equipo Mesio (CRM de prospectos, Superadmin, Analytics de plataforma, Monitoring, Costs) viven en el namespace `internal/` bajo URLs `/internal/*` y `/api/internal/*`. Son herramientas para el equipo Mesio — NO son features vendibles a restaurantes.

### Regla de separación
**Las features de la app (catálogo, órdenes, mesas, staff, bot, billing, reservas, fidelidad) NO deben importar de `app/routes/internal/*` ni de `app/repositories/internal/*`.**

La única excepción permitida es `app/routes/chat.py` que importa `register_inbound_from_prospect` de `app/routes/internal/crm` para registrar mensajes entrantes de prospectos al número CRM.

### Namespace de archivos internos

| Tipo | Ruta |
|---|---|
| Routes Python | `app/routes/internal/` |
| Repos Python | `app/repositories/internal/` |
| HTML pages | `app/static/html/internal/` |
| JS scripts | `app/static/js/internal/` |

### URLs internas

| URL | Módulo | Propósito |
|---|---|---|
| `/internal/analytics` | `routes/internal/analytics.py` | Dashboard KPIs plataforma |
| `/internal/monitoring` | `routes/internal/ops.py` | Infraestructura real-time |
| `/internal/superadmin` | (HTML estático) | Gestión restaurantes |
| `/internal/crm` | (HTML estático) | CRM prospectos |
| `/api/internal/admin/*` | `routes/internal/admin.py` | CRUD superadmin |
| `/api/internal/analytics/*` | `routes/internal/analytics.py` | API KPIs |
| `/api/internal/billing/*` | `routes/internal/billing_admin.py` | Billing admin soporte |
| `/api/internal/crm/*` | `routes/internal/crm.py` | API CRM prospectos |
| `/api/internal/ops/metrics` | `routes/internal/ops.py` | Métricas operacionales |

### Legacy redirects (eliminados 2026-04-27)
`app/routes/legacy_redirects.py` fue eliminado en la sesión "Dead Code Cleanup". Antes hacía 301 de `/api/crm/*`, `/api/admin/*`, `/api/analytics/*`, `/api/billing/admin/*`, `/health/metrics`, `/analytics`, `/monitoring` hacia `/internal/*`. Las URLs viejas ahora retornan 404. Si una herramienta interna o bookmark del equipo Mesio sigue pegándole, actualizar al canónico `/internal/...`.

