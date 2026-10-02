# Internal Mesio tools (HQ) and internal vs app separation

> Moved verbatim from CLAUDE.md (2026-09-12) so it isn't loaded on every turn.

## Mesio HQ — unified operations center (post-2026-05-08)

The 5 internal sections (analytics, monitoring, superadmin, crm, costs) now share one shell. The landing page is `/internal` (the HQ homepage — a "morning ritual" page with MRR + 5 KPIs + notification inbox + quick links).
### Shared components

- `app/static/js/internal/hq-shell.js` — sticky topbar + collapsible sidebar + global search input. Auto-injects into every `/internal/*` page (skip via `data-hq-shell="skip"` on body, sidebar opt-in via `data-hq-sidebar="true"`). Auth via `hq_session` in sessionStorage (a single login).
- `app/static/js/internal/cmd-palette.js` — Cmd+K (Mac) / Ctrl+K (Win) palette. Lazy-loaded by the shell. Debounced fetch to `/api/internal/search`. Keyboard nav (↑↓ Enter Esc). Results grouped by type (Tenants / Prospects / Actions).
- `app/static/css/internal/hq-shell.css` — shared design tokens.
- `app/static/html/internal/index.html` — HQ landing page.
- `app/services/audit_middleware.py` — captures every POST/PATCH/PUT/DELETE mutation on `/api/internal/*` (status<400). 21 naming rules. Best-effort, lazy-imports the repo, never breaks the API call.

### Ficha de organización (2026-10-01, HQ control center wave 1)

`/internal/org/{org_id}` (page `html/internal/org.html` + `js/internal/org.js` + `css/internal/org.css`), data from `GET /api/internal/hq/orgs/{org_id}` (`routes/internal/hq.py` → `services/hq_snapshot.py` → `repositories/internal/hq_repo.py`, read-only under bypass). Opened from Superadmin › "Ficha" and from Ctrl+K tenant results.

- **Qué atender**: health flags (org: suspended/paused/overdue/trial_ending; sede: stuck rounds >45 min, web orders open >90 min, waiting acceptance, sittings >6 h, checks open >3 h, proofs to review, delivered unpaid, no activity 7 d, no tables, slow kitchen p90 >30 min, low inventory, NPS negative). Each flag = `RUNBOOK[code]` in `hq_snapshot.py`: severity, title, **where to look**, **how to fix**. Add new situations there.
- **Negocio**: billing status, plan, price/sede, active sedes, MRR (0 unless billed), LLM cost 30 d (cost_metrics_repo) and margin, last panel login.
- **Per sede**: operation (orders/sales today in the sede's local day, 7/30 d, ticket, open tables, rounds, delivery/pickup, kitchen p50/p90 from `table_orders.ready_at` (0107, stamped the first time a round turns listo), waiter alerts, last order), adoption (diner sessions, remembered diners, AI chats, reservations, inventory, ops_config answers, web delivery switches), NPS 30 d, staff with last login (`sessions` rows `staff:<id>`).
- Every per-sede query filters org_id AND location_id. Tests: `tests/test_hq_org_snapshot.py`.
- **Errores (wave 2, 0108 `platform_errors`)**: `error_capture_middleware` in main.py records every unhandled exception and 5xx (org/sede from the user deps already resolved; `X-Request-ID` on every response and in the `http.unhandled` log line → search it in Railway). `agent._call_llm_and_execute` records bot failures (the diner gets "problema técnico") and empty replies as source `bot`. `services/error_log.record_error` is fire-and-forget in a clean context (never breaks or slows the request), masks phone numbers, groups repeats by `fingerprint`; drained on shutdown; scheduler purges > 90 days. Ficha › Errores (7 d) + flags `bot_failing` (≥3 bot errors/24 h, critical) and `errors_repeated` (≥5 server errors/24 h). Global: `GET /api/internal/hq/errors?hours=`. Tests: `tests/test_platform_errors.py`.
- Next waves: support actions (reason + audit), alert rules + email to miguel@mesioai.com.

### New internal endpoints (post-HQ)

| Endpoint | Purpose |
|---|---|
| `GET /api/internal/search?q=` | Cmd+K palette — searches tenants + prospects + actions |
| `GET /api/internal/notifications` | Aggregator of 6 sources (dead letters, cost runaway, churn risk, new prospects, suspended tenants, plan caps) sorted by severity |
| `GET /api/internal/analytics/mrr` | MRR + paying/comp/free split + by_plan + MoM delta |
| `GET /api/internal/analytics/restaurants.csv` (also churn-risk.csv, activation.csv) | CSV exports — blob download via fetch + Authorization header |
| `GET /api/internal/costs/{org_id}/drilldown?period=` | Top-5 most expensive days + cost-per-conv + margin recap per tenant |
| `GET /api/internal/admin/organizations/{org_id}/onboarding` | 5-stage checklist score (created, menu, staff, billing, first_convo) |
| `PATCH /api/internal/admin/organizations/{org_id}/plan` | Change plan (Pricing v1) + optional comp_until |
| `POST /api/internal/admin/users/{username}/reset-password` | Bcrypt hash + delete all sessions for that user |
| `GET /api/internal/admin/audit-log` | Filterable log of all Mesio team mutations |
| `POST /api/internal/crm/prospects/{id}/convert` | Onboarding automation — creates org + admin user + welcome WA in 1 click |
| `GET /api/internal/crm/loss-reasons` | Top-10 aggregated loss reasons |

### Security helpers (new, post-audit 2026-05-07)

- **`app/services/password_hash.py`** — direct bcrypt 4.x API. `hash_password()` pre-hashes long inputs with SHA-256+base64 to avoid the 72-byte truncation. `verify_password()` is 3-path (new pre-hashed, legacy passlib bcrypt, ancient SHA-256 hex) with opportunistic upgrade on login. Replaces CryptContext/passlib EVERYWHERE in the code.
- **`mask_phone()` in `app/services/logging.py`** — `"***" + phone[-4:]`. Apply it in EVERY `log.{info,warning,error,exception}(phone=...)`. Sentry's `before_send` also scrubs by key name (phone/email/token/password/pin/secret/key) in addition to value regex.
- **NFKD injection check** — `_normalize_for_injection_check(text)` before testing `_INJECTION_RE`. The original text still goes to the LLM (no stripping of Spanish accents); it only normalizes for the regex test. Closes the homoglyph bypass (`Ìgnora`, `Ꮖgnore`).
- **`bypass_tenant_scope` reason ≥ 8 chars** — all internal routes touching cross-tenant data MUST enter a bypass. Never use `pool.acquire()` directly in endpoints serving cross-tenant data.

### Explicitly forbidden patterns (post-audit)

1. F-string SQL with user input (documented exception: dynamic SET col=$N column lists from a whitelist).
2. `==` comparing secrets (use `hmac.compare_digest`).
3. `pool.acquire()` directly against a tenant-scoped table (use `tenant_connection()` + scope).
4. Logging `phone=user_phone` without masking.
5. `detail=str(e)` in exception handlers (typed catches → generic `"Internal error"`).
6. `1; mode=block` in X-XSS-Protection (deprecated; use `0`).
7. JWT in localStorage WITHOUT `_escHtml` on every user-data render path (XSS = full token theft).

### Migrations added in HQ + audit

- `0073_perf_idx_blocklist_check` — perf indexes + phone_blocklist WITH CHECK tightening
- `0074_conv_turns_without_progress` — anti-conversational counter
- `0075_pending_plan_downgrade` — plan downgrade with 7d grace
- `0076_rls_restaurant_tables` — security audit RLS hot-fix
- `0077_hq_audit_log` — global audit log table
- `0078_crm_lost_reason` — sales analytics

## Internal vs App Separation

The Mesio team's tools (prospect CRM, Superadmin, platform Analytics, Monitoring, Costs) live under the `internal/` namespace, under `/internal/*` and `/api/internal/*` URLs. They're tools for the Mesio team — NOT features sold to restaurants.

### Separation rule
**App features (catalog, orders, tables, staff, bot, billing, reservations, loyalty) must NOT import from `app/routes/internal/*` or `app/repositories/internal/*`.**

The only allowed exception is `app/routes/chat.py`, which imports `register_inbound_from_prospect` from `app/routes/internal/crm` to log inbound messages from prospects to the CRM number.

### Internal file namespace

| Type | Path |
|---|---|
| Python routes | `app/routes/internal/` |
| Python repos | `app/repositories/internal/` |
| HTML pages | `app/static/html/internal/` |
| JS scripts | `app/static/js/internal/` |

### Internal URLs

| URL | Module | Purpose |
|---|---|---|
| `/internal/analytics` | `routes/internal/analytics.py` | Platform KPI dashboard |
| `/internal/monitoring` | `routes/internal/ops.py` | Real-time infrastructure |
| `/internal/superadmin` | (static HTML) | Restaurant management |
| `/internal/crm` | (static HTML) | Prospect CRM |
| `/api/internal/admin/*` | `routes/internal/admin.py` | Superadmin CRUD |
| `/api/internal/analytics/*` | `routes/internal/analytics.py` | KPI API |
| `/api/internal/billing/*` | `routes/internal/billing_admin.py` | Support billing admin |
| `/api/internal/crm/*` | `routes/internal/crm.py` | Prospect CRM API |
| `/api/internal/ops/metrics` | `routes/internal/ops.py` | Operational metrics |

### Legacy redirects (removed 2026-04-27)
`app/routes/legacy_redirects.py` was removed in the "Dead Code Cleanup" session. It used to 301-redirect `/api/crm/*`, `/api/admin/*`, `/api/analytics/*`, `/api/billing/admin/*`, `/health/metrics`, `/analytics`, `/monitoring` to `/internal/*`. The old URLs now return 404. If an internal tool or a Mesio team bookmark still hits one, update it to the canonical `/internal/...`.
