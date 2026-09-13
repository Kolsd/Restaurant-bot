# Versión completa de reglas de estilo e instrucciones críticas

> Movido verbatim desde CLAUDE.md (2026-09-12) para no cargarlo en cada turno.

## Reglas de Seguridad y Estilo

- **SQL**: PROHIBIDO f-strings para inyectar valores. Siempre `$1, $2, ...` posicionales. Excepción aceptada: f-string solo para construir cláusulas `SET col=$n` dinámicas en updates (ver `db_update_deduction_item`), nunca para valores de usuario.
- **Auth**: JWT 72h. Passwords bcrypt. Usuarios (email/pass) vs Staff (nombre+PIN). Staff token = `staff:<uuid>`. Sesiones admin almacenadas como SHA-256 hash.
- **XSS**: En JS usar `textContent` para datos de usuario, nunca `innerHTML`. `innerHTML` solo para strings estáticos sin datos externos.
- **JSONB**: asyncpg auto-codifica. No usar `json.dumps()` excepto donde el driver lo requiera explícitamente (e.g. pasar un dict como `$n::jsonb`).
- **NULL en SQL**: `IS NULL` / `IS NOT NULL`. Nunca `WHERE col = NULL`.
- **Fetch en JS**: Usar siempre `_staffFetch(path, method, body)` o `mesioHeaders()` en lugar de `fetch()` raw.
- **AI API**: PROHIBIDO llamar a Anthropic/OpenAI desde el browser. Usar proxy `POST /api/ai/proxy` (auth requerido, server-side).
- **Logging**: PROHIBIDO `except Exception: pass`. Usar `from app.services.logging import get_logger; log = get_logger(__name__)`. Catch tipado + `log.exception("contexto.evento", **ctx)`. Si afecta consistencia de datos/dinero → re-raise tras loguear.
- **Money**: PROHIBIDO `float` en aritmética financiera. Usar `Decimal` + helpers de `services/money.py`. `float(...)` solo en el borde JSON con comentario `# JSON boundary`.
- **Prompt injection**: Cualquier nuevo punto donde se inyecte texto del usuario al LLM debe pasar por `_wrap_user_message(...)`.
- **Multi-tenant RLS (Fase 1)**: PROHIBIDO `pool.acquire()` / `get_pool()` directo en repos nuevos — usá `async with tenant_connection() as conn:`. El call site debe entrar en `tenant_scope(rid)` (rutas admin/staff via deps `_scoped`) o en `bypass_tenant_scope("reason")` (internal/scheduler/inbox pre-resolve). PROHIBIDO capturar `TenantNotSetError` (es señal de call site sin scope). Nueva tabla con `restaurant_id NOT NULL` DEBE agregarse a `_RLS_TABLES` en una migración que habilite + force RLS.
- **DB URLs**: app runtime conecta como `mesio_app` (non-superuser) via `DATABASE_URL`. Alembic corre con `DATABASE_URL_ADMIN` (postgres superuser). PROHIBIDO apuntar la app runtime a una URL superuser — invalida el enforcement de RLS.

## Instrucciones Críticas para Claude Code
- **No Vaguedad**: Ante una duda técnica, pregunta antes de proponer cambios masivos que consuman tokens.
- **Aislamiento Multi-Worker**: Al modificar estados (`NPS`, `checkout`), asume siempre que hay 4 workers y usa `state_store` (Redis).
- **Patrón Repositorio**: Prohibido SQL en `app/routes/` y `app/services/` (excepto `billing.py` fiscal). Todo SQL nuevo va en `app/repositories/`.
- **Multi-tenant RLS (v11.0)**: Todo repo nuevo que toque una tabla con `restaurant_id` DEBE usar `async with tenant_connection() as conn:` y ser llamado desde un call site con `tenant_scope(rid)` activo (o `bypass_tenant_scope("reason")` si es genuinamente cross-tenant). LEE la sección "Blindaje Multi-tenant RLS" antes de tocar repos, deps, bot runtime, o alembic. El estado ha sido verificado empíricamente — no lo rompas con "silent fails" o catches genéricos.
- **Precisión Financiera**: Prohibido usar `float` para dinero. Usa `Decimal` y los helpers en `app/services/money.py`.
- **Logging Estricto**: Usa `structlog` vía `get_logger(__name__)`. Prohibido el uso de `print()` o bloques `except Exception: pass`.
- **Migraciones**: Usa siempre `IF NOT EXISTS` para garantizar que el comando de inicio en Railway no falle. Alembic corre con `DATABASE_URL_ADMIN` (superuser); la app runtime conecta con `DATABASE_URL` (mesio_app non-superuser).
- **Patrones Alembic (3 footguns que rompieron prod 2026-05-06)**:
  1. **Multiple heads**: si dos sprints paralelos crean migraciones con el mismo `down_revision`, `alembic upgrade head` falla con "Multiple head revisions are present". ANTES de mergear a main, correr `alembic heads` — debe retornar 1 línea. Si retorna 2+, crear merge migration no-op (ver `0072_merge_plan_limits_demo_seed.py` como patrón) con `down_revision = ("rev_a", "rev_b")` y `upgrade/downgrade` vacíos.
  2. **`conn.execute("string")` no funciona en SQLAlchemy 2.0**. Usar `import sqlalchemy as sa` + `conn.execute(sa.text("..."))`. `op.execute("string")` SÍ acepta strings raw (alembic los convierte) pero `op.get_bind().execute(...)` no — son APIs distintas. Patrón canónico del repo: `0034_create_organizations_locations.py`.
  3. **`:param::tipo` rompe con `sa.text()` bound params**. El regex de SQLAlchemy se confunde con el `::` (cast operator) y deja `:param` literal en la query final → `psycopg2.errors.SyntaxError`. Usar `CAST(:param AS tipo)` en su lugar. Ej.: `INSERT ... VALUES (CAST(:menu AS jsonb))` ✅, NO `:menu::jsonb` ❌.
- **Bot Intocable**: LEER la sección "Reglas del Bot — NO ROMPER" ANTES de tocar cualquier archivo del bot. Cada regla existe por un bug real que afectó a clientes.
- **Tests Obligatorios**: Después de cualquier cambio en archivos del bot, correr `pytest tests/ --ignore=tests/ai_sim`.
  - **Con `TEST_DATABASE_URL` exportada** (DB en UTC, migrada a head): **1741 passed / 0 failed / 6 skipped** en ~2.5 min (sin e2e).
  - **Sin `TEST_DATABASE_URL`**: **1400 passed / 0 failed** (~350 skipped: los tests con DB). Nunca tomar este número como prueba de salud: no ejercita la DB.
  - **E2E tests (`tests/e2e/`)**: requieren `TEST_DATABASE_URL` + `ANTHROPIC_API_KEY` (los `e2e_no_llm` solo DB). Correr serial — son lentos por seed real + Anthropic real.
  - Cualquier failure nuevo es regresión real — no merguear hasta resolverla.
- **Claim-then-ack**: NUNCA revertir inbox_worker a transacción larga. El patrón de 3 fases existe para evitar pool deadlock.
- **Frontend Lint ("No-v2" sprint)**: Antes de mergear cualquier cambio que toque `app/static/js/**` o `app/static/html/**`, correr `python scripts/lint_frontend.py` — CI falla si encuentra mock/TODO/dead-fetch/seed-data. Supresión legítima via `// lint-allow: razón` (JS) o `<!-- lint-allow: razón -->` (HTML). PROHIBIDO suprimir sin razón explícita.
- **Tests Verídicos**: Nuevos tests integration contra `TEST_DATABASE_URL` DEBEN probar correctness de agregados (seed data → assert valor exacto), tenant isolation (org A vs org B), y caso vacío (sin 500). Prohibidos: `assert status_code == 200` como única aserción, mock del repo completo, `assert "key" in data` sin checkear valor. Ver fixture de referencia en `tests/test_loyalty_aggregates.py` ("No-v2" sprint).
- **Tool Use Nativo**: El bot usa Claude tool_use API. NUNCA volver a JSON-in-prompt. `_validate_tool_call()` es la barrera de seguridad.
- **Checkout State Machine**: Antes de modificar `handle_checkout_flow`, dibujar mentalmente todos los steps y verificar que cada uno tiene branch. Un step sin branch = checkout roto.
- **Regla "Si lo ves, lo arreglás" (PM 2026-04-29)**: Si durante una sesión encontrás algo roto, raro o sospechoso — aunque sea pre-existente, aunque no esté en el scope literal de la tarea — NO escribís en el reporte "es pre-existente", "no es mío", "fuera de scope", "deuda histórica". Lo arreglás o, si requiere decisión de producto (no técnica), preguntás al PM antes. Cada problema visto y no arreglado es deuda que reaparece. Excepción legítima: si arreglarlo expande el scope >50% del trabajo original, anotalo como sub-tarea concreta con archivo/líneas (no como handwave) y preguntás al PM si seguimos.

