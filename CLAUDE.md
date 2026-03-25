# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run server locally
uvicorn app.main:app --reload --port 8000

# Run all tests
pytest

# Run a single test file
pytest tests/test_billing_routes.py -v

# Run a specific test
pytest tests/test_billing_routes.py::test_get_billing_config_not_configured -v
```

## Required Environment Variables

```
DATABASE_URL          # PostgreSQL connection string (asyncpg)
ANTHROPIC_API_KEY     # Claude API key

# WhatsApp / Meta
META_ACCESS_TOKEN     # Meta Graph API access token
META_PHONE_NUMBER_ID  # Default WhatsApp phone number ID
META_APP_SECRET       # For webhook signature verification
META_VERIFY_TOKEN     # Webhook verification token (no default)
META_API_VERSION      # Meta Graph API version, e.g. "v20.0"

# CRM
CRM_PHONE_NUMBER_ID   # Separate WhatsApp number ID used for prospect outreach
ADMIN_KEY             # Secret key for CRM and admin-only endpoints

# Payments
WOMPI_EVENTS_SECRET       # For Wompi webhook signature verification
WOMPI_PUBLIC_KEY          # Wompi public key (used in payment links)
WOMPI_INTEGRITY_SECRET    # For Wompi payment integrity hash verification

# App
APP_DOMAIN            # Public domain, e.g. "mesioai.com" (no https://)
APP_ALLOWED_ORIGINS   # Comma-separated CORS origins, e.g. "https://example.com,http://localhost:3000"
NPS_INTERNAL_KEY      # Random secret used to authorize POST /api/nps/response (internal only)
```

## Deployment

Hosted on Railway. Production start command (from `railway.toml`):
```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 4 --loop uvloop
```

Running 4 workers means **in-process state is not shared across workers**. This shapes several design decisions:
- Rate limiting uses the `meta_rate_limits` PostgreSQL table (not in-memory) so all workers see the same counts.
- Cart race conditions are guarded by a per-phone `asyncio.Lock` in `orders.py` — safe within one worker, but concurrent requests for the same phone could land on different workers. The DB cart is the source of truth.
- NPS state (`_nps_state` dict in `agent.py`) is in-memory and does not survive restarts or cross-worker. Scores ≤ 3 are immediately persisted to DB as a fallback.

## Architecture Overview

Mesio is a multi-tenant SaaS WhatsApp AI bot for restaurants. Each restaurant is a row in the `restaurants` table with its own WhatsApp number, menu (JSONB), and feature flags.

### Request Flow

1. **Incoming WhatsApp message** → `POST /api/webhook/meta` in `app/routes/chat.py`
2. Signature verified → `bot_number` extracted from webhook metadata → restaurant looked up in DB
3. Message dispatched as `asyncio.create_task` (ACK returned immediately to Meta)
4. `app/services/agent.py:chat()` is the AI core:
   - Detects table context (dine-in vs. delivery/pickup) by parsing `[table_id:X]` tags or table number mentions
   - Enriches the user message with `[RESTAURANTE]`, `[MENÚ]`, `[CARRITO]`, `[MESA]` context blocks
   - Calls Claude (`claude-haiku-4-5-20251001` for speed, `claude-sonnet-4-6` for precise tasks) with a structured JSON system prompt
   - Parses the JSON response and dispatches an `action` (`order`, `delivery`, `pickup`, `reserve`, `bill`, `waiter`, `end_session`)

### Multi-tenancy

- Each restaurant has `whatsapp_number` (bot's phone) as the routing key
- `bot_number` from the webhook identifies which restaurant to serve
- Restaurant `features` (JSONB) stores `payment_methods`, `google_maps_url`, `timezone`, `locale`, `currency`, etc.
- Restaurants can have sub-branches: `restaurant_tables` have a `branch_id` FK to `restaurants`

### Key Services

| File | Responsibility |
|------|---------------|
| `app/services/agent.py` | AI chat core, action execution, NPS flow |
| `app/services/database.py` | All DB access via asyncpg connection pool |
| `app/services/orders.py` | Cart management, order creation |
| `app/services/billing.py` | Invoice emission (Siigo, Alegra, Loggro) |
| `app/services/scheduler.py` | Background loop: inactivity detection, session closing |
| `app/services/auth.py` | Token-based session auth (bcrypt + secrets.token_hex) |

### Route Modules

| Prefix | File | Notes |
|--------|------|-------|
| `/api` | `routes/chat.py` | WhatsApp webhooks + `/api/chat` test endpoint |
| `/api` | `routes/orders.py` | Order list, cart, Wompi payment webhook |
| `/api` | `routes/stats.py` | Dashboard sync/stats, conversation management, manual reply, menu availability |
| `/api/tables`, `/menu`, `/cocina` | `routes/tables.py` | Table CRUD, QR generation, waiter alerts, kitchen display, POS manual orders, delivery order management |
| `/api/billing` | `routes/billing.py` | Billing config + invoice emission |
| `/api/crm` | `routes/crm.py` | Internal prospect CRM (ADMIN_KEY protected) |
| `/api` | `routes/nps.py` | NPS responses |
| `/api` | `routes/inventory.py` | Menu availability/inventory |
| (dashboard) | `routes/dashboard.py` | Static HTML dashboard |

### Authentication

`app/routes/deps.py` provides three shared dependencies used across all protected routes:
- `require_auth(request)` — validates Bearer token, returns username
- `get_current_user(request)` — returns the full user dict (includes `branch_id`, `role`)
- `get_current_restaurant(request)` — returns the restaurant row for the authenticated user

Public endpoints (no auth): `GET /api/webhook/meta`, `GET /menu/{table_id}`, `GET /api/public/menu-context/{table_id}`.

### Database Tables (auto-created on startup)

`restaurants`, `billing_log`, `reservations`, `conversations`, `orders`, `carts`, `table_sessions`, `table_orders`, `waiter_alerts`, `restaurant_tables`, `nps_responses`, `inventory`, `meta_rate_limits`, `sessions` (auth), `users`

`table_orders` is distinct from `orders`: it tracks dine-in kitchen tickets (with `base_order_id` grouping sub-orders per table) while `orders` tracks delivery/pickup.

CRM tables are created lazily on first CRM request: `prospects`, `prospect_notes`, `prospect_interactions`, `crm_templates`.

### AI System Prompt

The system prompt in `agent.py:_STATIC_SYSTEM` is marked with `cache_control: ephemeral` for Claude prompt caching. It instructs the model to:
- Always reply in the customer's language
- Return structured JSON with `action`, `items`, `reply`, and optional fields
- Follow a strict sales funnel for external (delivery/pickup) orders
- Use `action="order"` only when a table context is detected

`call_claude()` uses the **assistant prefill trick**: it appends `{"role": "assistant", "content": "{"}` before calling the API, forcing the model to complete a JSON object. The raw response is then prefixed with `{` before parsing. This is why `_parse_bot_response` always receives a string starting with `{`.

User input is sanitized against prompt injection before being sent to Claude (`_sanitize_user_input`, `_INJECTION_RE`).

### NPS Flow

NPS state (`_nps_state` dict in `agent.py`) is in-memory — it does not survive server restarts. For scores ≤ 3, a pending record is persisted to DB via `db_save_nps_pending` so the follow-up comment can be collected after a restart.

### Tests

Tests use `pytest` with no live database or API keys required. Two patterns exist:

- **Sync route tests** (`test_billing_routes.py`): use `fastapi.testclient.TestClient` + the `mock_db` fixture from `conftest.py`, which monkeypatches DB functions and auth via `monkeypatch.setattr`.
- **Async service tests** (`test_table_flow.py`): use `pytest.mark.asyncio` + `unittest.mock.AsyncMock`, monkeypatching at the module level (e.g., `tables_routes.db.get_pool`).
