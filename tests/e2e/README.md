# E2E Test Harness — Pickup Lifecycle

## What this tests

One test exercises the full pickup order lifecycle against real infrastructure:
real Postgres, real Anthropic API, real RLS enforcement, real inbox worker dispatch.

## Prerequisites

```bash
# Required — must be a DEDICATED test DB, NOT production
export TEST_DATABASE_URL="postgresql://user:pass@host/mesio_test"
export ANTHROPIC_API_KEY="sk-ant-..."

# Optional (state_store has graceful in-process fallback without Redis)
export REDIS_URL="redis://localhost:6379"

# Auto-set by conftest.py (do NOT set manually)
# DISABLE_EMBEDDED_WORKER=1
# DISABLE_META_SIGNATURE_VERIFY=1
```

**CRITICAL:** `TEST_DATABASE_URL` must point to an isolated test database with **no
production inbox worker running against it**. If the production Railway worker is
connected to the same DB, it will consume test inbox rows before the test drain
can process them, causing false failures.

The test DB must have `alembic upgrade head` applied (conftest.py runs it automatically).

### Quick local test DB setup

```bash
createdb mesio_test
export TEST_DATABASE_URL="postgresql://postgres@localhost/mesio_test"
alembic upgrade head
```

## How to run

```bash
# Run only E2E tests
pytest tests/e2e/test_pickup_lifecycle.py -v -s -m e2e

# Skip E2E tests in normal CI
pytest -m "not e2e"
```

## Expected cost per run

~$0.15–0.25 of Anthropic API credits (4 multi-turn LLM calls with tool use).

## Expected duration

60–120 seconds (dominated by LLM API latency across 4 conversation turns).

## Architecture decisions

- **No background worker loop**: `DISABLE_EMBEDDED_WORKER=1` + manual `drain_inbox()` gives deterministic turn-by-turn control.
- **WA capture via httpx patch**: All `httpx.AsyncClient` POST calls to `graph.facebook.com` are intercepted. No real Meta calls made.
- **Signature bypass**: `DISABLE_META_SIGNATURE_VERIFY=1` skips HMAC check so tests can POST to `/webhook/meta` without a real Meta secret.
- **Truncate-not-rollback**: `agent.py` commits through its own pool connections, so transaction rollback cannot undo its work. DELETE-based cleanup runs per test instead.
- **Dedicated test DB**: The test DB must not be shared with any running production inbox worker. Otherwise the production worker races with `drain_inbox()` and grabs rows before the test can.
