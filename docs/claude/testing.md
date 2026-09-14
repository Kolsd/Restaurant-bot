# Test discipline, integration fixture and frontend lint

> Moved verbatim from CLAUDE.md (2026-09-12) so it isn't loaded on every turn.

## Test Discipline + Frontend Lint

Live testing and lint rules. Detail on the original "No-v2 sprint" that introduced them is in [docs/history/sprints.md](docs/history/sprints.md).

### Frontend lint — `scripts/lint_frontend.py` + `tests/test_frontend_lint.py`

Script auto-invoked as a pytest test. Six checks:
1. **MOCK** — `mock|fake|dummy|lorem|ipsum` keywords in JS (`mesio-demo-*` files and `dashboard-demo-mesio.js` are exempt)
2. **TODO** — `TODO|FIXME|XXX|HACK` markers in admin JS (force explicit resolution)
3. **FETCH** — every `fetch('/api/…')` checked against registered FastAPI routes (segment-match supports `{param}` + string concat `'/api/x/' + id`)
4. **HTML-SEED (names)** — hardcoded Spanish names in admin HTML (`María`, `Carlos`, etc.)
5. **HTML-SEED (money)** — `$X.YM` / `$Nk` literals in admin HTML content
6. **PAGE-CONTRACTS** — load-bearing action buttons + required fetches per operational page. Catches "page renders blank with no buttons" (real regression, 2026-05-05, in `courier.html`).

Suppress with `// lint-allow: reason` (JS) or `<!-- lint-allow: reason -->` (HTML). **PAGE-CONTRACTS cannot be suppressed** — if you rename a button, update the contract, don't silence it.

Contracts declared in the `PAGE_CONTRACTS` dict in `scripts/lint_frontend.py`. Pages covered: `courier.html`, `waiter.html`, `cashier.html`, `kitchen.html`, `bar.html`, `staff-hq.html`. If you add a new operational page, add its contract.

Run: `python scripts/lint_frontend.py` or `pytest tests/test_frontend_lint.py`. CI must fail on regression.

### Integration test pattern against `TEST_DATABASE_URL` (correct fixture)

Sonnet agents wrote broken fixtures on their first pass. The validated pattern that works:

```python
@pytest.fixture                       # function-scoped — module/session scope gives "Future attached to different loop"
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

        # get_pool is async → the mock MUST be an async def (not lambda: shim)
        async def _fake_get_pool():
            return shim
        monkeypatch.setattr(db_module, "get_pool", _fake_get_pool)
        # do NOT patch app.services.tenant_db.get_pool — it doesn't exist as an attribute (lazy-imported)

        tx = conn.transaction()
        await tx.start()
        try:
            # CRITICAL: without this, postgres (superuser) bypasses FORCE RLS and the
            # cross-tenant isolation tests "pass" without testing anything real.
            await conn.execute("SET LOCAL ROLE mesio_app")
            yield proxy
        finally:
            await tx.rollback()        # never "raise PostgresError" — pytest counts it as an error
```

The 3 minimum cases per endpoint (sprint discipline):
1. **Empty tenant** — a new org with 0 data returns zeros/empty arrays, NOT a 500
2. **Aggregate correctness** — seed N orders worth $Y, assert SUM == N·Y
3. **Tenant isolation** — scope A sees data A, scope B sees data B, never cross-over

Forbidden in new tests:
- `assert response.status_code == 200` as the only assertion
- Mocking the whole repo (that tests the handler, not the aggregate)
- Copying the docstring's shape without verifying the SQL actually produces it
- `assert "key" in response.json()` without checking the value

Reference files: `tests/test_loyalty_aggregates.py`, `tests/test_loyalty_campaigns.py`, `tests/test_stats_aggregates.py`.

### Gotchas found during the sprint (for next time)

1. **Parallel worktrees** sometimes don't create a worktree — some agents write straight to main. Verify `git worktree list` post-completion.
2. **`loyalty_customers` vs `customer_profiles`**: different columns. `loyalty_customers` has `points_balance/total_earned/total_redeemed`; `customer_profiles` has `total_spent/total_orders`. One agent mixed them up in its tests.
3. **`orders.id` has no DEFAULT** — use `gen_random_uuid()::text` in test INSERTs.
4. **`orders.subtotal NUMERIC NOT NULL`** — required on INSERTs, don't forget it.
5. **asyncpg + tz**: `orders.created_at` is `TIMESTAMP WITHOUT TIME ZONE`. Passing `datetime.now(timezone.utc)` as a param is rejected with `InvalidParameterValue`. Use `datetime.utcnow().replace(tzinfo=None)` or strip tzinfo in the proxy.
6. **Race on time windows**: an `INSERT orders (..., NOW())` can fall outside the `created_at < $upper_bound` window if Python computed `$upper_bound` before the INSERT committed. Insert with `NOW() - INTERVAL '2 minutes'` as a margin.
7. **The `WITH CHECK` policy requires the GUC to be set**: with `SET LOCAL ROLE mesio_app` active, an INSERT into an RLS-protected table without first setting `app.org_id` fires `InsufficientPrivilegeError`. Call `_set_org_scope(conn, org_id)` before every seed.

### Documented TODOs (not hidden, nullable fields)

Agent 2 (stats_repo) left 6 metrics as `null` in `db_branches_comparison` because the data sources don't exist yet: food cost %, payroll/sales cost, 12-month staff turnover, tables/day turnover, per-location YoY growth, confirmed-reservation rate per location. When those sources are added, the endpoints are already wired.

Agent 3 (loyalty_repo) left 2:
- `roi_multiple` = null (missing campaign spend tracking — future optional field)
- `birthdays` segment count = 0 if the schema has no birthday column (conditional)
