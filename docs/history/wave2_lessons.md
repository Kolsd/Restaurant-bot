# Wave 2 (Org/Location) — Estado Post-Deploy y Aprendizajes

**Fecha aplicado a prod: 2026-04-18.** Esta sección documenta el estado post-migración y las lecciones aprendidas durante el despliegue (~8-10 horas de iteración reactiva). Las **reglas vivas** están en `CLAUDE.md`. Este archivo es referencia histórica.

---

## Estado actual del schema (post-0038, 2026-04-19)

- **Tablas nuevas canónicas:** `organizations` (tenant) + `locations` (sede). Schema canónico vive en migraciones 0034-0038 + 0057.
- **`restaurants` es ahora una VIEW read-only** sobre `locations JOIN organizations`. Cada fila de la VIEW representa una Location con los datos de su Org injerados. `id` de la VIEW == `location_id`.
- **VIEW post-0038**: NO incluye `parent_restaurant_id` ni `is_primary`. Sí incluye `subscription_status` y `subscription_plan`.
- **`restaurants_deprecated`** — DROPPED en 0038. Para restaurar datos pre-Wave-2, restorá de backup pre-0038.
- **`_migration_restaurant_to_location`** — DROPPED en 0038 (mapping table retirada).
- **`locations.is_primary`** — DROPPED en 0038. Cada location es peer; no hay "primary". Para "default location" usar `ORDER BY id ASC LIMIT 1`.
- **`parent_restaurant_id`** — DROPPED en 0038. Code que lo lee obtiene `None`.
- **`restaurant_id` column** — DROPEADA de las 33 tablas RLS en 0037.
- **`org_id` + `location_id`** — las columnas canónicas. `org_id` es el tenant key. `location_id` es la sede operativa.
- **RLS:** policy `org_isolation` (por `org_id`) activa en las 33 tablas + FORCE RLS. Policy legacy `tenant_isolation` DROPPED en 0037.
- **Triggers auto-populate:** DROPPED en 0037. App code debe setear `org_id` y `location_id` explícitamente en INSERTs.
- **Migración head actual:** ver `alembic heads`. Cadena Wave-2: `0036 → 0037b → 0037c → 0037d → 0037 → 0038`. Posterior: `0039 → 0049` ("No-v2"), `0050-0053`, `0054` (drop tip_distributions).
- **billing_config** — en `organizations.billing_config` (0037c), expuesto en la VIEW.
- **location_id nullable** en 17 tablas operativas (0037d, post-0054).
- ~~`legacy_restaurant_id` columna~~ — DROPPED por migración 0041 (2026-04-19).
- ~~`tip_distributions` tabla~~ — DROPPED por migración 0054 (2026-04-27).

## Forward guards contra reintroducción

Tests AST-walking que fallan CI si alguien reintroduce símbolos dropeados:
- **`tests/test_no_parent_restaurant_id_sql.py`** — bloquea `WHERE/JOIN/SELECT parent_restaurant_id`.
- **`tests/test_no_is_primary_sql.py`** — bloquea `WHERE/ORDER BY/INSERT is_primary`.

Si necesitás re-introducir alguno de estos symbols, **revertir los guards es señal de que estás haciendo algo mal** — el modelo Wave-2 nativo no los soporta.

## Rehearsal infrastructure

- **Script:** `scripts/rehearsal_railway.py` — pg_dump prod → restore test → alembic upgrade head → 14 smoke checks → reporta PASS/FAIL.
- **Variables:** `PROD_DATABASE_URL` + `TEST_DATABASE_URL` seteadas. `nixpacks.toml` provee postgresql_17 para pg_dump.
- **Uso pragmático hoy**: validación manual contra `TEST_DATABASE_URL` (correr migración + suite completa) antes de push a main.

## ai_sim — smoke funciona, E2E requiere presupuesto

- `python run_ai_sim.py --smoke` → valida plumbing sin Anthropic.
- Full E2E (20 escenarios) requiere `ANTHROPIC_API_KEY` + budget (~$2-5 por corrida).
- Smoke valida: seed org + tenant_scope resolve + snapshot_db_state + truncate_test_data + empty post-truncate.

## Errores recurrentes que ya vimos (y sus fixes)

Lecciones que se fueron ganando a lo largo de la migración. Aplicar PROACTIVAMENTE antes de escribir código nuevo:

1. **Alembic `version_num VARCHAR(32)`** — cualquier revision_id > 32 chars crashea el UPDATE final. Usar IDs cortos (ej. `0034_org_locations`).

2. **UPDATE target alias dentro de FROM-clause JOIN** — Postgres rechaza `UPDATE t ... FROM x JOIN y ON y.col = t.col` porque `t` no es visible en el JOIN ON. Usar CTE intermedia que resuelve el mapping, luego UPDATE usando solo ctid de la CTE.

3. **`ON CONFLICT` con índice UNIQUE parcial** — no funciona sin especificar el predicado. Si el índice es `... WHERE col IS NOT NULL`, hay que hacer `ON CONFLICT (col) WHERE col IS NOT NULL`. Alternativa: check de existencia antes del INSERT.

4. **`SET LOCAL ROLE` no persiste en asyncpg sin transacción** — cada `execute()` en autocommit es su propia TX. `SET LOCAL` se pierde. Envolver en `async with conn.transaction():`.

5. **`::tipo` cast confunde parameter substitution** — SQLAlchemy `text()` con `:param::regclass` o `:param::jsonb` deja el `:param` literal en la query final → `psycopg2.errors.SyntaxError`. Visto con `regclass` (Wave 2) y de nuevo con `jsonb` en `0071_demo_seed` (2026-05-06). Usar `CAST(:param AS tipo)` siempre. Para `regclass` específicamente, alternativa: JOIN explícito a `pg_class` por nombre.

6. **Shell `$$` expande a PID** — los bloques `DO $$ BEGIN ... END $$;` de PL/pgSQL se rompen si pasan por bash con interpolación. Pasar SQL por stdin (`psql -f -` con `input=sql`), no con `-c "..."`.

7. **pg_dump version mismatch** — Railway Postgres es v17, el apt default de Ubuntu 24 es v16. En nixpacks.toml usar `nixPkgs = ["python312", "gcc", "postgresql_17"]`. nixPkgs REEMPLAZA los packages default, hay que re-incluir python + gcc.

8. **Orphan `restaurant_id` después de tenant borrado** — producción tenía filas con `restaurant_id` apuntando a un tenant eliminado. Migraciones de backfill deben auto-dedupear / auto-delete esas filas con log de advertencia, no crashear. Ceiling de seguridad: si hay >100 orphans, sí crashear.

9. **Duplicados aparecidos entre drop/recreate de UNIQUE** — durante downgrades encadenados, el app puede insertar duplicados en la ventana sin constraint. Migraciones de recuperación deben auto-dedupear con `ROW_NUMBER() OVER (PARTITION BY ...)` manteniendo MAX(id).

10. **Railway deploy == `alembic upgrade head` siempre corre** — un archivo `.deferred` (extensión no-`.py`) es la única forma de "esconder" una migración del upgrade automático.

11. **Grant USAGE on schema public** — después de `DROP SCHEMA public CASCADE; CREATE SCHEMA public;`, se pierde el `GRANT USAGE ON SCHEMA public TO PUBLIC` default. Sin eso, grants table-level no alcanzan. Siempre re-grant después de recrear schema.

12. **Variables ambiguas cuando Railway linkea múltiples DBs** — al linkear 2 Postgres a un mismo servicio, `DATABASE_URL` puede resolverse a cualquiera. Usar nombres explícitos (`PROD_DATABASE_URL`, `TEST_DATABASE_URL`) + anti-swap guard que verifica row counts antes de cualquier escritura.

13. **Multiple alembic heads cuando dos sprints en paralelo** — `0070_plan_limits` y `0071_demo_seed` ambos pusieron `down_revision = "0069_password_reset_tokens"` y al mergear a main Railway crasheó con "Multiple head revisions are present". Antes de mergear PRs que tocan `alembic/versions/`, correr `alembic heads` localmente — debe retornar 1 línea. Si retorna 2+, agregar merge migration no-op (ver `0072_merge_plan_limits_demo_seed.py`) con `down_revision = ("rev_a", "rev_b")` y `upgrade/downgrade` vacíos. (2026-05-06)

14. **`conn.execute("string")` falla en SQLAlchemy 2.0** — `op.execute("...")` SÍ acepta strings raw (alembic los convierte) pero `op.get_bind().execute(...)` requiere `sa.text(...)`. Visto en `0071_demo_seed` upgrade(): `AttributeError: 'str' object has no attribute '_execute_on_connection'`. Patrón canónico: `import sqlalchemy as sa` + `conn.execute(sa.text("..."))` (ver `0034_create_organizations_locations.py`). (2026-05-06)

## Lección estratégica principal

**No ser reactivo.** La migración Wave 2 costó ~8 horas de iteración principalmente por pushear sin haber pensado proactivamente qué podría fallar. La regla:

Antes de cualquier `git push` que toque migraciones, schema, o deploys:
1. Leer mentalmente cada línea del código que se modifica.
2. Pensar "¿qué supuestos estoy haciendo?" y listar los 3-5 principales.
3. Verificar cada supuesto contra el código real antes de asumir.
4. Considerar: ¿qué pasa si corre en un estado distinto al esperado? (DB a mitad de migración, env var faltante, tool ausente).
5. Si el deploy afecta prod, ¿está el rollback probado?

Para migraciones grandes: **staging rehearsal obligatorio ANTES** de tocar prod. `scripts/rehearsal_railway.py` existe exactamente para esto.
