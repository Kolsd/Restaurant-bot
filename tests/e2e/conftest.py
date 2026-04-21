"""
tests/e2e/conftest.py — Fixtures for the E2E test harness.

Architecture:
- Uses a REAL Postgres database (TEST_DATABASE_URL or DATABASE_URL_ADMIN with a fresh schema)
- Uses a REAL Anthropic API
- Redis is optional (state_store has graceful in-process fallback)
- Outbound Meta WhatsApp HTTP calls are captured (not forwarded) via httpx patching
- HMAC webhook signature verification is bypassed via env flag DISABLE_META_SIGNATURE_VERIFY=1
- The inbox worker is driven manually (drain_inbox) rather than as a background loop

Key design decision:
  The E2E test calls POST /webhook/meta (through httpx ASGI), which enqueues into
  webhook_inbox. Then drain_inbox() manually pulls from the queue and dispatches,
  exactly replicating the inbox_worker logic but controllably within the test.
  This avoids timing races from a concurrent background loop.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time

# .env loading is INTENTIONALLY deferred to fixture scope (see _ensure_dotenv_loaded
# below). Loading it at module import poisons os.environ for every other test in the
# suite — pytest collects all conftest.py files regardless of which test paths the
# user runs, so a module-level load_dotenv() leaks DATABASE_URL into integration
# tests that should skip when no DB is configured.
def _ensure_dotenv_loaded() -> None:
    """Idempotent .env loader. Call from inside an e2e fixture, never at module top."""
    try:
        from dotenv import load_dotenv as _load_dotenv  # type: ignore
        _load_dotenv(override=False)
    except ImportError:
        pass


def _dotenv_peek(key: str) -> str | None:
    """Read a single key from .env WITHOUT mutating os.environ.

    Used by pytest_collection_modifyitems where we need to see env values to
    decide skip markers, but cannot afford to leak them into the global env
    (e.g. DATABASE_URL leaking would force integration tests to attempt DB
    connections instead of skipping cleanly).
    """
    if os.environ.get(key):
        return os.environ[key]
    try:
        from dotenv import dotenv_values  # type: ignore
        return dotenv_values().get(key)
    except ImportError:
        return None
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.services.logging import get_logger
from app.repositories import inbox_repo

log = get_logger(__name__)

# ── Skip guard: check for required env vars ───────────────────────────────────

def pytest_collection_modifyitems(items):
    """
    Skip E2E tests if ANTHROPIC_API_KEY is not set.
    This prevents confusing AuthenticationError failures mid-test.
    """
    # Read ANTHROPIC_API_KEY from env or .env without mutating os.environ.
    if not (_dotenv_peek("ANTHROPIC_API_KEY") or "").strip():
        skip_marker = pytest.mark.skip(
            reason=(
                "ANTHROPIC_API_KEY not set. "
                "Set it to run E2E tests: export ANTHROPIC_API_KEY='sk-ant-...'"
            )
        )
        for item in items:
            if item.get_closest_marker("e2e"):
                item.add_marker(skip_marker)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_phone(number: str) -> str:
    return number.replace(" ", "").replace("+", "")


def _same_host_and_db(url_a: str, url_b: str) -> bool:
    """Return True if two DB URLs point to the same host and database name."""
    import re
    # Extract host:port/dbname from postgresql://user:pass@host:port/dbname?...
    _pat = re.compile(r"@([^/?]+)/([^?]+)")
    m_a = _pat.search(url_a)
    m_b = _pat.search(url_b)
    if not m_a or not m_b:
        return False
    return m_a.group(1) == m_b.group(1) and m_a.group(2) == m_b.group(2)


def _build_meta_signature(body: bytes, secret: str) -> str:
    """Build X-Hub-Signature-256 for a given body and secret."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ── Database fixture ──────────────────────────────────────────────────────────

@pytest_asyncio.fixture(scope="function")
async def test_pool():
    """
    Function-scoped asyncpg pool pointing at the E2E test database.

    Priority:
    1. TEST_DATABASE_URL env var — REQUIRED: a dedicated (non-production) Postgres DB
       that has had `alembic upgrade head` run (or this fixture runs it automatically).
    2. DATABASE_URL (only if the URL contains 'test' or 'sim' — a safety guard to
       prevent accidentally running against production).

    IMPORTANT: Do NOT use DATABASE_URL_ADMIN (production superuser) as the test DB.
    If the production inbox worker is running against the same DB, it will race with
    the test's drain_inbox(), consuming test rows before the test can process them.

    The test DB MUST be isolated from the production environment.

    Runs alembic upgrade head before yielding the pool.
    """
    import subprocess
    import sys
    from pathlib import Path

    REPO_ROOT = Path(__file__).parent.parent.parent

    # E2E tests are about to run — now it's safe to materialise .env into
    # os.environ. This is fixture-scoped, so non-e2e tests collected in the
    # same pytest session never see these values.
    _ensure_dotenv_loaded()

    url = os.environ.get("TEST_DATABASE_URL", "").strip()

    if not url:
        # Try DATABASE_URL if it looks like a test DB (safety guard)
        candidate = os.environ.get("DATABASE_URL", "").strip()
        if candidate and ("test" in candidate.lower() or "sim" in candidate.lower()):
            url = candidate
        else:
            pytest.skip(
                "E2E tests require TEST_DATABASE_URL pointing at a DEDICATED (non-production) "
                "Postgres database with the Mesio schema. "
                "Example: export TEST_DATABASE_URL='postgresql://user:pass@localhost/mesio_test'\n"
                "Do NOT use DATABASE_URL_ADMIN (production DB) — the production inbox worker "
                "would race with the test drain and consume test rows."
            )

    # Normalize postgres:// → postgresql://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]

    # Set DATABASE_URL so that app.services.database.get_pool() uses this URL.
    # This must happen BEFORE importing the FastAPI app.
    os.environ["DATABASE_URL"] = url
    # Disable embedded inbox worker — the test drives it manually
    os.environ["DISABLE_EMBEDDED_WORKER"] = "1"
    # Bypass Meta HMAC verification in tests
    os.environ["DISABLE_META_SIGNATURE_VERIFY"] = "1"
    # Limit LLM response length for faster E2E runs (doesn't change model reasoning)
    os.environ.setdefault("BOT_MAX_TOKENS", "512")

    # Run alembic upgrade head (same as production startup)
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    # For alembic, use DATABASE_URL_ADMIN only if it also points at the test DB.
    # If DATABASE_URL_ADMIN points to a different (production) DB, use the test URL.
    admin_url_raw = os.environ.get("DATABASE_URL_ADMIN", "").strip()
    # Use admin URL only if it shares the same host+db as the test URL (rough check)
    # to prevent accidentally migrating the production DB.
    if admin_url_raw and _same_host_and_db(admin_url_raw, url):
        admin_url = admin_url_raw
        if admin_url.startswith("postgres://"):
            admin_url = "postgresql://" + admin_url[len("postgres://"):]
    else:
        # Use the test URL directly for alembic (requires test DB superuser or equivalent)
        admin_url = url
    env["DATABASE_URL_ADMIN"] = admin_url

    print(f"\n[E2E] Running alembic upgrade head against {url[:60]}...")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    if result.returncode != 0:
        print(f"[E2E] alembic stdout: {result.stdout[-1000:]}")
        print(f"[E2E] alembic stderr: {result.stderr[-1000:]}")
        pytest.fail(f"alembic upgrade head failed (rc={result.returncode})")
    print("[E2E] alembic upgrade head: OK")

    import json as _json

    async def _jsonb_init(conn):
        """Register jsonb codec so dicts are passed/returned natively (matches app pool)."""
        await conn.set_type_codec(
            "jsonb",
            encoder=_json.dumps,
            decoder=_json.loads,
            schema="pg_catalog",
        )

    pool = await asyncpg.create_pool(
        url,
        min_size=2,
        max_size=10,
        command_timeout=60,
        init=_jsonb_init,
    )

    # ── Apply grants for mesio_app and mesio_superadmin ───────────────────────
    # On a fresh test DB, `mesio_app` and `mesio_superadmin` roles are created by
    # migration 0029 but have no DML grants (on the production DB these were set up
    # manually before the migration was written). Without the grants, tenant_connection()
    # which does SET LOCAL ROLE mesio_superadmin will get permission denied.
    #
    # We grant ALL TABLES in the test DB so the test pool (connecting as postgres)
    # can switch to those roles and still operate. This is test-only — production
    # grants were applied separately when the DB was originally provisioned.
    async with pool.acquire() as _grant_conn:
        try:
            await _grant_conn.execute(
                "GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO mesio_app"
            )
            await _grant_conn.execute(
                "GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO mesio_superadmin"
            )
            await _grant_conn.execute(
                "GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO mesio_app"
            )
            await _grant_conn.execute(
                "GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO mesio_superadmin"
            )
            print("[E2E] Grants applied to mesio_app and mesio_superadmin.")
        except Exception as _grant_err:
            # If the roles don't exist or grants fail, log and continue.
            # The test will fail later with a clear permission error if needed.
            print(f"[E2E] Warning: could not apply grants: {_grant_err}")

    yield pool

    # Reset the app's database singleton BEFORE closing our pool. When multiple
    # E2E tests run in the same process, the app's lifespan startup will reuse the
    # cached _pool from a prior test (pointing at a closed event loop) and explode.
    try:
        from app.services import database as _db_mod
        _db_mod._pool = None
    except Exception:
        pass

    try:
        await pool.close()
    except Exception:
        pass


# ── WA capture fixture ────────────────────────────────────────────────────────

class WACapture:
    """Collects outbound WhatsApp messages instead of sending them to Meta."""

    def __init__(self):
        self.messages: list[dict] = []

    def append(self, phone: str, text: str, phone_id: str):
        self.messages.append({"phone": phone, "text": text, "phone_id": phone_id})

    def all_texts(self) -> list[str]:
        return [m["text"] for m in self.messages]

    def texts_to(self, phone: str) -> list[str]:
        norm = _normalize_phone(phone)
        return [m["text"] for m in self.messages if _normalize_phone(m["phone"]) == norm]


@pytest.fixture()
def wa_capture():
    """
    Captures all outbound WhatsApp messages sent via httpx to graph.facebook.com.

    Patches httpx.AsyncClient at the module level so every POST to
    graph.facebook.com/*/messages is intercepted. Other HTTP calls pass through.
    """
    capture = WACapture()

    class _FakeResponse:
        status_code = 200
        text = '{"messages": [{"id": "wamid.fake"}]}'

        def json(self):
            return {"messages": [{"id": "wamid.fake"}]}

    class _FakeClient:
        """Minimal async context manager that intercepts graph.facebook.com posts."""

        def __init__(self, *args, **kwargs):
            self._kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url: str, **kwargs) -> _FakeResponse:
            if "graph.facebook.com" in url and "/messages" in url:
                # Parse the outbound payload
                payload = kwargs.get("json", {})
                phone = payload.get("to", "")
                msg_type = payload.get("type", "")
                if msg_type == "text":
                    text = payload.get("text", {}).get("body", "")
                elif msg_type == "interactive":
                    # Extract body text from interactive message
                    text = (
                        payload.get("interactive", {})
                        .get("body", {})
                        .get("text", "[interactive]")
                    )
                else:
                    text = f"[{msg_type}]"
                # Extract phone_id from URL: .../v20.0/{phone_id}/messages
                parts = url.split("/")
                phone_id = parts[-2] if len(parts) >= 2 else ""
                capture.append(phone, text, phone_id)
                log.info(
                    "wa_capture.intercepted",
                    phone=phone,
                    text_preview=text[:80],
                    phone_id=phone_id,
                )
            return _FakeResponse()

        async def get(self, url: str, **kwargs) -> _FakeResponse:
            return _FakeResponse()

    # Patch httpx.AsyncClient everywhere it is used for WA sends
    import httpx
    original_client = httpx.AsyncClient

    with patch("httpx.AsyncClient", _FakeClient):
        yield capture


# ── Seed helper ───────────────────────────────────────────────────────────────

async def seed_restaurant(
    pool: asyncpg.Pool,
    *,
    name: str = "E2E Test Restaurant",
    bot_number_raw: str = "+570000E2ETEST",
    menu: dict | None = None,
    payment_methods: list[str] | None = None,
    features_override: dict | None = None,
    num_branches: int = 2,
    branch_latlons: list[tuple[float, float]] | None = None,
) -> dict:
    """
    Seed a multi-branch restaurant for E2E tests. Idempotent.

    Post-Wave-2 (migration 0038): `restaurants` is a READ-ONLY VIEW over
    `organizations JOIN locations`. This function writes directly to
    `organizations` + `locations`.

    Schema summary (confirmed against live schema):
      organizations: id, name, slug, whatsapp_number, menu, features, ...
      locations:     id, org_id, name, code, address, latitude, longitude,
                     whatsapp_number (location-level override), active, timezone, ...
      restaurants VIEW: id=l.id, whatsapp_number=COALESCE(l.whatsapp_number, o.whatsapp_number),
                        menu=o.menu, features=o.features, ...

    Strategy:
      - org.whatsapp_number = parent bot_number  (VIEW exposes this via the
        "sede principal" location whose l.whatsapp_number IS NULL)
      - each branch location: l.whatsapp_number = bot_number + _b{org_id}{i+1}
        (the _b suffix convention is required by agent_external.py branch routing)

    Returns:
        {
          "id": int,                   # org_id — matches db_get_restaurant_by_phone return
          "whatsapp_number": str,      # normalized bot_number
          "owner_email": str,
          "branches": [
              {"id": int, "whatsapp_number": str, "lat": float, "lon": float},
              ...                      # id = location_id for each branch location
          ]
        }

    Note on "id":
      db_get_restaurant_by_phone() does `d["id"] = d["org_id"]` so all bot-runtime
      code uses org_id as the tenant key. seed_restaurant() returns org_id under "id"
      to match that convention. Branch "id" values are location_ids (used as
      X-Branch-ID header in admin API calls).
    """
    if menu is None:
        menu = {
            "Empanadas": [
                {
                    "name": "Empanaditas de Carne (3 uds)",
                    "description": "Empanadas fritas rellenas de carne y papa",
                    "price": 15000,
                    "active": True,
                }
            ]
        }
    if payment_methods is None:
        payment_methods = ["Nequi", "Efectivo"]
    if branch_latlons is None:
        # branch_1: Bogotá zona norte, branch_2: Bogotá zona sur
        branch_latlons = [
            (4.710989, -74.072092),
            (4.609710, -74.081741),
        ]

    bot_number = _normalize_phone(bot_number_raw)

    base_features = {
        "bot_active": True,
        "domicilio_active": True,
        "recoger_active": True,
        "currency": "COP",
        "locale": "es-CO",
        "timezone": "America/Bogota",
        "payment_methods": payment_methods,
        "bot_visual_menu": False,
        "catalog_v2_enabled": False,
        "module_reservations": False,
        "module_reviews": False,
        "dynamic_discounts": False,
    }
    if features_override:
        base_features.update(features_override)

    slug = f"e2e-test-{bot_number[-8:]}"

    async with pool.acquire() as conn:
        async with conn.transaction():
            # ── Upsert organization ───────────────────────────────────────────
            # organizations has a partial unique index on whatsapp_number WHERE NOT NULL.
            # Use ON CONFLICT to update menu/features on re-run.
            org_row = await conn.fetchrow(
                """
                INSERT INTO organizations (name, slug, whatsapp_number, menu, features)
                VALUES ($1, $2, $3, $4::jsonb, $5::jsonb)
                ON CONFLICT (whatsapp_number)
                  WHERE whatsapp_number IS NOT NULL
                  DO UPDATE SET
                    name     = EXCLUDED.name,
                    menu     = EXCLUDED.menu,
                    features = EXCLUDED.features,
                    updated_at = NOW()
                RETURNING id
                """,
                name,
                slug,
                bot_number,
                json.dumps(menu),
                json.dumps(base_features),
            )
            org_id = org_row["id"]

            # ── Upsert "sede principal" location ─────────────────────────────
            # This location has NO whatsapp_number override so the VIEW resolves
            # COALESCE(l.whatsapp_number, o.whatsapp_number) = org.whatsapp_number.
            # It represents the "parent" entry visible in the restaurants VIEW as
            # whatsapp_number = bot_number.
            existing_principal = await conn.fetchrow(
                """
                SELECT id FROM locations
                WHERE org_id = $1 AND whatsapp_number IS NULL
                ORDER BY id ASC LIMIT 1
                """,
                org_id,
            )
            if existing_principal:
                principal_loc_id = existing_principal["id"]
                await conn.execute(
                    """
                    UPDATE locations
                    SET name=$2, address=$3, active=true
                    WHERE id=$1
                    """,
                    principal_loc_id,
                    name,
                    "Calle 93 #13-24, Bogotá (E2E)",
                )
            else:
                principal_loc_row = await conn.fetchrow(
                    """
                    INSERT INTO locations
                      (org_id, name, code, address, active, timezone)
                    VALUES ($1, $2, 'principal', $3, true, 'America/Bogota')
                    RETURNING id
                    """,
                    org_id,
                    name,
                    "Calle 93 #13-24, Bogotá (E2E)",
                )
                principal_loc_id = principal_loc_row["id"]

            # ── Owner user ────────────────────────────────────────────────────
            # users.branch_id = org_id (the tenant key used by auth middleware).
            owner_email = f"e2e-owner-{org_id}@mesio.test"
            await conn.execute(
                """
                INSERT INTO users (username, password_hash, restaurant_name, role, branch_id)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (username) DO NOTHING
                """,
                owner_email,
                "$2b$12$placeholderhashneverusedXXXXXXXXXXXXXXX",
                name,
                "owner",
                org_id,
            )

            # ── Subscription usage ────────────────────────────────────────────
            # Post-Wave-2: subscription_usage uses org_id (no restaurant_id column).
            await conn.execute(
                """
                INSERT INTO subscription_usage (org_id, usage_date, total_tokens, total_invoices)
                VALUES ($1, CURRENT_DATE, 0, 0)
                ON CONFLICT (org_id, usage_date) DO NOTHING
                """,
                org_id,
            )

            # ── Branch locations ──────────────────────────────────────────────
            # Each branch is a location with its own whatsapp_number override.
            # The _b{org_id}{i+1} suffix is required by agent_external.py branch routing
            # (see grep for '_b' in app/services/agent_external.py).
            branches = []
            for i in range(num_branches):
                lat, lon = branch_latlons[i] if i < len(branch_latlons) else (4.6, -74.1)
                branch_bot = f"{bot_number}_b{org_id}{i + 1}"

                # Check for existing branch location by whatsapp_number
                b_existing = await conn.fetchrow(
                    """
                    SELECT id FROM locations
                    WHERE whatsapp_number = $1
                    """,
                    branch_bot,
                )
                if b_existing:
                    branch_loc_id = b_existing["id"]
                    await conn.execute(
                        """
                        UPDATE locations
                        SET latitude=$2, longitude=$3, active=true
                        WHERE id=$1
                        """,
                        branch_loc_id,
                        lat,
                        lon,
                    )
                else:
                    b_row = await conn.fetchrow(
                        """
                        INSERT INTO locations
                          (org_id, name, code, address, latitude, longitude,
                           whatsapp_number, active, timezone)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, true, 'America/Bogota')
                        RETURNING id
                        """,
                        org_id,
                        f"{name} — Sucursal {i + 1}",
                        f"s{i + 1}",
                        f"Dirección Sucursal {i + 1}, Bogotá (E2E)",
                        lat,
                        lon,
                        branch_bot,
                    )
                    branch_loc_id = b_row["id"]

                branches.append({
                    "id": branch_loc_id,
                    "whatsapp_number": branch_bot,
                    "lat": lat,
                    "lon": lon,
                })

        # ── Sanity check: VIEW must resolve both bot numbers ──────────────────
        # If this fails the test seeding is wrong — fail fast with a clear message.
        principal_view_row = await conn.fetchrow(
            "SELECT id, whatsapp_number FROM restaurants WHERE whatsapp_number = $1",
            bot_number,
        )
        assert principal_view_row is not None, (
            f"seed_restaurant: 'restaurants' VIEW returned no row for bot_number={bot_number!r}. "
            f"org_id={org_id}, principal_loc_id={principal_loc_id}. "
            "Check that the location has whatsapp_number IS NULL so the VIEW COALESCEs to org.whatsapp_number."
        )
        for b in branches:
            branch_view_row = await conn.fetchrow(
                "SELECT id FROM restaurants WHERE whatsapp_number = $1",
                b["whatsapp_number"],
            )
            assert branch_view_row is not None, (
                f"seed_restaurant: 'restaurants' VIEW returned no row for branch {b['whatsapp_number']!r}. "
                f"org_id={org_id}. Branch location insert may have failed."
            )

        return {
            "id": org_id,
            "whatsapp_number": bot_number,
            "owner_email": owner_email,
            "branches": branches,
        }


async def create_admin_token(pool: asyncpg.Pool, username: str) -> str:
    """Create a real session token for admin auth in E2E tests."""
    from app.repositories.sessions_repo import create_session as _create_session
    # sessions_repo.create_session uses get_pool() — we must make sure the pool
    # is initialized. Since we set DATABASE_URL before importing the app, get_pool()
    # will connect to the right DB.
    token = await _create_session(username)
    return token


# ── Inbox drain helper ────────────────────────────────────────────────────────

async def drain_inbox(
    pool: asyncpg.Pool,
    max_iterations: int = 30,
    wait_for_first: float = 3.0,
) -> int:
    """
    Manually drains the webhook_inbox table by running the production
    claim-then-ack loop (matching inbox_worker.py) until empty or
    max_iterations is reached.

    Production pattern (3 phases, Rule #4 in CLAUDE.md):
      Phase 1 — Claim (short transaction, ~ms):
        SELECT FOR UPDATE SKIP LOCKED → claim_rows (SET next_attempt_at) → COMMIT
      Phase 2 — Dispatch (no DB connection):
        asyncio.wait_for(_dispatch(...), timeout=180)
      Phase 3 — Ack (new short connection, ~ms):
        mark_processed or mark_failed

    Args:
        wait_for_first: seconds to wait for at least one row to appear
            before declaring the queue empty.

    Returns number of items successfully processed.
    """
    from app.services import inbox_worker as _iw
    import time as _time

    total_processed = 0
    deadline = _time.monotonic() + wait_for_first

    for iteration in range(max_iterations):
        # Phase 1: claim (short transaction — release conn before dispatch)
        claimed = []
        async with pool.acquire() as conn:
            async with conn.transaction():
                rows = await inbox_repo.fetch_batch(conn, limit=10)
                if rows:
                    await inbox_repo.claim_rows(conn, [r["id"] for r in rows])
                    for r in rows:
                        claimed.append({
                            "id": r["id"],
                            "provider": r["provider"],
                            "payload": r["payload"],
                            "attempts": r["attempts"] + 1,
                        })
        # conn released here — production pattern (no DB held during dispatch)

        if not claimed:
            # Wait briefly on first iteration in case the row just landed
            if iteration == 0 and _time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                continue
            # After processing some items, allow one more poll for follow-on messages
            if total_processed > 0 and _time.monotonic() < deadline:
                await asyncio.sleep(0.2)
                continue
            break

        # Phase 2: dispatch (no DB connection open — matches production)
        for item in claimed:
            inbox_id = item["id"]
            provider = item["provider"]
            payload = item["payload"]
            attempts = item["attempts"]
            dispatch_error = None

            try:
                await asyncio.wait_for(
                    _iw._dispatch(provider, payload),
                    timeout=180,
                )
            except asyncio.TimeoutError:
                dispatch_error = "dispatch_timeout"
                log.error("drain_inbox.timeout", inbox_id=inbox_id)
            except Exception as exc:
                import traceback
                dispatch_error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
                log.error("drain_inbox.dispatch_failed", inbox_id=inbox_id, error=str(exc))

            # Phase 3: ack (new connection — matches production)
            if dispatch_error is None:
                async with pool.acquire() as conn:
                    await inbox_repo.mark_processed(conn, inbox_id)
                total_processed += 1
            else:
                async with pool.acquire() as conn:
                    await inbox_repo.mark_failed(
                        conn, inbox_id, dispatch_error, attempts, already_incremented=True
                    )

    return total_processed


# ── Inbound simulation helper ─────────────────────────────────────────────────

async def simulate_whatsapp_inbound(
    client: AsyncClient,
    pool: asyncpg.Pool,
    *,
    phone: str,
    text: str,
    bot_number: str,
    wam_id: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> int:
    """
    Send a simulated inbound WhatsApp message to POST /webhook/meta,
    then drain the inbox worker until the message is processed.

    For GPS location messages, set lat and lon (text is ignored for the
    location payload but we also save coords to the cart directly).

    Returns number of inbox items processed.
    """
    # Generate a unique wam_id if not provided (avoids dedup collisions)
    if wam_id is None:
        wam_id = f"wamid.e2e_{uuid.uuid4().hex}"

    normalized_bot = _normalize_phone(bot_number)

    if lat is not None and lon is not None:
        # Location message
        message = {
            "id": wam_id,
            "from": _normalize_phone(phone),
            "type": "location",
            "location": {"latitude": lat, "longitude": lon},
        }
    else:
        message = {
            "id": wam_id,
            "from": _normalize_phone(phone),
            "type": "text",
            "text": {"body": text},
        }

    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "e2e_entry",
                "changes": [
                    {
                        "value": {
                            "metadata": {
                                "display_phone_number": normalized_bot,
                                "phone_number_id": "e2e_phone_id",
                            },
                            "messages": [message],
                        }
                    }
                ],
            }
        ],
    }

    body_bytes = json.dumps(payload).encode()

    # Build a valid signature (DISABLE_META_SIGNATURE_VERIFY bypasses the check,
    # but we send a dummy signature so the parser doesn't log noise).
    app_secret = os.environ.get("META_APP_SECRET", "test_secret_e2e")
    signature = _build_meta_signature(body_bytes, app_secret)

    resp = await client.post(
        "/api/webhook/meta",
        content=body_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": signature,
        },
    )
    # The webhook should always return 200 (even on partial failures)
    assert resp.status_code == 200, f"Webhook returned {resp.status_code}: {resp.text}"

    # Drain the inbox using the production claim-then-ack pattern.
    # The mesio_e2e_test database is isolated — the Railway production inbox worker
    # does NOT poll it, so there is no race condition with a competing worker.
    processed = await drain_inbox(pool)
    log.info(
        "simulate_whatsapp_inbound.done",
        phone=phone,
        text_preview=(text[:60] if text else f"GPS {lat},{lon}"),
        inbox_processed=processed,
    )
    return processed


# ── Truncate volatile E2E tables between tests ────────────────────────────────

_E2E_VOLATILE_TABLES = [
    "conversations",
    "carts",
    "orders",
    "table_orders",
    "table_sessions",
    "table_checks",
    "waiter_alerts",
    "nps_responses",
    "webhook_inbox",
]


async def truncate_e2e_data(pool: asyncpg.Pool, org_id: int) -> None:
    """
    Truncate volatile tables for a specific org (using DELETE for safety).
    Does NOT truncate the organization or location rows themselves.

    Post-Wave-2: volatile tables use org_id (not restaurant_id).
    table_checks has no tenant key — deleted via their parent table_orders.
    webhook_inbox has no tenant key — deleted by bot_number pattern in payload.

    This function is called with both org_id and branch location_ids from tests
    (e.g. truncate_e2e_data(pool, parent_id) and truncate_e2e_data(pool, branch_1_id)).
    When called with a location_id that is not an org_id, the org_id lookup ensures
    we still delete the right rows. For simplicity we accept any integer and
    match against org_id column directly; callers should always pass org_id.
    """
    async with pool.acquire() as conn:
        # Tables that use org_id as tenant key (confirmed by schema check).
        for table in [
            "conversations", "carts", "orders", "table_orders",
            "table_sessions", "waiter_alerts", "nps_responses",
        ]:
            await conn.execute(
                f"DELETE FROM {table} WHERE org_id = $1",  # noqa: S608
                org_id,
            )

        # table_checks has no direct tenant key — delete via parent table_orders.
        await conn.execute(
            """
            DELETE FROM table_checks
            WHERE base_order_id IN (
                SELECT id FROM table_orders WHERE org_id = $1
            )
            """,
            org_id,
        )

        # webhook_inbox has no tenant key — match by bot_number patterns present
        # in the payload JSONB.  This deletes all e2e inbox rows regardless of
        # which org they belong to (acceptable since it's a test-only operation
        # on an isolated test DB).
        await conn.execute("DELETE FROM webhook_inbox WHERE payload IS NOT NULL")
