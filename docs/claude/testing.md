# Disciplina de tests, fixture de integración y lint frontend

> Movido verbatim desde CLAUDE.md (2026-09-12) para no cargarlo en cada turno.

## Test Discipline + Frontend Lint

Reglas de testing y lint vivas. El detalle del "No-v2 sprint" original que las introdujo está en [docs/history/sprints.md](docs/history/sprints.md).

### Lint frontend — `scripts/lint_frontend.py` + `tests/test_frontend_lint.py`

Script auto-invocado como pytest test. Seis checks:
1. **MOCK** — keywords `mock|fake|dummy|lorem|ipsum` en JS (archivos `mesio-demo-*` y `dashboard-demo-mesio.js` exentos)
2. **TODO** — marcadores `TODO|FIXME|XXX|HACK` en admin JS (fuerzan resolución explícita)
3. **FETCH** — cada `fetch('/api/…')` contra rutas FastAPI registradas (segment-match soporta `{param}` + string concat `'/api/x/' + id`)
4. **HTML-SEED (nombres)** — nombres españoles hardcoded en admin HTML (`María`, `Carlos`, etc.)
5. **HTML-SEED (dinero)** — literales `$X.YM` / `$Nk` en contenido HTML admin
6. **PAGE-CONTRACTS** — botones de acción load-bearing + fetches obligatorios por página operacional. Catches "página renderiza plana sin botones" (regresión real 2026-05-05 en `domiciliario.html`).

Supresión con `// lint-allow: razón` (JS) o `<!-- lint-allow: razón -->` (HTML). **PAGE-CONTRACTS NO permite supresión** — si renombrás un botón hay que actualizar el contrato, no silenciarlo.

Contratos declarados en `PAGE_CONTRACTS` dict en `scripts/lint_frontend.py`. Páginas cubiertas: `domiciliario.html`, `mesero.html`, `caja.html`, `kitchen.html`, `bar.html`, `staff-hq.html`. Si agregás una página operacional nueva, agregar su contrato.

Correr: `python scripts/lint_frontend.py` o `pytest tests/test_frontend_lint.py`. CI debe fallar si hay regresión.

### Patrón de tests integración contra `TEST_DATABASE_URL` (fixture correcto)

Los agentes Sonnet escribieron fixtures rotas en la primera pasada. El patrón validado que funciona:

```python
@pytest.fixture                       # function-scoped — módulo/session da "Future attached to different loop"
async def raw_pool():
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=3)
    try:
        yield pool
    finally:
        await pool.close()

@pytest.fixture
async def db_conn(raw_pool, monkeypatch):
    from app.services import database as db_module

    async with raw_pool.acquire() as conn:
        proxy = _ConnProxy(conn)
        shim = _PoolShim(proxy)

        # get_pool es async → el mock DEBE ser async def (no lambda: shim)
        async def _fake_get_pool():
            return shim
        monkeypatch.setattr(db_module, "get_pool", _fake_get_pool)
        # NO patchear app.services.tenant_db.get_pool — no existe como atributo (se importa lazy)

        tx = conn.transaction()
        await tx.start()
        try:
            # CRÍTICO: sin esto, postgres (superuser) bypasea FORCE RLS y los tests
            # de aislamiento cross-tenant "pasan" sin probar nada real.
            await conn.execute("SET LOCAL ROLE mesio_app")
            yield proxy
        finally:
            await tx.rollback()        # nunca "raise PostgresError" — pytest lo cuenta como error
```

Los 3 casos mínimos por endpoint (discipline de sprint):
1. **Empty tenant** — nuevo org con 0 data devuelve ceros/arrays vacíos, NO 500
2. **Aggregate correctness** — sembrar N órdenes por $Y, asserting el SUM == N·Y
3. **Tenant isolation** — scope A ve data A, scope B ve data B, nunca cross-over

Prohibidos en tests nuevos:
- `assert response.status_code == 200` como única aserción
- Mock del repo completo (eso prueba el handler, no el agregado)
- Copiar shape del docstring sin verificar que SQL lo produce
- `assert "key" in response.json()` sin checkear valor

Reference files: `tests/test_loyalty_aggregates.py`, `tests/test_loyalty_campaigns.py`, `tests/test_stats_aggregates.py`.

### Gotchas descubiertas durante el sprint (para la próxima vez)

1. **Worktrees paralelos** a veces no crean worktree — algunos agents escriben directamente al main. Verificar `git worktree list` post-completion.
2. **`loyalty_customers` vs `customer_profiles`**: columnas diferentes. `loyalty_customers` tiene `points_balance/total_earned/total_redeemed`; `customer_profiles` tiene `total_spent/total_orders`. Un agent las confundió en sus tests.
3. **`orders.id` no tiene DEFAULT** — usar `gen_random_uuid()::text` en INSERTs de tests.
4. **`orders.subtotal NUMERIC NOT NULL`** — obligatorio en INSERTs, no olvidar.
5. **asyncpg + tz**: `orders.created_at` es `TIMESTAMP WITHOUT TIME ZONE`. Passing `datetime.now(timezone.utc)` como param rechaza con `InvalidParameterValue`. Usar `datetime.utcnow().replace(tzinfo=None)` o strip tzinfo en proxy.
6. **Carrera en windows de tiempo**: un `INSERT orders (..., NOW())` puede caer fuera de la ventana `created_at < $upper_bound` si Python computó `$upper_bound` antes de que el INSERT committeara. Insertar con `NOW() - INTERVAL '2 minutes'` como margen.
7. **`WITH CHECK` policy exige GUC seteado**: con `SET LOCAL ROLE mesio_app` activo, INSERT en tabla RLS-protegida sin haber setear `app.org_id` primero dispara `InsufficientPrivilegeError`. Llamar `_set_org_scope(conn, org_id)` antes de cada seed.

### TODOs documentados (no ocultos, campos nullables)

Agent 2 (stats_repo) dejó 6 métricas como `null` en `db_branches_comparison` porque las fuentes de data no existen todavía: food cost %, costo nómina/ventas, rotación personal 12m, rotación mesas/día, crecimiento YoY per-location, tasa de reserva confirmada per-location. Cuando se agreguen las fuentes, los endpoints ya están cableados.

Agent 3 (loyalty_repo) dejó 2:
- `roi_multiple` = null (falta tracking de campaign spend — campo opcional futuro)
- `birthdays` segment count = 0 si el schema no tiene columna de cumpleaños (condicional)

