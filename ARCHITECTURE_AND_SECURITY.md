# Architecture & Security Rules

> Maintained by the SecOps agent. All contributors must read this before modifying any file.

---

## 1. Project Architecture

### 1.1 Layer Model

```
HTTP Request
    │
    ▼
app/routes/         ← Thin HTTP layer: validates input, calls services, returns HTTP responses.
                      NO business logic. NO direct DB access.
    │
    ▼
app/services/       ← Business logic layer. Orchestrates DB calls, third-party APIs, AI calls.
                      Returns plain dicts/lists, never HTTP exceptions.
    │
    ▼
app/services/database.py  ← Data access layer. All asyncpg queries live here.
                              Never called directly from routes (except legacy code being refactored).
```

### 1.2 Directory Structure

```
app/
├── main.py                 # FastAPI app factory, middleware, router registration
├── routes/                 # HTTP handlers only
│   ├── chat.py             # WhatsApp webhooks (Meta + Twilio) + /api/chat test
│   ├── dashboard.py        # Auth, admin, dashboard data endpoints
│   ├── tables.py           # Table management, kitchen display, POS
│   ├── orders.py           # Delivery/pickup orders, Wompi webhook
│   ├── billing.py          # Invoice emission (Siigo, Alegra, Loggro)
│   ├── crm.py              # Internal prospect CRM (ADMIN_KEY only)
│   ├── nps.py              # NPS collection and stats
│   ├── inventory.py        # Stock management
│   └── stats.py            # Dashboard stats/charts
├── services/
│   ├── agent.py            # AI core: Claude integration, action dispatch, NPS flow
│   ├── database.py         # All asyncpg queries — single source of truth for DB
│   ├── auth.py             # Token auth, bcrypt, session management
│   ├── billing.py          # Siigo / Alegra / Loggro API clients
│   ├── orders.py           # Cart logic, order creation, Wompi payment link
│   └── scheduler.py        # Background inactivity loop
├── data/
│   └── restaurant.py       # Static example data (dev/demo only)
├── migrations/             # One-off migration scripts (run manually)
└── static/                 # Vanilla JS frontend (served as static files)
```

### 1.3 Multi-Tenancy Model

- Each restaurant is one row in `restaurants` keyed by `whatsapp_number` (bot's phone).
- Incoming WhatsApp webhooks are routed by `bot_number` (extracted from Meta metadata).
- Restaurant `features` (JSONB) stores per-tenant config: `payment_methods`, `google_maps_url`, `timezone`, `currency`, `locale`, `delivery_fee`, etc.
- Sub-branches share a `parent_restaurant_id` FK. Tables have a `branch_id` FK.

---

## 2. Authentication & Authorization Rules

### 2.1 Auth Mechanism

- Tokens are generated with `secrets.token_hex(32)` and stored in the `sessions` table with a 72-hour TTL.
- All protected endpoints must call `await verify_token(token)` or the shared `require_auth()` helpers.
- Passwords must be hashed with **bcrypt** via `passlib`. SHA256 is NOT acceptable for new users.

### 2.2 Endpoint Auth Matrix

| Endpoint group | Auth required | Notes |
|---|---|---|
| `POST /api/webhook/meta` | Meta signature (`X-Hub-Signature-256`) | `META_APP_SECRET` env var required |
| `POST /api/webhook/twilio` | None (Twilio signs at IP level) | Consider adding Twilio signature validation |
| `POST /api/auth/login` | None (it IS the login) | Rate-limited: max 10 attempts / 15 min / IP |
| `GET /api/public/menu/{bot_number}` | None | Intentionally public |
| `GET /menu/{table_id}` | None | Intentionally public (QR scan) |
| `GET /catalog` | None | Public menu page |
| `POST /api/nps/response` | Internal call only (from agent service) | Must NOT be publicly callable |
| `GET /orders`, `GET /orders/{id}` | Bearer token required | Owner/admin only |
| `GET /cart/{phone}/{bot_number}` | Bearer token required | Owner/admin only |
| All other `/api/*` endpoints | Bearer token required | |
| `/api/crm/*` | `ADMIN_KEY` header | Mesio internal only |
| `/api/billing/admin/*` | `ADMIN_KEY` query param | Mesio internal only |

### 2.3 Role Hierarchy

```
superadmin  → all restaurants, all data
owner       → their restaurant and branches
waiter      → table orders and waiter alerts only
delivery    → delivery orders only
cashier     → orders and payments only
```

---

## 3. Security Rules (MANDATORY)

### 3.1 SQL — No String Interpolation

**NEVER** build SQL queries by interpolating user-controlled values with f-strings.
**ALWAYS** use asyncpg parameterized queries (`$1`, `$2`, ...).

```python
# BAD — SQL injection risk
query = f"SELECT * FROM table_sessions WHERE closed_at >= NOW() - INTERVAL '{hours} hours'"

# GOOD — parameterized
query = "SELECT * FROM table_sessions WHERE closed_at >= NOW() - ($1 * INTERVAL '1 hour')"
rows = await conn.fetch(query, hours)
```

**Exception:** Column/table names and fixed enum values from `period_map` (server-controlled dicts) are safe to interpolate. User-supplied integers used in INTERVAL must still use the parameterized pattern above.

### 3.2 Input Validation

- Phone numbers: strip all non-digit characters; reject if length < 7 or > 15.
- `period` parameters: always validated against a server-side allowlist dict.
- `limit` parameters: always capped server-side (`limit = min(limit, 500)`).
- `hours` parameters: always capped (`hours = max(1, min(hours, 720))`).
- String inputs from users: max length enforced at Pydantic model level.

### 3.3 XSS Prevention

- All user-supplied text rendered in HTML must be escaped.
- In Vanilla JS: use `element.textContent = value` (safe), never `element.innerHTML = value` (unsafe).
- Never reflect user input directly into HTML responses from the server.

### 3.4 Rate Limiting

| Endpoint | Limit |
|---|---|
| `POST /api/auth/login` | 10 req / 15 min / IP |
| `POST /api/webhook/meta` | Handled by DB-backed per-phone rate limiter (20 msg/60s) |
| `POST /api/nps/response` | Internal only — removed from public surface |
| `GET /api/public/menu/*` | 60 req / min / IP (via middleware) |

### 3.5 Secret Management

- No secrets in code. All secrets via environment variables.
- `wa_access_token` in `restaurants` table: treat as sensitive. Mask in API responses.
- `ADMIN_KEY` must be a random string ≥ 32 chars. No default value.
- `META_VERIFY_TOKEN` must be set. App refuses to start webhook verification without it.

### 3.6 Demo / Seed Users

- **No default users must be created in `init_db()`**.
- Seed users belong in a separate `app/migrations/seed_dev.py` script, run manually, never in production.

---

## 4. Database Rules

### 4.1 Required Indexes (to be enforced by DBA agent)

| Table | Index |
|---|---|
| `sessions` | `(token)` PK, `(expires_at)` for cleanup queries |
| `conversations` | `(phone, bot_number)` PK |
| `orders` | `(bot_number, created_at DESC)`, `(phone)`, `(paid, created_at)` |
| `table_orders` | `(table_id, status)`, `(base_order_id)` |
| `table_sessions` | `(phone, bot_number)`, `(status, closed_at)` |
| `restaurants` | `(whatsapp_number)` UNIQUE |
| `carts` | `(phone, bot_number)` PK |
| `nps_responses` | `(bot_number, created_at DESC)` |
| `inventory` | `(restaurant_id)` |
| `meta_rate_limits` | `(phone, created_at)` |
| `prospects` | `(phone)` UNIQUE, `(stage, archived)`, `(updated_at DESC)` |

### 4.2 Connection Pool

```python
min_size=2, max_size=20, command_timeout=30
```

Never increase `max_size` beyond 20 without a Railway Pro plan. Each Railway worker gets its own pool — with 4 workers that's 80 max connections total.

### 4.3 JSONB Codec

The pool is initialized with a JSONB codec that auto-encodes/decodes Python dicts. Never call `json.loads()` or `json.dumps()` manually on values returned from asyncpg.

---

## 5. API Design Rules

- **Webhooks return 200 immediately.** Processing happens in `asyncio.create_task()`.
- **Errors**: use FastAPI `HTTPException`. Never return `{"error": "..."}` with a 200 status.
- **Pagination**: all list endpoints accept `limit: int` (max 500) and `offset: int`.
- **No duplicate routes**: each HTTP method + path combination must appear exactly once.
- **Public endpoints** must be explicitly documented in section 2.2 above.

---

## 6. Frontend Rules (Vanilla JS)

- Use `textContent`, not `innerHTML` for user-generated content.
- All API calls must include `Authorization: Bearer <token>` from `localStorage.getItem('rb_token')`.
- Currency/locale formatting must use `Intl.NumberFormat` with values from `rb_restaurant` in localStorage — never hardcode `'COP'` or `'es-CO'` as final values.
- No `console.log` with sensitive data (tokens, phone numbers) in production builds.

---

## 7. Deployment

- **Railway**: 4 uvicorn workers, `uvloop`, `--host 0.0.0.0 --port $PORT`.
- **Env vars**: all required vars listed in `CLAUDE.md`. App must fail fast (not silently) if critical vars are missing.
- **DB migrations**: run as one-off Railway jobs from `app/migrations/`, never inline in `startup()`.
- **Startup** (`main.py`): only `CREATE TABLE IF NOT EXISTS` + critical `ALTER TABLE` migrations acceptable. No seed data.
