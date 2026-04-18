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

# Load .env so that DATABASE_URL_ADMIN / ANTHROPIC_API_KEY are available
# when running pytest directly without pre-setting env vars.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(override=False)  # don't override vars already set in the shell
except ImportError:
    pass
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
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
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

    Returns:
        {
          "id": int,                  # parent restaurant id
          "whatsapp_number": str,     # normalized bot_number
          "branches": [
              {"id": int, "whatsapp_number": str, "lat": float, "lon": float},
              ...
          ]
        }
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

    async with pool.acquire() as conn:
        # ── Parent restaurant ─────────────────────────────────────────────────
        existing = await conn.fetchrow(
            "SELECT id FROM restaurants WHERE whatsapp_number = $1",
            bot_number,
        )
        if existing:
            parent_id = existing["id"]
            # Update menu and features to latest seed values
            await conn.execute(
                "UPDATE restaurants SET menu=$2::jsonb, features=$3::jsonb WHERE id=$1",
                parent_id,
                json.dumps(menu),
                json.dumps(base_features),
            )
        else:
            slug = f"e2e-test-{bot_number[-6:]}"
            await conn.execute(
                """
                INSERT INTO restaurants (name, whatsapp_number, address, menu, features, slug)
                VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6)
                ON CONFLICT (slug) DO UPDATE
                  SET menu=$4::jsonb, features=$5::jsonb
                """,
                name,
                bot_number,
                "Calle 93 #13-24, Bogotá (E2E)",
                json.dumps(menu),
                json.dumps(base_features),
                slug,
            )
            parent_id = await conn.fetchval(
                "SELECT id FROM restaurants WHERE whatsapp_number = $1",
                bot_number,
            )

        # Owner user
        owner_email = f"e2e-owner-{parent_id}@mesio.test"
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
            parent_id,
        )

        # Subscription usage
        await conn.execute(
            """
            INSERT INTO subscription_usage (restaurant_id, usage_date, total_tokens, total_invoices)
            VALUES ($1, CURRENT_DATE, 0, 0)
            ON CONFLICT (restaurant_id, usage_date) DO NOTHING
            """,
            parent_id,
        )

        # ── Branches ──────────────────────────────────────────────────────────
        branches = []
        for i in range(num_branches):
            lat, lon = branch_latlons[i] if i < len(branch_latlons) else (4.6, -74.1)
            branch_num_suffix = f"_b{parent_id}{i+1}"
            branch_bot = bot_number + branch_num_suffix

            b_existing = await conn.fetchrow(
                "SELECT id FROM restaurants WHERE whatsapp_number = $1",
                branch_bot,
            )
            branch_features = {
                **base_features,
                "branch_lat": lat,
                "branch_lon": lon,
            }
            branch_slug = f"e2e-test-branch-{parent_id}-{i+1}"
            if b_existing:
                branch_id = b_existing["id"]
                await conn.execute(
                    "UPDATE restaurants SET features=$2::jsonb, latitude=$3, longitude=$4 WHERE id=$1",
                    branch_id,
                    json.dumps(branch_features),
                    lat,
                    lon,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO restaurants
                      (name, whatsapp_number, address, menu, features, slug, parent_restaurant_id, latitude, longitude)
                    VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6, $7, $8, $9)
                    ON CONFLICT (slug) DO UPDATE
                      SET features=$5::jsonb, latitude=$8, longitude=$9
                    """,
                    f"{name} — Sucursal {i+1}",
                    branch_bot,
                    f"Dirección Sucursal {i+1}, Bogotá (E2E)",
                    json.dumps(menu),
                    json.dumps(branch_features),
                    branch_slug,
                    parent_id,
                    lat,
                    lon,
                )
                branch_id = await conn.fetchval(
                    "SELECT id FROM restaurants WHERE whatsapp_number = $1",
                    branch_bot,
                )

            # Subscription usage for branch
            await conn.execute(
                """
                INSERT INTO subscription_usage (restaurant_id, usage_date, total_tokens, total_invoices)
                VALUES ($1, CURRENT_DATE, 0, 0)
                ON CONFLICT (restaurant_id, usage_date) DO NOTHING
                """,
                branch_id,
            )

            branches.append({"id": branch_id, "whatsapp_number": branch_bot, "lat": lat, "lon": lon})

        return {
            "id": parent_id,
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
    Manually drains the webhook_inbox table by running the claim-then-ack
    loop until empty or max_iterations is reached.

    This replicates what inbox_worker.run_worker() does but is controlled
    and synchronous from the test's perspective — no race conditions.

    Args:
        wait_for_first: seconds to wait for at least one row to appear
            before declaring the queue empty. Handles the case where a row
            was just inserted and next_attempt_at == NOW() causes a race.

    Returns number of items processed.
    """
    from app.services import inbox_worker as _iw
    import time as _time

    total_processed = 0
    deadline = _time.monotonic() + wait_for_first

    for iteration in range(max_iterations):
        # Phase 1: claim
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

        if not claimed:
            # On the first iteration, wait a bit in case the row was JUST inserted
            # and next_attempt_at is racing with NOW() in the DB.
            if iteration == 0 and _time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                continue
            # If we've already processed some items, try once more in case
            # the agent created follow-on messages.
            if total_processed > 0 and _time.monotonic() < deadline:
                await asyncio.sleep(0.2)
                continue
            break

        # Phase 2 + 3: dispatch and ack each item
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

            # Phase 3: ack
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

    # Brief pause to let the DB transaction commit and the row become
    # visible to the subsequent fetch_batch SELECT.
    await asyncio.sleep(0.2)

    # Drain inbox so the agent processes this message before the next turn
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


async def truncate_e2e_data(pool: asyncpg.Pool, restaurant_id: int) -> None:
    """
    Truncate volatile tables for a specific restaurant (using DELETE for safety).
    Does NOT truncate the restaurant row itself.
    """
    async with pool.acquire() as conn:
        # Use DELETE instead of TRUNCATE so we only remove data for our test restaurant
        # and don't disturb other data that may exist in a shared test DB.
        await conn.execute(
            "DELETE FROM webhook_inbox WHERE payload->>'bot_number' LIKE $1",
            f"%{restaurant_id}%",
        )
        # For tables with restaurant_id column we can be precise
        for table in [
            "conversations", "carts", "orders", "table_orders",
            "table_sessions", "table_checks", "waiter_alerts", "nps_responses",
        ]:
            try:
                await conn.execute(
                    f"DELETE FROM {table} WHERE restaurant_id = $1",  # noqa: S608
                    restaurant_id,
                )
            except Exception:
                # Table may not have restaurant_id column — skip silently
                pass
        # Also clear webhook_inbox by bot_number pattern
        await conn.execute("DELETE FROM webhook_inbox WHERE payload IS NOT NULL")
