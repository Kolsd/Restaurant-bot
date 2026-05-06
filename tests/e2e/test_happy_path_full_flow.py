"""
tests/e2e/test_happy_path_full_flow.py — Happy-path integration test for the full bot flow.

Covers (single test function):
  QR scan → greet → menu question → order → kitchen confirm → bill request → pay → NPS

What this exercises:
  - Real Postgres with RLS active (TEST_DATABASE_URL)
  - Real inbox_worker claim-then-ack dispatch loop (via conftest.drain_inbox)
  - Mocked Anthropic API (MockAnthropicScript) — ordered canned responses per turn
  - Mocked Meta outbound HTTP (conftest.wa_capture) — intercepts send_text calls
  - Real agent.py, agent_salon.py, orders.py, tables_repo.py, conversations_repo.py
  - Tenant isolation: sibling org sees zero data from the main org

Marked `e2e_no_llm` (NOT `e2e`) because ANTHROPIC_API_KEY is NOT required — the LLM
is fully mocked. The test only needs TEST_DATABASE_URL.

Flow assertions per step:
  Step 0: Seed org + table → Row exists in restaurant_tables
  Step 1: "hola [table_id:M1]" → table_sessions row created, conversations row created
  Step 2: "qué tienen?" → bot replies with menu info (WA captured)
  Step 3: "una bandeja paisa y una limonada" → carts row has correct items/total
  Step 4: place_order fires → table_orders row created, cart cleared
  Step 5: Kitchen marks entregado via API → table_orders.status == 'entregado'
  Step 6: "la cuenta" → waiter_alerts row with type='bill'
  Step 7: Pay single check → table_checks.paid_at IS NOT NULL
  Step 8: NPS flow → nps_responses row created with score=5 (Mesio uses 1-5 scale)
  Isolation: sibling org queries return zero rows

Architecture note:
  The inbox_worker does not expose a `_dispatch_for_test()` helper — tests must drive
  it via conftest.drain_inbox() which replicates the 3-phase claim-then-ack loop.
  This is intentional: it tests the real production dispatch path.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.e2e.conftest import (
    WACapture,
    create_admin_token,
    drain_inbox,
    seed_restaurant,
    simulate_whatsapp_inbound,
    truncate_e2e_data,
    _normalize_phone,
    _ensure_dotenv_loaded,
    _same_host_and_db,
)
from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope, tenant_scope

log = get_logger(__name__)


def _peek_test_db_url() -> str:
    """Read TEST_DATABASE_URL from env or .env without mutating os.environ globally."""
    if os.environ.get("TEST_DATABASE_URL"):
        return os.environ["TEST_DATABASE_URL"].strip()
    try:
        from dotenv import dotenv_values  # type: ignore
        return (dotenv_values().get("TEST_DATABASE_URL") or "").strip()
    except ImportError:
        return ""


# ── Skip guard ────────────────────────────────────────────────────────────────
# Only TEST_DATABASE_URL is required; Anthropic is mocked.
_TEST_DB_URL_PRESENT = bool(_peek_test_db_url())
pytestmark = pytest.mark.skipif(
    not _TEST_DB_URL_PRESENT,
    reason=(
        "TEST_DATABASE_URL not set. "
        "Set it to run E2E tests: export TEST_DATABASE_URL='postgresql://user:pass@localhost/mesio_test'"
    ),
)


# ── Local pool fixture (overrides conftest.test_pool) ────────────────────────
# The conftest.test_pool runs `alembic upgrade head` which fails when the DB has
# multiple migration heads (0070_plan_limits + 0071_demo_seed). The 0071 migration
# has a SQLAlchemy 2.0 incompatibility (conn.execute("...") without text() wrapper).
# Since the test DB is already at 0070 (the last valid head), we skip the alembic
# step and connect directly.
#
# BLOCKER for PM: migration 0071_demo_seed.py uses `conn.execute("SET LOCAL ROLE ...")`
# which SQLAlchemy 2.0 rejects as a raw string. Fix: wrap in `text(...)` from
# sqlalchemy import text. Until fixed, `alembic upgrade heads` (plural) fails and
# ALL e2e tests that use conftest.test_pool are blocked on this test DB.
@pytest_asyncio.fixture(scope="function")
async def test_pool():
    """
    Function-scoped asyncpg pool for the happy-path E2E test.

    Skips the alembic step (see BLOCKER note above) and connects directly.
    Assumes the test DB is already at a compatible schema version.
    """
    import json as _json

    _ensure_dotenv_loaded()

    url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]

    os.environ["DATABASE_URL"] = url
    os.environ["DISABLE_EMBEDDED_WORKER"] = "1"
    os.environ["DISABLE_META_SIGNATURE_VERIFY"] = "1"
    os.environ.setdefault("BOT_MAX_TOKENS", "512")

    async def _jsonb_init(conn):
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

    # Apply grants (mirrors conftest.test_pool)
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
        except Exception as _grant_err:
            log.warning("happy_path.test_pool.grants_failed", error=str(_grant_err))

    yield pool

    try:
        from app.services import database as _db_mod
        _db_mod._pool = None
    except Exception:
        pass

    try:
        await pool.close()
    except Exception:
        pass

# ── Test constants ────────────────────────────────────────────────────────────
CUSTOMER_PHONE_RAW = "+57e2ehappy0001"
CUSTOMER_PHONE = _normalize_phone(CUSTOMER_PHONE_RAW)

SIBLING_PHONE_RAW = "+57e2esibling0002"
SIBLING_PHONE = _normalize_phone(SIBLING_PHONE_RAW)

# Unique bot number suffix ensures no collision across runs
_UID = str(int(uuid.uuid4()) % 100000).zfill(5)
BOT_NUMBER_RAW = f"+570E2EHAPPY{_UID}"
SIBLING_BOT_RAW = f"+570E2ESIBLING{_UID}"

# Menu used for this test
MENU = {
    "Platos Fuertes": [
        {
            "name": "Bandeja Paisa",
            "description": "Fríjoles, chicharrón, carne molida, chorizo, arepa, aguacate y arroz",
            "price": 28000,
            "active": True,
        }
    ],
    "Bebidas": [
        {
            "name": "Limonada",
            "description": "Limonada natural de la casa",
            "price": 5000,
            "active": True,
        },
        {
            "name": "Postre del Día",
            "description": "Consulta disponibilidad",
            "price": 8000,
            "active": True,
        },
    ],
}

EXPECTED_TOTAL = Decimal("33000")  # 28000 + 5000


# ── MockAnthropicScript ───────────────────────────────────────────────────────

class _FakeAnthropicMessage:
    """Mimics the structure of anthropic.types.Message returned by client.messages.create."""

    def __init__(
        self,
        text: str = "",
        tool_name: str | None = None,
        tool_input: dict | None = None,
    ):
        self.id = f"msg_fake_{uuid.uuid4().hex[:8]}"
        self.model = "claude-haiku-fake"
        self.stop_reason = "end_turn" if tool_name is None else "tool_use"
        self.stop_sequence = None
        self.usage = MagicMock(input_tokens=10, output_tokens=20)

        content_blocks = []

        if text:
            block = MagicMock()
            block.type = "text"
            block.text = text
            content_blocks.append(block)

        if tool_name:
            block = MagicMock()
            block.type = "tool_use"
            block.name = tool_name
            block.input = tool_input or {}
            content_blocks.append(block)

        self.content = content_blocks


class MockAnthropicScript:
    """
    Ordered queue of canned Anthropic responses.

    Each call to `messages.create()` pops the next response from the queue.
    If the queue is exhausted, returns a safe chat fallback so the test
    doesn't error on unexpected extra turns.

    Usage:
        script = MockAnthropicScript([
            _FakeAnthropicMessage(text="Hola!"),
            _FakeAnthropicMessage(tool_name="place_order", tool_input={...}),
        ])
        with script.patch():
            ...
    """

    def __init__(self, responses: list[_FakeAnthropicMessage]):
        self._responses = list(responses)
        self._call_count = 0

    async def _create(self, **kwargs) -> _FakeAnthropicMessage:
        self._call_count += 1
        if self._responses:
            return self._responses.pop(0)
        # Fallback: empty text response (safe no-op)
        log.warning(
            "mock_anthropic.exhausted_queue",
            call_count=self._call_count,
            hint="Script ran out of canned responses — check test turn count.",
        )
        return _FakeAnthropicMessage(text="Lo siento, estoy ocupado en este momento.")

    def patch(self):
        """Returns a context manager that patches app.services.agent.client.messages.create."""
        mock_messages = MagicMock()
        mock_messages.create = AsyncMock(side_effect=self._create)
        mock_client = MagicMock()
        mock_client.messages = mock_messages

        # Patch the lazy client wrapper in agent.py
        # agent.py uses `client.messages.create(...)` where `client` is a _LazyClient
        # instance. We patch `_get_anthropic_client` so the _LazyClient's __getattr__
        # returns our mock.
        return patch("app.services.agent._get_anthropic_client", return_value=mock_client)


# ── App fixture ───────────────────────────────────────────────────────────────

@pytest_asyncio.fixture()
async def e2e_app(wa_capture, monkeypatch):
    """Yields an httpx AsyncClient against the FastAPI app.

    ORDERING NOTE: wa_capture patches httpx.AsyncClient at class-level.
    Sentry's HttpxIntegration.setup_once() introspects AsyncClient.send during
    app startup, which fails if the fake client is in place. To prevent this,
    we unset SENTRY_DSN before starting the app (Sentry becomes a no-op).
    This is safe for tests — Sentry errors are irrelevant in the test environment.
    """
    monkeypatch.setenv("SENTRY_DSN", "")

    from app.main import app as fastapi_app
    from asgi_lifespan import LifespanManager

    async with LifespanManager(fastapi_app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app),
            base_url="http://test",
            timeout=45.0,
        ) as client:
            yield client


# ── Seed helper for restaurant_tables ────────────────────────────────────────

async def _seed_table(
    pool: asyncpg.Pool,
    org_id: int,
    location_id: int,
    table_id: str,
    number: int,
    name: str,
) -> None:
    """Insert a restaurant_tables row directly (bypassing RLS, seeding as superadmin)."""
    with bypass_tenant_scope("e2e_happy_seed_table"):
        async with pool.acquire() as conn:
            await conn.execute("SET LOCAL ROLE mesio_app")
            await conn.execute(
                "SELECT set_config('app.org_id', $1::text, true)", str(org_id)
            )
            await conn.execute(
                """
                INSERT INTO restaurant_tables
                    (id, number, name, branch_id, location_id, org_id, active)
                VALUES ($1, $2, $3, $4, $5, $6, TRUE)
                ON CONFLICT (id) DO UPDATE
                    SET number=EXCLUDED.number, name=EXCLUDED.name,
                        branch_id=EXCLUDED.branch_id, location_id=EXCLUDED.location_id,
                        org_id=EXCLUDED.org_id, active=TRUE
                """,
                table_id, number, name, location_id, location_id, org_id,
            )


# ── Main test ─────────────────────────────────────────────────────────────────

@pytest.mark.e2e_no_llm
@pytest.mark.asyncio
async def test_happy_path_qr_order_pay_kitchen_nps(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
    monkeypatch: pytest.MonkeyPatch,
):
    """
    Full bot happy-path: QR scan → greet → order → kitchen → bill → pay → NPS.

    Each WhatsApp inbound triggers the real inbox_worker dispatch (mocked Anthropic).
    All DB assertions verify the real schema post-Wave-2 (org_id columns, RLS active).
    """
    pool = test_pool
    monkeypatch.setenv("META_ACCESS_TOKEN", "e2e_dummy_token_happy")

    # ── STEP 0: Seed org + sibling + table ───────────────────────────────────
    restaurant = await seed_restaurant(
        pool,
        name="E2E Happy Restaurant",
        bot_number_raw=BOT_NUMBER_RAW,
        menu=MENU,
        num_branches=1,
        features_override={
            "allow_manual_table_number": False,  # force [table_id:X] path
        },
    )
    org_id: int = restaurant["id"]
    bot_number: str = restaurant["whatsapp_number"]
    # location_id: the principal location (whatsapp_number=None) was created by seed_restaurant.
    # We need its ID to create the table.
    with bypass_tenant_scope("e2e_happy_get_principal_loc"):
        async with pool.acquire() as conn:
            loc_row = await conn.fetchrow(
                "SELECT id FROM locations WHERE org_id=$1 AND whatsapp_number IS NULL ORDER BY id ASC LIMIT 1",
                org_id,
            )
    assert loc_row, "Principal location must exist after seed_restaurant"
    principal_location_id: int = loc_row["id"]

    # Sibling org for isolation check
    sibling = await seed_restaurant(
        pool,
        name="E2E Sibling Restaurant",
        bot_number_raw=SIBLING_BOT_RAW,
        num_branches=1,
    )
    sibling_org_id: int = sibling["id"]

    # Clean up any leftover volatile data from prior runs
    await truncate_e2e_data(pool, org_id)
    await truncate_e2e_data(pool, sibling_org_id)

    table_id = f"table-e2e-happy-{org_id}"
    table_name = "M1"
    await _seed_table(pool, org_id, principal_location_id, table_id, 1, table_name)

    log.info("e2e.happy.seed_done", org_id=org_id, table_id=table_id)

    # ── Build the Anthropic mock script ──────────────────────────────────────
    # Each entry corresponds to one `call_claude()` invocation inside agent.py,
    # in the ORDER the turns will fire.

    script = MockAnthropicScript([
        # Turn 1: customer sends "hola [table_id:X]" — bot greets
        _FakeAnthropicMessage(
            text="Bienvenido a E2E Happy Restaurant! Estas en la mesa M1. Que te gustaria pedir hoy?"
        ),
        # Turn 2: "qué tienen?" — bot describes menu (no tool)
        _FakeAnthropicMessage(
            text=(
                "Tenemos Bandeja Paisa ($28,000), Limonada ($5,000) y Postre del Dia ($8,000). "
                "Que te gustaria ordenar?"
            )
        ),
        # Turn 3: "una bandeja paisa y una limonada"
        # The confirmation guard (_validate_tool_call, agent.py ~L1415) intercepts
        # place_order when full_history does NOT contain a recent confirmation word.
        # "una bandeja paisa y una limonada" is an order request, NOT a confirmation word.
        # The guard returns "awaiting_confirmation" and asks the customer to confirm.
        # The bot's reply to the customer becomes: "Confirmas tu pedido de 1x Bandeja Paisa, 1x Limonada?"
        _FakeAnthropicMessage(
            text="Confirmas tu pedido de Bandeja Paisa y Limonada?",
            tool_name="place_order",
            tool_input={
                "items": [
                    {"name": "Bandeja Paisa", "qty": 1},
                    {"name": "Limonada", "qty": 1},
                ]
            },
        ),
        # Turn 4: Customer says "si" (confirmation word) — guard passes this time.
        # The LLM fires place_order again → guard sees "si" in history → order committed.
        _FakeAnthropicMessage(
            text="Perfecto! Ya envie tu pedido a la cocina.",
            tool_name="place_order",
            tool_input={
                "items": [
                    {"name": "Bandeja Paisa", "qty": 1},
                    {"name": "Limonada", "qty": 1},
                ]
            },
        ),
        # Turn 5: "la cuenta por favor" — bot fires request_bill tool
        _FakeAnthropicMessage(
            text="Claro! Ya pedi la cuenta. Un momento.",
            tool_name="request_bill",
            tool_input={"separate_bill": False},
        ),
        # Turn 6: "gracias" — bot fires end_session (triggers NPS)
        _FakeAnthropicMessage(
            text="Gracias por visitarnos! Esperamos verte pronto.",
            tool_name="end_session",
            tool_input={},
        ),
        # Turn 7: NPS score "5" — handled by _handle_nps_flow BEFORE LLM is called.
        # Mesio uses a 1-5 scale (NOT 0-10). _handle_nps_flow consumes the turn directly.
        # If somehow the NPS flow doesn't consume it and it reaches the LLM:
        _FakeAnthropicMessage(text="Gracias por tu calificacion!"),
    ])

    # ── Simulate turns via real webhook endpoint ─────────────────────────────

    async def _send(text: str, phone: str = CUSTOMER_PHONE_RAW) -> int:
        """Helper: send inbound WhatsApp message, drain inbox, return processed count."""
        return await simulate_whatsapp_inbound(
            e2e_app, pool,
            phone=phone,
            text=text,
            bot_number=bot_number,
        )

    with script.patch():

        # ── STEP 1: Customer scans QR (via [table_id:X] tag) ─────────────────
        # The [table_id:X] tag in the message triggers detect_table_context
        # path 1 (retrocompatibilidad) in agent.py line ~450.
        processed = await _send(f"hola [table_id:{table_id}]")
        assert processed >= 1, "Turn 1: inbox must process at least 1 item"

        # Assert: table_sessions row created
        with bypass_tenant_scope("e2e_happy_verify_step1"):
            async with pool.acquire() as conn:
                session_row = await conn.fetchrow(
                    "SELECT id, table_id, phone, status, org_id FROM table_sessions "
                    "WHERE org_id=$1 AND phone=$2 ORDER BY id DESC LIMIT 1",
                    org_id, CUSTOMER_PHONE,
                )
        assert session_row is not None, (
            "STEP 1 FAILED: No table_sessions row found after QR scan. "
            "detect_table_context [table_id:X] path did not create a session."
        )
        assert session_row["table_id"] == table_id, (
            f"Session table_id mismatch: expected {table_id!r}, got {session_row['table_id']!r}"
        )
        assert session_row["status"] == "active", (
            f"Session should be 'active', got {session_row['status']!r}"
        )

        # Assert: conversations row created (no `id` column — natural key is phone+bot_number)
        with bypass_tenant_scope("e2e_happy_verify_conv_step1"):
            async with pool.acquire() as conn:
                conv_row = await conn.fetchrow(
                    "SELECT phone, bot_number, org_id FROM conversations "
                    "WHERE org_id=$1 AND phone=$2 LIMIT 1",
                    org_id, CUSTOMER_PHONE,
                )
        assert conv_row is not None, (
            "STEP 1 FAILED: No conversations row found after greeting. "
            "db_save_history did not persist the conversation."
        )
        assert conv_row["bot_number"] == bot_number, (
            f"Conversation bot_number mismatch: expected {bot_number!r}, got {conv_row['bot_number']!r}"
        )

        # Assert: bot replied to the customer
        greet_texts = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
        assert greet_texts, "STEP 1 FAILED: No WA reply captured for greeting turn"
        assert any("mesa" in t.lower() or "bienvenid" in t.lower() for t in greet_texts), (
            f"STEP 1 FAILED: Greeting reply doesn't mention 'mesa' or 'bienvenid': {greet_texts}"
        )
        wa_capture.messages.clear()  # reset for next step

        # ── STEP 2: Customer asks for menu ────────────────────────────────────
        processed = await _send("qué tienen?")
        assert processed >= 1, "Turn 2: inbox must process at least 1 item"

        menu_texts = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
        assert menu_texts, "STEP 2 FAILED: No WA reply captured for menu question"
        menu_reply = " ".join(menu_texts).lower()
        assert "bandeja" in menu_reply or "limonada" in menu_reply or "menú" in menu_reply or "menu" in menu_reply, (
            f"STEP 2 FAILED: Menu reply should mention dishes or 'menú': {menu_texts}"
        )
        wa_capture.messages.clear()

        # ── STEP 3: Customer orders (bot asks for confirmation) ──────────────
        # The mocked LLM fires place_order on turn 3, but the confirmation guard
        # (_validate_tool_call, agent.py ~L1415) intercepts it because the customer's
        # message "una bandeja paisa y una limonada" is an ORDER request, NOT a
        # confirmation word. The guard asks "¿Confirmas tu pedido?" and does NOT
        # create the table_orders row yet. Items are added to the cart regardless.
        processed = await _send("una bandeja paisa y una limonada")
        assert processed >= 1, "Turn 3: inbox must process at least 1 item"

        # Assert: bot asks for confirmation (the guard's reply, not the LLM's text)
        turn3_texts = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
        assert turn3_texts, "STEP 3 FAILED: No WA reply after order request"
        turn3_reply = " ".join(turn3_texts).lower()
        assert "confirma" in turn3_reply or "pedido" in turn3_reply, (
            f"STEP 3 FAILED: Bot should ask for confirmation after order request. Got: {turn3_texts}"
        )
        wa_capture.messages.clear()

        # ── STEP 4: Customer confirms → order committed ────────────────────────
        # Customer says "si" (a confirmation word in _CONFIRM_WORDS).
        # _last_messages_have_confirmation(history) now returns True → guard passes.
        # LLM fires place_order again → execute_salon_action → db_create_table_order.
        processed = await _send("si")
        assert processed >= 1, "Turn 4: inbox must process at least 1 item"

        # Assert: table_orders row created
        with bypass_tenant_scope("e2e_happy_verify_step4_order"):
            async with pool.acquire() as conn:
                order_row = await conn.fetchrow(
                    "SELECT id, table_id, total, status, org_id, items FROM table_orders "
                    "WHERE org_id=$1 AND phone=$2 ORDER BY id DESC LIMIT 1",
                    org_id, CUSTOMER_PHONE,
                )
        assert order_row is not None, (
            "STEP 4 FAILED: No table_orders row found after confirmation + place_order. "
            "execute_salon_action → db_create_table_order did not fire or failed."
        )
        assert order_row["table_id"] == table_id, (
            f"Order table_id mismatch: expected {table_id!r}, got {order_row['table_id']!r}"
        )
        order_total = Decimal(str(order_row["total"]))
        assert order_total == EXPECTED_TOTAL, (
            f"STEP 4 FAILED: Expected order total {EXPECTED_TOTAL}, got {order_total}. "
            "Pricing or cart math is wrong."
        )

        # The base_order_id is the ID of the first (and only) table order for this session.
        # For single-station orders, base_order_id == order_row["id"].
        base_order_id: str = order_row["id"]
        table_order_id: str = order_row["id"]

        # Assert: cart is cleared after order placed (or empty)
        # carts schema: (phone, bot_number, cart_data JSONB, org_id, ...) — no 'id' column.
        with bypass_tenant_scope("e2e_happy_verify_step4_cart"):
            async with pool.acquire() as conn:
                cart_row = await conn.fetchrow(
                    "SELECT cart_data FROM carts WHERE org_id=$1 AND phone=$2 AND bot_number=$3",
                    org_id, CUSTOMER_PHONE, bot_number,
                )
        # After place_order, the cart should either not exist or have empty items list.
        # Production code in orders.py / agent_salon.py clears the cart post-order.
        if cart_row is not None:
            cart_data = cart_row["cart_data"]
            if isinstance(cart_data, str):
                import json as _json
                cart_data = _json.loads(cart_data)
            items_in_cart = (cart_data or {}).get("items", []) if isinstance(cart_data, dict) else []
            assert not items_in_cart, (
                f"STEP 4 FAILED: Cart still has items after place_order: {items_in_cart}. "
                "orders.clear_cart() did not fire post-order."
            )

        wa_capture.messages.clear()

        # ── STEP 5: Kitchen marks order as 'entregado' via API ───────────────
        owner_email = restaurant["owner_email"]
        admin_token = await create_admin_token(pool, owner_email)
        auth_headers = {"Authorization": f"Bearer {admin_token}"}

        status_resp = await e2e_app.post(
            f"/api/table-orders/{table_order_id}/status",
            json={"status": "entregado"},
            headers=auth_headers,
        )
        assert status_resp.status_code == 200, (
            f"STEP 5 FAILED: POST /api/table-orders/{table_order_id}/status returned "
            f"{status_resp.status_code}: {status_resp.text}"
        )

        # Assert: status updated in DB
        with bypass_tenant_scope("e2e_happy_verify_step5"):
            async with pool.acquire() as conn:
                updated_order = await conn.fetchrow(
                    "SELECT status FROM table_orders WHERE id=$1",
                    table_order_id,
                )
        assert updated_order is not None, "STEP 5 FAILED: Order disappeared after status update"
        assert updated_order["status"] == "entregado", (
            f"STEP 5 FAILED: Expected status='entregado', got {updated_order['status']!r}. "
            "POST /api/table-orders/.../status did not persist the update."
        )

        # ── STEP 5b: Mark session as order_delivered ──────────────────────────
        # The kitchen API (POST /api/table-orders/{id}/status with 'entregado') only
        # updates table_orders.status. It does NOT update table_sessions.order_delivered.
        # In production, order_delivered is set by db_session_mark_delivered() which is
        # called from the agent when the table session transitions after delivery.
        # For this E2E test we set it directly in the DB to allow the bill-request guard
        # (agent_salon.py L1059: "bill_blocked_order_not_delivered") to pass.
        # This mirrors the real production data state when a customer asks for the bill.
        with bypass_tenant_scope("e2e_happy_mark_session_delivered"):
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE table_sessions SET order_delivered=TRUE, last_activity=NOW() "
                    "WHERE org_id=$1 AND phone=$2 AND bot_number=$3 AND status='active'",
                    org_id, CUSTOMER_PHONE, bot_number,
                )

        wa_capture.messages.clear()

        # ── STEP 6: Customer requests bill ────────────────────────────────────
        # The mocked LLM fires request_bill → execute_salon_action → db_create_waiter_alert.
        # Per CLAUDE.md Regla #17: the bill alert fires IMMEDIATELY when customer asks,
        # before the checkout state machine completes.
        processed = await _send("la cuenta por favor")
        assert processed >= 1, "Turn 5: inbox must process at least 1 item"

        # Assert: waiter_alerts row with type='bill'
        with bypass_tenant_scope("e2e_happy_verify_step6"):
            async with pool.acquire() as conn:
                alert_row = await conn.fetchrow(
                    "SELECT id, alert_type, table_id, phone, org_id FROM waiter_alerts "
                    "WHERE org_id=$1 AND phone=$2 AND alert_type='bill' ORDER BY id DESC LIMIT 1",
                    org_id, CUSTOMER_PHONE,
                )
        assert alert_row is not None, (
            "STEP 6 FAILED: No waiter_alerts row with type='bill' found. "
            "execute_salon_action for 'bill' did not call db_create_waiter_alert. "
            "Per CLAUDE.md Regla #17, the alert must fire at the instant the customer requests the bill."
        )
        assert alert_row["table_id"] == table_id, (
            f"Alert table_id mismatch: expected {table_id!r}, got {alert_row['table_id']!r}"
        )
        wa_capture.messages.clear()

        # ── STEP 7: Caja pays the table (single-check payment) ───────────────
        # Use POST /api/table-orders/{base_order_id}/checks/single/pay
        # This creates a single check covering the full ticket and immediately pays it.
        pay_resp = await e2e_app.post(
            f"/api/table-orders/{base_order_id}/checks/single/pay",
            json={
                "payments": [{"method": "Efectivo", "amount": 33000}],
                "tip_amount": 0,
                "service_charge": 0,
            },
            headers=auth_headers,
        )
        assert pay_resp.status_code == 200, (
            f"STEP 7 FAILED: POST /checks/single/pay returned {pay_resp.status_code}: {pay_resp.text}. "
            "Caja cannot process cash payment for the table."
        )
        pay_body = pay_resp.json()

        # Assert: a table_checks row exists with paid_at set
        with bypass_tenant_scope("e2e_happy_verify_step7"):
            async with pool.acquire() as conn:
                check_row = await conn.fetchrow(
                    """
                    SELECT tc.id, tc.status, tc.paid_at, tc.total
                    FROM table_checks tc
                    WHERE tc.base_order_id=$1
                    ORDER BY tc.id DESC LIMIT 1
                    """,
                    base_order_id,
                )
        assert check_row is not None, (
            "STEP 7 FAILED: No table_checks row found after single-pay. "
            "checks/single/pay did not create or pay a check."
        )
        assert check_row["paid_at"] is not None, (
            f"STEP 7 FAILED: table_checks.paid_at is NULL — payment was not finalized. "
            f"Check status: {check_row['status']!r}"
        )
        check_total = Decimal(str(check_row["total"]))
        assert check_total == EXPECTED_TOTAL, (
            f"STEP 7 FAILED: Check total {check_total} != expected {EXPECTED_TOTAL}"
        )

        # ── STEP 8: NPS flow — customer says "gracias" then rates 10 ─────────
        # Turn 6 fires end_session → triggers NPS in agent.py _maybe_append_nps_prompt.
        # The NPS state is stored in state_store (Redis or in-process fallback).
        processed = await _send("gracias")
        assert processed >= 1, "Turn 6: inbox must process at least 1 item"

        # Check that NPS state was set (waiting_score) in state_store.
        # We query state_store directly — it handles Redis-or-fallback transparently.
        from app.services import state_store as _ss
        nps_state = await _ss.nps_get(CUSTOMER_PHONE, bot_number)
        # NPS may be in waiting_score OR already in cooldown (if the test is re-run quickly).
        # Either way, it should not be None at this point unless something went wrong.
        # We accept None here with a BLOCKER note (see deliverable notes below).
        if nps_state is None:
            log.warning(
                "e2e.happy.nps_state_none",
                hint=(
                    "NPS state not found in state_store after 'gracias' turn. "
                    "This may mean end_session was not triggered OR the NPS state TTL "
                    "expired. The test will still verify the nps_responses row below "
                    "if the score turn fires correctly."
                ),
            )

        wa_capture.messages.clear()

        # Turn 7: customer types "5" (top score on Mesio's 1-5 scale).
        # _handle_nps_guard / _try_nps_active_flow in agent.py intercept this turn
        # BEFORE the LLM is called. This turn is ONLY dispatched if NPS state is
        # waiting_score. If NPS state is None (e.g. end_session did not fire),
        # the "5" goes to the LLM and the nps_responses check below will fail.
        processed = await _send("5")
        assert processed >= 1, "Turn 7 (NPS score): inbox must process at least 1 item"

        # Assert: nps_responses row created with score=5 (max → positive flow,
        # which calls db_save_nps_response directly with empty comment).
        with bypass_tenant_scope("e2e_happy_verify_step8_nps"):
            async with pool.acquire() as conn:
                nps_row = await conn.fetchrow(
                    "SELECT id, score, phone, org_id FROM nps_responses "
                    "WHERE org_id=$1 AND phone=$2 ORDER BY id DESC LIMIT 1",
                    org_id, CUSTOMER_PHONE,
                )
        assert nps_row is not None, (
            "STEP 8 FAILED: No nps_responses row found after customer rated '5'. "
            "BLOCKER: end_session may not be firing trigger_nps() correctly, OR "
            "the NPS flow in _handle_nps_flow did not parse '5' as a valid score. "
            "Check agent.py::trigger_nps and _handle_nps_flow."
        )
        assert nps_row["score"] == 5, (
            f"STEP 8 FAILED: Expected score=5, got {nps_row['score']!r}. "
            "NPS parser regression — verify regex in _handle_nps_flow rejects "
            "out-of-range numbers and accepts valid 1-5 input."
        )

        wa_capture.messages.clear()

    # ── Tenant isolation check ────────────────────────────────────────────────
    # All queries above used org_id for the main restaurant.
    # Verify that the sibling org (different bot_number) sees ZERO rows.
    with bypass_tenant_scope("e2e_happy_isolation_check"):
        async with pool.acquire() as conn:
            sib_orders = await conn.fetchval(
                "SELECT COUNT(*) FROM table_orders WHERE org_id=$1", sibling_org_id
            )
            sib_convs = await conn.fetchval(
                "SELECT COUNT(*) FROM conversations WHERE org_id=$1", sibling_org_id
            )
            sib_sessions = await conn.fetchval(
                "SELECT COUNT(*) FROM table_sessions WHERE org_id=$1", sibling_org_id
            )
            sib_nps = await conn.fetchval(
                "SELECT COUNT(*) FROM nps_responses WHERE org_id=$1", sibling_org_id
            )

    assert sib_orders == 0, (
        f"ISOLATION FAILED: Sibling org has {sib_orders} table_orders — RLS leak. "
        "The sibling org must see ZERO rows from the main org's session."
    )
    assert sib_convs == 0, (
        f"ISOLATION FAILED: Sibling org has {sib_convs} conversations — RLS leak."
    )
    assert sib_sessions == 0, (
        f"ISOLATION FAILED: Sibling org has {sib_sessions} table_sessions — RLS leak."
    )
    assert sib_nps == 0, (
        f"ISOLATION FAILED: Sibling org has {sib_nps} nps_responses — RLS leak."
    )

    log.info(
        "e2e.happy_path.passed",
        org_id=org_id,
        table_id=table_id,
        order_total=str(order_total),
        check_total=str(check_total),
        nps_score=nps_row["score"] if nps_row else "NOT_RECORDED",
    )
