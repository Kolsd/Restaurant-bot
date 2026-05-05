# Sprint History — Mesio Restaurant Bot

Historical record of completed sprints. Live state lives in `CLAUDE.md`. This file is for reference only — do not consult it for current architectural decisions.

---

## Refactor Blindaje — Fases 1-8 (shipped)

ACID órdenes, webhook inbox durable, Redis multi-worker state, SHA-256 sesiones + anti prompt injection, Decimal end-to-end, Repository pattern completo, god files extraídos, observabilidad + alertas + analytics.

Detalles que SIGUEN siendo relevantes hoy (también capturados en CLAUDE.md secciones específicas):
- **Worker separado (Fase 8)**: `scripts/run_inbox_worker.py` + `railway.toml` con `WORKER_MODE=inbox`. `DISABLE_EMBEDDED_WORKER=1` para web service.
- **Lifespan pattern**: `@asynccontextmanager lifespan(app)` (no `@app.on_event`).
- **Leader election scheduler**: via `state_store.scheduler_leader_acquire` (Redis SET NX EX).
- **Rate limiting**: `state_store.rate_limit_check()` aplicado a `pay_check` (3 req/10s).
- **Observabilidad**: `/monitoring`, `/analytics`, `/health/metrics` + `services/alerts.py` (5 checks cada 60s, cooldown 5min, webhook vía `ALERT_WEBHOOK_URL`).
- **Arquitectura repos**: 16+ repos. Zero SQL en routes/services.

## Integración Apparta — Fases 1-7 (shipped)

Reservas (schema + dashboard + bot), floor plan editor, Wompi deposits, dynamic discounts, reviews públicos.

### Feature Flags Apparta

| Flag | Tipo | Default | Fase | Controla |
|---|---|---|---|---|
| `module_reservations` | opt-out | true | existente | Gates `action="reserve"` en bot |
| `reservation_auto_confirm` | opt-in | false | 3 | Reservas van directo a "confirmed" |
| `reservation_deposits` | opt-in | false | 5 | Cobra depósito Wompi antes de confirmar |
| `reservation_deposit_amount` | config | 50000 | 5 | Monto del depósito en moneda local |
| `dynamic_discounts` | opt-in | false | 6 | Descuentos 5-50% por franja horaria |
| `module_reviews` | opt-in | false | 7 | Publicación de reseñas del NPS |
| `bot_visual_menu` | opt-in | false | Catálogo v2 F1 | Activa envío de platos con foto desde el bot |
| `catalog_v2_enabled` | opt-out | true | Catálogo v2 F1 | Kill-switch global del catálogo visual |

### Tablas nuevas Apparta

| Tabla | Migración | Propósito |
|---|---|---|
| `time_slot_discounts` | 0015 | Descuentos por día/hora con UNIQUE constraint |
| `reservation_deposits` | 0016 | Pagos anticipados Wompi vinculados a reservas |
| `occupancy_snapshots` | 0017 | Fotos periódicas de ocupación para analytics |

### Columnas agregadas

- **`restaurant_tables`** (0014): `capacity INT`, `table_type TEXT`, `zone TEXT`, `position_x REAL`, `position_y REAL`
- **`reservations`** (0014): `status`, `table_id`, `confirmation_sent`, `confirmed_at`, `cancelled_at`, `cancellation_reason`, `deposit_amount`, `deposit_paid`, `deposit_transaction_id`, `no_show`, `branch_id`, `source`, `restaurant_id`
- **`nps_responses`** (0017): `is_public`, `owner_reply`, `owner_reply_at`, `customer_name`

### Diferido: Mesio Pay
Billetera digital + cashback. Requiere regulación financiera colombiana. Alternativa viable: extender loyalty como "crédito".

---

## Closed — historial 2026-04-18/29

- Migración `0012_b2b_sales_system.py` tablas huérfanas → dropeadas por 0031
- `legacy_restaurant_id` column → dropeada por 0041 (zero readers reales)
- `20 bypass_tenant_scope` audit → 18 calls legítimos, zero downgrades
- `db_calculate_tips_by_attendance` per-sede filter → fixed + unskipped
- `db_get_primary_location` rename → renombrada a `db_get_default_location`
- CSP inline-onclick cleanup → resuelto vía rediseño frontend
- POST /api/reservations + restaurant pause → shipped en Sprint X
- Admin↔Staff comms (announcements + tasks) → shipped en Sprint C (migración 0042)
- Staff self-service tips/upcoming/performance → shipped en Sprints Y+Z
- Shift swap (v1) → shipped en Sprint W (migración 0043)
- Fase 2 + Fase 3 hardening → shipped 2026-04-17 (commit ceaddf9)
- Wave-2 Org/Location migration (0034-0038 + 0057 sync) → shipped y validado en producción
- REMEDIATION_PLAN audit (10 sesiones) → 9/10 done, sesión 10 (auth fallback) diferida
- MESA_QR Capa 1 (QR-Phone-Claim) → shipped 2026-04-28 (commit aab0c46)
- MESA_QR Capa 2 (multi-participante con join_code) → shipped 2026-04-29 (migración 0058 + commit 185016a)
- MESA_QR Capa 3 (anti-impostor first-order hold + phone_blocklist) → shipped 2026-04-29 (migraciones 0059 + 0060)
- 10 disconnects de PRODUCT_CONTEXT regla #13 → 9/10 cerrados
- `branch_id` legacy column eradication + forward guard test → done 2026-04-29 (commit 1239ebd)
- 36 test failures + Wompi cash-order prod bug → done 2026-04-29 (commit 084c806). Suite 1214 passed.
- E2E test del flujo completo (Capa 1+2+3) → shipped 2026-04-29

### Backend gaps pre-launch (2026-04-20) — todos resueltos
- `POST /api/table-orders/{id}/checks/single/pay` → existe en tables.py:1377
- `POST /api/staff/self/verify-pin` → existe en staff.py:260
- `?table_id=` param en `/api/table-orders` → soportado y testeado en E2E
- `orders.py` Wompi path → migrado a `tenant_connection()`

---

## Pre-launch hardening sprint (2026-04-20)

6-lead departmental audit + 8-wave execution (commits 39c631c → f5fcf8f):

- Migraciones 0044-0048 (RLS gap webauthn_credentials, policy naming consistency, NUMERIC money precision, CHECK constraint shift_swap status, 7 índices compuestos)
- Wave-2 debris cleanup: `parent_restaurant_id`/`restaurant_id` lecturas muertas en ~12 sitios (team IDOR, table ownership, order status, inventory, reservations, reviews, staff self-profile KeyError)
- RLS bypass en 8 repos (`db_get_nps_*`, `db_get_dashboard_*`, `db_delete_staff_by_id`, etc.) migrados con patrón correcto
- Auth gaps cerrados: `POST /api/chat` (free LLM), `/api/table-sessions/*` (IDOR), Cloudinary upload signing, `/api/media/{id}` org_id comparison
- Bot runtime: raw pool.acquire en `agent_external.py` (3 sitios) → tenant_connection, orphan reservation fix (deposit link preflight antes del insert), confirmation guard extendido a delivery+pickup, `module_reservations` code-level gate, cart preservation on bar failures
- `staff_payroll.py` eliminado (duplicate + crashing endpoint)
- Caja rebuild completo (~1,175 LOC): pay flow + split checks + tip cap + proof validation + chats tab
- Staff clock bio/PIN wiring (cerro vulnerabilidad "anyone clocks as anyone"), kiosco mode via `?kiosco=1`
- Mesero enrichment: 5 nuevos fields en `/api/pos/tables-status`, station filter en kitchen/bar
- Cross-sucursal leak cerrado (X-Branch-ID header en caja/kitchen/bar/mesero/domiciliario)
- Frontend: CSP `connect-src` sin Anthropic, menu-admin image upload wired, billing.html split + XSS fix, sidebar role filtering, sw.js precache modernizado
- PIN login enumeration oracle unificado (siempre 401), tip-distribution exige 100% exacto
- Tests: 1065 passed / 0 failed / 105 skipped

---

## "No-v2" sprint (2026-04-21)

Reemplazo total de placeholders `v2` + seed data fake por features reales:

- 3 agents Sonnet en paralelo (worktrees) + 2 agents de fix de tests = 5 delegaciones
- Frontend: 10 HTML admin limpios de nombres/montos hardcoded + 4 JS populan placeholders que antes nadie llenaba
- Backend: 11 endpoints nuevos — 3 stats aggregates, 3 loyalty aggregates, 5 loyalty campaigns CRUD, 1 payroll approve
- Migración 0049: `loyalty_campaigns` con RLS `org_isolation` + state machine draft/active/paused
- Lint frontend extendido: 5 checks (MOCK/TODO/FETCH/HTML-SEED nombres/HTML-SEED dinero) + `// lint-allow` como supresión con razón obligatoria
- Tests: +27 integration tests verídicos contra `TEST_DATABASE_URL` (baseline 1143 → 1170 passed / 1 pre-existing failure / 27 skipped)
- Patrón de fixture integración documentado (function scope + `_fake_get_pool` async + `SET LOCAL ROLE mesio_app` + manual `tx.start/rollback`)
- Bugs reales descubiertos: 4 backend-path mismatches en JS (`/delivery/orders` sin `/api` 3×, `/payroll/export/{id}` invertido, `approve` endpoint faltante, `/api/admin/logout` legacy)

---

## E2E hardening sprint (2026-05-05)

Disparado por bug real: `domiciliario.html` deployó sin botones (los renderiza el JS según data, sin órdenes la página queda plana). Tests existentes no lo agarraron porque NUNCA cargaban data semilla a la UI.

- 3 agents Sonnet en paralelo: theater audit, page contracts lint, real seed-based E2E
- Theater audit: **0 deletes**, suite ya estaba limpia post-No-v2 (130 tests con value assertions reales). El gap real es UI coverage, no test theater.
- **PAGE-CONTRACTS lint check** (`scripts/lint_frontend.py`): declara botones load-bearing + fetches por cada página operacional (domiciliario, mesero, caja, kitchen, bar, staff-hq). Mutation-tested.
- **5 nuevos E2E tests** en `tests/e2e/` con seed real a `TEST_DATABASE_URL` + assertions de valor computado + mutation tests:
  - `test_domiciliario_page_renders_buttons.py`
  - `test_mesero_table_session_renders.py`
  - `test_caja_validates_proof.py`
  - `test_staff_clock_full_cycle.py`
  - `test_staff_pin_login_e2e.py`
- **Bug prod descubierto y arreglado**: `POST /api/staff/pin-login` llamaba `db_get_staff_for_pin_login()` sin `tenant_scope()` previo → todos los login PIN tiraban `TenantNotSetError`. Fix: wrappear en `tenant_scope(body.restaurant_id)`. Mutation-tested.

---

## Optimization audit sprint (2026-05-05)

Purga de cruft + dead code post-redesign. 3 agents Sonnet en paralelo (Python dead code, frontend assets, scripts hygiene) + cleanup local previo.

- Borrado local: `.audit/`, `.design_review/`, `.design_review_v2/` (TRACKED, 43 archivos), `ai/` scratchpad, `__pycache__/`, `.pytest_cache/`.
- `.gitignore` extendido: `.design_review_v2/`, `ai/`.
- Frontend: 12 archivos borrados — 8 JS legacy del dashboard pre-redesign + 1 CSS + 3 SVG logos.
- Scripts: 7 archivos borrados — 6 debug scratchpads + `scripts/REHEARSAL.md`.
- Python: 0 archivos borrados (todos con consumidores). 2 funciones dead removidas (`db_init_nps_inventory`, `db_init_dish_recipes`) + 3 imports unused.
- File count proyecto (excl. `.git/` y `.claude/worktrees/`): ~1,006 → ~530 (47% reducción).

---

## LLM token + DB hot path optimization (2026-05-05)

Tier S #1 — `perf(llm)` commit 3a9e6e9:
- Cache mutation bug fix en `build_system_prompt` (dynamic content ya NO invalida el bloque cacheado).
- Tool definitions cacheadas (`cache_control: ephemeral` en último tool de cada array).
- `_SYSTEM_EXTERNAL` trim 4,327 → 2,179 tokens (50%).
- `_SYSTEM_SALON` trim 2,064 → 1,225 tokens (41%).
- `MAX_TOKENS` adaptativo: 768 default, 2048 ceiling.
- Estimado: ~$200/mes savings @ 10K msgs/mes.

Tier S #2 — `perf(db)` commit c5a0f9b:
- N+1 fixes en loyalty_repo: `db_get_loyalty_funnel` 5→2 queries, `db_get_loyalty_aggregates` 4→3 queries.
- Migración 0068 con 4 índices compuestos (loyalty_ledger, customer_profiles, reservations, loyalty_customers).

---

## Frontend Redesign + Polish Sprints (Shipped 2026-04-20)

El frontend completo fue rediseñado en 2 bundles de Claude Design + 7 sprints de backend polish post-audit.

### Estructura HTML/CSS/JS

- **Chrome compartido**: `app/static/css/tokens.css` + `app/static/css/shared.css` (foundation), `app/static/js/pages/sidebar.js` (componente shared).
- **Pages**: cada página admin/staff carga `tokens.css` → `shared.css` → `pages/<page>.css` + `mesio-utils.js` → `pages/sidebar.js` → `pages/<page>.js`.
- **13 pages principales rediseñados** + **9 pages admin nuevos**.
- **Zero archivos `*-legacy.html`, `*-v2.html`, `*-wip.html`** post-cleanup 2026-04-20.

### Pages admin (servidos via `app/routes/dashboard.py`)
- `/dashboard`, `/pedidos`, `/reservaciones`, `/menu-admin`, `/menu-engineering`, `/nps`, `/fidelizacion`, `/clientes-riesgo`, `/nomina`, `/sucursales`
- `/floorplan`, `/equipo`, `/settings`, `/billing`
- `/staff-hq` = portal staff self-service (alias `/staff-clock`)

### Pages operacionales (dark theme)
- `/caja` (POS), `/kitchen` (KDS), `/bar` (KDS variante), `/mesero` (tablet grid), `/domiciliario` (mobile)

### Pages públicas
- `/login.html` (dark split panel + 2 modos: admin + staff)
- `/menu.html` (QR público — muestra "Valentina te atiende" cuando table_sessions.assigned_staff_id presente)
- `/demo` (money shot split-screen WhatsApp ↔ dashboard)
- `/dashboard-demo`

### Sprints A–W

| Sprint | Migración | Qué entregó |
|---|---|---|
| Cleanup (53750c1) | — | 10 wiring fixes + audit doc |
| A (bdb40bf) | — | POST /api/settings full save + GET /api/loyalty/customers |
| B (bdb40bf) | — | 8 páginas render live data + NPS/Reservaciones buttons |
| C (d9b4b5c) | 0042 | Admin↔Staff Communication: 3 tablas + 10 endpoints |
| X (3c01507) | — | POST /api/reservations + POST /api/settings/pause |
| Y (d7e3130) | — | GET /api/staff/self/{tips,upcoming-shifts} + staff-clock wire |
| Z (0157f58) | — | GET /api/staff/self/performance |
| W (a11ed7e) | 0043 | Shift swap: tabla + 9 endpoints + state machine + admin approve UI |

### Archivos clave post-sprints

- Repos: `app/repositories/staff_comms_repo.py` (Sprint C/W).
- Routes: `app/routes/staff_comms.py` (20+ endpoints consolidados).
- Migraciones: `0039`, `0041`, `0042`, `0043`.
