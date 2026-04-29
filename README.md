# Mesio Restaurant Bot — v11.0

SaaS multi-tenant de WhatsApp con IA para restaurantes. Cada restaurante opera en un tenant aislado con Row-Level Security en Postgres. Gestiona pedidos, reservas, mesas (POS), staff, nomina y facturacion electronica. El bot corre sobre Claude (tool_use API) y recibe mensajes via Meta WhatsApp Business API.

---

## Quickstart

```bash
git clone <repo>
cd Restaurant-bot
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # editar con tus valores
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

### Variables de entorno criticas

| Variable | Descripcion |
|---|---|
| `DATABASE_URL` | Conexion runtime como `mesio_app` (non-superuser). RLS se aplica automaticamente. |
| `DATABASE_URL_ADMIN` | Conexion superuser SOLO para migraciones Alembic. La app NUNCA debe usar esta URL en runtime. Si no se setea, Alembic cae a `DATABASE_URL`. |
| `ANTHROPIC_API_KEY` | API key de Anthropic para el LLM (Claude). |
| `META_APP_SECRET` | Secret para verificar firmas de webhooks de Meta. |
| `ADMIN_KEY` | Key para endpoints internos de Mesio (analytics, monitoring, superadmin). |
| `REDIS_URL` | Estado compartido entre workers (NPS, checkout, cooldowns, cart locks). Sin esto el bot degrada a in-process. |
| `META_ACCESS_TOKEN` | Token de acceso de la app Meta para enviar mensajes salientes. |
| `APP_DOMAIN` | Dominio publico (usado en WebAuthn RP_ID y links de pago). |

Variables opcionales: `OPENAI_API_KEY` (transcripcion de audios via Whisper), `CLOUDINARY_*` (catalogo visual), `ALERT_WEBHOOK_URL`, `WOMPI_*`, `BOT_MAX_TOKENS`, `BOT_MODEL_FAST`, `BOT_MODEL_PRECISE`, `DISABLE_EMBEDDED_WORKER`, `WORKER_MODE`.

---

## Arquitectura

- **Capas**: HTTP routes (sin SQL) -> repositories (todo el SQL) -> `tenant_connection()` -> Postgres con RLS activo. Zero SQL directo en routes o services.
- **Multi-worker**: Estado compartido (NPS, checkout, cooldowns, cart locks) en Redis via `app/services/state_store.py`. Cuatro uvicorn workers compiten por inbox via `FOR UPDATE SKIP LOCKED`.
- **Bot AI**: Claude tool_use API. El orquestador vive en `app/services/agent.py`. Las definiciones de tools en `app/services/agent_tools.py`. Todo texto del usuario pasa por `_wrap_user_message()` antes de llegar al LLM.
- **Webhook durable**: `POST /webhook` encola en `webhook_inbox` (Postgres). El `inbox_worker` procesa con patron claim-then-ack en 3 fases: claim (transaccion corta, ms) -> dispatch (hasta 120s, sin conexion DB) -> ack (conexion nueva, ms). Nunca meter dispatch dentro de una transaccion larga.
- **Scheduler**: Loop de background con leader election via Redis (`scheduler_leader_acquire`). Solo un worker ejecuta el tick. Maneja inactividad, recordatorios, depositos, ocupacion y alertas operativas.

---

## Seguridad multi-tenant (RLS)

RLS activo en 33 tablas. En runtime, la app conecta como `mesio_app` (non-superuser, LOGIN), lo que hace que Postgres aplique las politicas automaticamente. El GUC `app.restaurant_id` se setea por conexion via `SET LOCAL` en `tenant_connection()`.

Para rutas cross-tenant legitimas (internas Mesio, scheduler, inbox pre-resolucion), se usa `bypass_tenant_scope("reason")` que activa `SET LOCAL ROLE mesio_superadmin` (BYPASSRLS, NOINHERIT, sin LOGIN).

Regla de oro: nunca usar `get_pool()` o `pool.acquire()` directo en repos nuevos. Siempre `async with tenant_connection() as conn:` con `tenant_scope(rid)` activo en el call site.

Ver seccion "Blindaje Multi-tenant RLS" en [CLAUDE.md](CLAUDE.md) para el detalle completo, incluyendo prueba empirica y clasificacion de repos.

---

## Tests

```bash
# Suite completa (766 tests, excluye simulacion E2E)
pytest tests/ --ignore=tests/ai_sim

# Simulacion E2E real (requiere Postgres + Anthropic API activos)
python run_ai_sim.py   # 20 escenarios multi-turno
```

---

## Deploy (Railway)

El `railway.toml` arranca condicionalmente segun `WORKER_MODE`:

```bash
# Web service (default): 4 uvicorn workers + scheduler + inbox worker embebido
uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 4 --loop uvloop

# Worker service separado: WORKER_MODE=inbox
python scripts/run_inbox_worker.py
```

Ambos servicios comparten la misma DB y Redis. Antes del primer deploy, setear `DATABASE_URL` apuntando al role `mesio_app` y `DATABASE_URL_ADMIN` apuntando al superuser `postgres`. Sin esta separacion, RLS no se aplica correctamente en produccion.

---

## Documentacion

- [CLAUDE.md](CLAUDE.md) — Spec de arquitectura completa, reglas de codigo, convenciones de repos, reglas del bot (NO ROMPER), y estado actual de todas las fases de hardening. Fuente autoritativa.
- [docs/PRODUCT_CONTEXT.md](docs/PRODUCT_CONTEXT.md) — Vision de producto, stage actual, reglas de comportamiento del agente IA, mapa de zonas sagradas/optimizables/borrables.
