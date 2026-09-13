# Mesio Restaurant Bot — v13.0 (head `0081_users_org_location`; 1741 tests con DB)

SaaS multi-tenant para restaurantes (FastAPI + Postgres RLS + Redis + Claude tool_use). Producto: canal web propio del comensal (QR → `/chat/{table_id}`); WhatsApp se retira.

## Uso de tokens — reglas de trabajo (PM 2026-09-12)
- Este archivo se carga en CADA turno: mantenerlo corto. El detalle vive en `docs/claude/*.md` y se lee SOLO cuando la tarea toca ese tema.
- Actualizar docs = editar la sección puntual con Edit. Nunca releer ni reescribir un archivo completo para un cambio pequeño.
- Leer archivos por rango/función (Grep → Read con offset/limit). No leer carpetas ni archivos grandes enteros.
- Tests: correr solo los archivos afectados con `-q`; la suite completa solo antes de commitear cambios de bot/repos. Nunca volcar salida completa (`-q --tb=short`, `| Select-Object -Last 30`).
- Sin agentes/subagentes salvo que el PM lo pida. Sin resúmenes largos: reportar en pocas líneas.
- Sugerir `/clear` al cambiar de tarea.

## Leer antes de tocar (índice de `docs/claude/`)
| Si vas a tocar… | Leer |
|---|---|
| Estado, decisiones de producto cerradas, siguiente sesión | `status.md` |
| Variables de entorno, comandos | `env.md` |
| `agent*.py`, `orders.py`, `orders_repo.py`, `inbox_worker.py`, `state_store.py`, `chat.py` | `bot-rules.md` (OBLIGATORIO) |
| Repos, deps, tenant scope, alembic, org/location, sucursales | `rls-multitenant.md` |
| Estructura, DB, webhook/inbox, Redis, scheduler, Decimal, feature flags | `architecture.md` |
| Wompi / Bold / cobro | `payments.md` |
| Tests nuevos, fixtures con DB, lint frontend | `testing.md` |
| `app/static/**`, catálogo visual | `frontend.md` |
| Staff, nómina, turnos, POS | `staff.md` |
| `/internal/*`, HQ, CRM, superadmin | `internal-hq.md` |
| Reglas de estilo completas | `rules-full.md` |
| Historial de sprints | `docs/history/` |

## Siguiente sesión (orden)
1. ~~Validar bot con LLM real~~ (2026-09-12: e2e 55/59, sim 12/20). Arreglos SIN COMMIT (suite 1772/0, e2e_no_llm 15/15, sim 15/20 con asserts de DB en verde): confirmación con reply vacío (pedido y reserva), alerta `bill` antes del guard de entrega, prompt de recoger, guard de confirmación en `make_reservation`, anuncio falso acotado. Falta re-verificar con LLM (sin crédito Anthropic) y el sim no resetea el tope de conversaciones del org de prueba. Clave en `.env` local.
2. Tiempo real SSE + Redis pub/sub (kitchen/bar/mesero/caja/comensal) — requisito del mapa en vivo y del chat con staff.
3. Ola domicilio web (decisiones cerradas 2026-09-12): link por organización + GPS asigna sede, Turnstile, recoger + domicilio, caja acepta primero, mapa en vivo (Mapbox), chat en vivo con caja, staff con `location_id`, apagar domicilio por WhatsApp.
4. Primer cliente: trial 8 días vía `comp_until`.
5. Barrer `json.dumps()` pasado a `$n::jsonb`.
6. Apagar WhatsApp restante (migrar harness e2e primero).
7. Arreglar webhook Wompi antes de reactivarlo (su test e2e falla: firma inválida → 200, espera 401).
Decisiones de producto cerradas: ver `status.md` — no re-discutir.

## Comandos
```bash
uvicorn app.main:app --reload --port 8000   # local: usar launcher propio, NO sobrescribir .claude/launch.json
alembic upgrade head                        # un solo head siempre (`alembic heads`)
pytest tests/<archivo>.py -q                # con TEST_DATABASE_URL/DATABASE_URL/DATABASE_URL_ADMIN a la DB de test
python scripts/lint_frontend.py             # antes de commitear static
```
Entorno local Windows: `.venv` Py 3.12, Postgres 16 (`postgres`/`mesio_local_dev`; `mesio_app`/`mesio_app_pw`), DBs `mesio_tests`/`mesio_test`/`mesio_fresh` en UTC, sin Redis. Pins: `pytest==8.4.2`, `pytest-asyncio==0.24.0`.

## Reglas no negociables (resumen; detalle en los docs)
- **SQL** solo en `app/repositories/`, parámetros `$n`, nunca f-string con valores.
- **RLS**: `async with tenant_connection()` + `tenant_scope(org_id)`; cross-tenant solo con `bypass_tenant_scope("razón≥8")`. Nunca `get_pool()` directo en repos nuevos, nunca capturar `TenantNotSetError`. Tabla nueva tenant → RLS ENABLE+FORCE en migración.
- **Org vs sede**: `org_id` y `location_id` son enteros distintos; nunca adivinar ni usar fallback entre ellos. `db_get_restaurant_by_location_id` / `_by_org_id` según intención. Tests de tenant siembran ids que choquen a propósito.
- **Dinero**: `Decimal` + `services/money.py`; `float` solo en borde JSON (`# JSON boundary`). Nunca Decimal en `state_store`.
- **4 workers**: estado mutable por `state_store` (Redis). Inbox worker claim-then-ack, nunca transacción larga.
- **Logging**: `get_logger(__name__)`, catch tipado, sin `except Exception: pass`, sin `print`, `mask_phone()` en logs.
- **Frontend**: `textContent` para datos de usuario; `mesioHeaders()`/`_staffFetch`; commit con static → subir `CACHE_VERSION` en `sw.js` en el mismo commit.
- **Alembic**: rev id ≤ 32 chars; `sa.text()` + `CAST(:p AS tipo)`; `IF NOT EXISTS`.
- **Tests verídicos**: nada de tests que se saltan en silencio, `status_code == 200` como única aserción, ni mockear el repo entero. Fechas en UTC. Pasarelas: firmas contra doc/evento real.
- **Si lo ves, lo arreglás** (o preguntás si es decisión de producto / expande >50% el scope).
- **LLM del bot = siempre Haiku 4.5** (decisión PM 2026-09-12). Los fallos del bot se arreglan en código/guards/prompts, nunca subiendo de modelo.
