"""
Suite — Customer Memory Integration Tests (17 tests)
tests/test_customer_memory_integration.py

Tests the end-to-end wiring of Feature 2 (customer memory) across:
  - Tool registration & schema (fast sanity checks)
  - _tool_use_to_parsed bridge
  - _validate_tool_call guard #7
  - execute_action remember branch
  - build_system_prompt customer_context injection
  - Profile load in _call_llm_and_execute
  - Post-order hook in orders.py

All external dependencies (DB, Anthropic, Redis) are fully mocked.
No real DB or real Anthropic calls are made.
"""
import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import make_pool, make_row
from app.services.tenant_context import tenant_scope


# ── Helper: run a coroutine in the current event loop ────────────────────────

def _run(coro):
    """Execute a coroutine in the current event loop (matches existing suite style)."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ── Helper: build a minimal Anthropic mock returning a text reply ─────────────

def _build_anthropic_mock(reply_text: str):
    """Build a mock Anthropic client whose messages.create returns reply_text."""
    content_block = MagicMock()
    content_block.type = "text"
    content_block.text = reply_text

    usage_mock = MagicMock()
    usage_mock.input_tokens = 10
    usage_mock.output_tokens = 10

    msg_mock = MagicMock()
    msg_mock.content = [content_block]
    msg_mock.stop_reason = "end_turn"
    msg_mock.usage = usage_mock

    anthropic_mock = MagicMock()
    anthropic_mock.messages = MagicMock()
    anthropic_mock.messages.create = AsyncMock(return_value=msg_mock)
    return anthropic_mock


# ── Helper: patch the minimum DB calls that agent.chat() needs ───────────────

def _patch_db_for_chat(monkeypatch, bot_number: str = "+573009876543"):
    """Patch the minimum DB calls that agent.chat() needs."""
    from app.services import database as db

    restaurant = {
        "id": 1,
        "name": "Restaurante Test",
        "whatsapp_number": bot_number,
        "features": {"locale": "es-CO", "currency": "COP"},
    }
    monkeypatch.setattr(db, "db_get_restaurant_by_bot_number",
                        AsyncMock(return_value=restaurant))
    monkeypatch.setattr(db, "db_get_history",
                        AsyncMock(return_value=[]))
    monkeypatch.setattr(db, "db_save_history", AsyncMock())
    monkeypatch.setattr(db, "db_check_usage_limits", AsyncMock())
    monkeypatch.setattr(db, "db_increment_token_usage", AsyncMock())
    monkeypatch.setattr(db, "db_get_menu", AsyncMock(return_value={}))
    monkeypatch.setattr(db, "db_get_menu_availability", AsyncMock(return_value={}))
    monkeypatch.setattr(db, "db_get_active_session", AsyncMock(return_value=None))
    monkeypatch.setattr(db, "db_get_all_restaurants",
                        AsyncMock(return_value=[restaurant]))
    monkeypatch.setattr(db, "db_get_cart",
                        AsyncMock(return_value={"items": []}))

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=make_pool(conn)))
    return restaurant


# ══════════════════════════════════════════════════════════════════════════════
# Class: TestCustomerMemoryIntegration
# ══════════════════════════════════════════════════════════════════════════════

class TestCustomerMemoryIntegration:

    # ── 1. Tool registration & schema ────────────────────────────────────────

    def test_remember_tool_present_in_salon_and_external(self):
        """remember_customer_preference is in TOOLS_SALON, TOOLS_EXTERNAL, and ALL_TOOLS
        with the required input_schema fields (key, value, reason all required)."""
        from app.services.agent_tools import TOOLS_SALON, TOOLS_EXTERNAL, ALL_TOOLS

        salon_names = {t["name"] for t in TOOLS_SALON}
        external_names = {t["name"] for t in TOOLS_EXTERNAL}

        assert "remember_customer_preference" in salon_names, \
            "remember_customer_preference missing from TOOLS_SALON"
        assert "remember_customer_preference" in external_names, \
            "remember_customer_preference missing from TOOLS_EXTERNAL"
        assert "remember_customer_preference" in ALL_TOOLS, \
            "remember_customer_preference missing from ALL_TOOLS"

        # Verify input_schema has key, value, reason all required
        tool_def = ALL_TOOLS["remember_customer_preference"]
        schema = tool_def["input_schema"]
        props = schema["properties"]
        required = schema["required"]

        assert "key" in props, "key missing from tool input_schema properties"
        assert "value" in props, "value missing from tool input_schema properties"
        assert "reason" in props, "reason missing from tool input_schema properties"
        assert "key" in required, "key not in required list"
        assert "value" in required, "value not in required list"
        assert "reason" in required, "reason not in required list"

    # ── 2. _TOOL_TO_ACTION mapping ───────────────────────────────────────────

    def test_remember_tool_maps_to_action(self):
        """_TOOL_TO_ACTION maps 'remember_customer_preference' to 'remember'."""
        from app.services.agent import _TOOL_TO_ACTION

        assert _TOOL_TO_ACTION["remember_customer_preference"] == "remember"

    # ── 3. _tool_use_to_parsed bridge ────────────────────────────────────────

    def test_tool_use_to_parsed_populates_preference_for_remember(self):
        """_tool_use_to_parsed correctly populates parsed['preference'] for remember action."""
        from app.services.agent import _tool_use_to_parsed

        tool_input = {
            "key": "dietary",
            "value": "vegetariano",
            "reason": "dijo: 'soy vegetariano'",
        }
        parsed = _tool_use_to_parsed("reply text", "remember_customer_preference", tool_input)

        assert parsed["action"] == "remember"
        assert parsed["preference"] == {
            "key": "dietary",
            "value": "vegetariano",
            "reason": "dijo: 'soy vegetariano'",
        }
        assert parsed["reply"] == "reply text"

    # ── 4. _validate_tool_call guard #7 — valid input ────────────────────────

    def test_validate_remember_accepts_valid_input(self, monkeypatch):
        """_validate_tool_call accepts valid remember_customer_preference input and
        passes it through unchanged when rate limit check returns True."""
        import app.services.agent as agent_mod

        monkeypatch.setattr(
            "app.services.agent.state_store.rate_limit_check",
            AsyncMock(return_value=True),
        )

        tool_input = {
            "key": "allergies",
            "value": "maní",
            "reason": "es alérgico al maní",
        }
        tool_name, reply, returned_input = _run(
            agent_mod._validate_tool_call(
                "remember_customer_preference",
                tool_input,
                "reply",
                None,  # table_context (external mode)
                "+573009876543",
                "+573001234567",
            )
        )

        assert tool_name == "remember_customer_preference", \
            "Valid input should not be downgraded"
        assert returned_input == tool_input, \
            "tool_input should be returned unchanged"

    # ── 5. _validate_tool_call — invalid key ─────────────────────────────────

    def test_validate_remember_rejects_invalid_key(self, monkeypatch):
        """_validate_tool_call rejects remember_customer_preference with invalid key
        by downgrading to chat (tool_name = None, tool_input = {})."""
        import app.services.agent as agent_mod

        monkeypatch.setattr(
            "app.services.agent.state_store.rate_limit_check",
            AsyncMock(return_value=True),
        )

        tool_input = {
            "key": "hacker_mode",
            "value": "evil",
            "reason": "some reason",
        }
        tool_name, reply, returned_input = _run(
            agent_mod._validate_tool_call(
                "remember_customer_preference",
                tool_input,
                "reply",
                None,
                "+573009876543",
                "+573001234567",
            )
        )

        assert tool_name is None, "Invalid key should downgrade to chat"
        assert returned_input == {}, "Downgraded tool_input should be {}"

    # ── 6. _validate_tool_call — empty value ─────────────────────────────────

    def test_validate_remember_rejects_empty_value(self, monkeypatch):
        """_validate_tool_call rejects remember with empty value."""
        import app.services.agent as agent_mod

        monkeypatch.setattr(
            "app.services.agent.state_store.rate_limit_check",
            AsyncMock(return_value=True),
        )

        tool_input = {
            "key": "dietary",
            "value": "",
            "reason": "some reason",
        }
        tool_name, reply, returned_input = _run(
            agent_mod._validate_tool_call(
                "remember_customer_preference",
                tool_input,
                "reply",
                None,
                "+573009876543",
                "+573001234567",
            )
        )

        assert tool_name is None, "Empty value should downgrade to chat"

    # ── 7. _validate_tool_call — empty reason ────────────────────────────────

    def test_validate_remember_rejects_empty_reason(self, monkeypatch):
        """_validate_tool_call rejects remember with empty or whitespace-only reason."""
        import app.services.agent as agent_mod

        monkeypatch.setattr(
            "app.services.agent.state_store.rate_limit_check",
            AsyncMock(return_value=True),
        )

        # Whitespace-only reason counts as empty
        tool_input = {
            "key": "dietary",
            "value": "vegetariano",
            "reason": "   ",
        }
        tool_name, reply, returned_input = _run(
            agent_mod._validate_tool_call(
                "remember_customer_preference",
                tool_input,
                "reply",
                None,
                "+573009876543",
                "+573001234567",
            )
        )

        assert tool_name is None, "Whitespace-only reason should downgrade to chat"

    # ── 8. _validate_tool_call — rate limit blocks ───────────────────────────

    def test_validate_remember_rate_limit_blocks_after_3(self, monkeypatch):
        """_validate_tool_call downgrades to chat when rate_limit_check returns False."""
        import app.services.agent as agent_mod

        monkeypatch.setattr(
            "app.services.agent.state_store.rate_limit_check",
            AsyncMock(return_value=False),
        )

        tool_input = {
            "key": "dietary",
            "value": "vegano",
            "reason": "dijo que es vegano",
        }
        tool_name, reply, returned_input = _run(
            agent_mod._validate_tool_call(
                "remember_customer_preference",
                tool_input,
                "reply",
                None,
                "+573009876543",
                "+573001234567",
            )
        )

        assert tool_name is None, "Rate-limited remember should downgrade to chat"
        assert returned_input == {}, "Downgraded tool_input should be {}"

    # ── 9. execute_action — remember calls update_preference ─────────────────

    def test_execute_action_remember_calls_update_preference(self, monkeypatch):
        """execute_action with action='remember' calls update_preference with correct kwargs
        and returns the reply unchanged."""
        import app.services.agent as agent_mod
        import app.repositories.customer_profiles_repo as repo

        update_mock = AsyncMock()
        monkeypatch.setattr(repo, "update_preference", update_mock)

        # Patch the lazy import inside execute_action to use our mock
        monkeypatch.setattr(
            "app.repositories.customer_profiles_repo.update_preference",
            update_mock,
        )

        parsed = {
            "action": "remember",
            "reply": "¡Anotado! Recordaré que eres vegetariano.",
            "preference": {
                "key": "dietary",
                "value": "vegetariano",
                "reason": "dijo: 'soy vegetariano'",
            },
            "items": [],
        }
        restaurant_obj = {"id": 1, "name": "Restaurante Test"}

        result = _run(
            agent_mod.execute_action(
                parsed,
                phone="+573001234567",
                bot_number="+573009876543",
                table_context=None,
                session_state={},
                full_history=[],
                restaurant_obj=restaurant_obj,
                routing_context={},
                message="soy vegetariano",
            )
        )

        assert result == "¡Anotado! Recordaré que eres vegetariano.", \
            "execute_action should return reply unchanged"
        update_mock.assert_awaited_once()
        call_kwargs = update_mock.call_args
        # Works with both positional and keyword call styles
        args, kwargs = call_kwargs
        # Combine positional and keyword for flexible assertion
        all_args = list(args) + list(kwargs.values())
        assert 1 in all_args or kwargs.get("restaurant_id") == 1, \
            "restaurant_id=1 should be passed to update_preference"
        assert "+573001234567" in all_args or kwargs.get("phone") == "+573001234567", \
            "phone should be passed to update_preference"

    # ── 10. execute_action — survives DB error ───────────────────────────────

    def test_execute_action_remember_survives_db_error(self, monkeypatch):
        """execute_action with action='remember' does not crash on DB error.
        The reply flows through unchanged even if update_preference raises."""
        import app.services.agent as agent_mod
        import app.repositories.customer_profiles_repo as repo

        monkeypatch.setattr(repo, "update_preference",
                            AsyncMock(side_effect=RuntimeError("db down")))

        parsed = {
            "action": "remember",
            "reply": "Lo tendré en cuenta.",
            "preference": {
                "key": "dietary",
                "value": "vegano",
                "reason": "dijo: soy vegano",
            },
            "items": [],
        }
        restaurant_obj = {"id": 1, "name": "Restaurante Test"}

        # Must not raise
        result = _run(
            agent_mod.execute_action(
                parsed,
                phone="+573001234567",
                bot_number="+573009876543",
                table_context=None,
                session_state={},
                full_history=[],
                restaurant_obj=restaurant_obj,
                routing_context={},
                message="soy vegano",
            )
        )

        assert result == "Lo tendré en cuenta.", \
            "Reply should flow through even when update_preference raises"

    # ── 11. build_system_prompt — includes customer_context ──────────────────

    def test_build_system_prompt_includes_customer_context_when_provided(self, monkeypatch):
        """build_system_prompt appends a [CONTEXTO_CLIENTE] block when customer_context
        is non-empty. The block must contain the context text."""
        import app.services.agent as agent_mod

        # Patch discount lookup to avoid DB calls
        monkeypatch.setattr(
            "app.services.agent.build_system_prompt.__wrapped__"
            if hasattr(agent_mod.build_system_prompt, "__wrapped__") else
            "app.services.agent.build_system_prompt",
            agent_mod.build_system_prompt,  # no-op — we call it directly below
        )

        prompt_blocks = _run(
            agent_mod.build_system_prompt(
                features={},
                table_context=None,
                restaurant_id=None,
                customer_context="Cliente: Miguel. 3 pedidos.",
            )
        )

        # The prompt is a list of blocks; at least one must contain the marker
        full_text = " ".join(
            b.get("text", "") for b in prompt_blocks if isinstance(b, dict)
        )
        assert "[CONTEXTO_CLIENTE]" in full_text, \
            "[CONTEXTO_CLIENTE] marker not found in system prompt"
        assert "Miguel" in full_text, \
            "Customer context text not injected into system prompt"

    # ── 12. build_system_prompt — omits block when empty ─────────────────────

    def test_build_system_prompt_omits_customer_context_when_empty(self):
        """build_system_prompt does NOT add [CONTEXTO_CLIENTE] block when
        customer_context is empty string."""
        import app.services.agent as agent_mod

        prompt_blocks = _run(
            agent_mod.build_system_prompt(
                features={},
                table_context=None,
                restaurant_id=None,
                customer_context="",
            )
        )

        full_text = " ".join(
            b.get("text", "") for b in prompt_blocks if isinstance(b, dict)
        )
        assert "[CONTEXTO_CLIENTE]" not in full_text, \
            "[CONTEXTO_CLIENTE] should not appear when customer_context is empty"

    # ── 13. build_system_prompt — backward compatible ────────────────────────

    def test_build_system_prompt_backward_compatible_without_kwarg(self):
        """build_system_prompt works without the customer_context kwarg (3-arg signature).
        No TypeError should be raised."""
        import app.services.agent as agent_mod

        # Call with only the original 3 args (no customer_context)
        result = _run(
            agent_mod.build_system_prompt(
                {},      # features
                None,    # table_context
                None,    # restaurant_id
            )
        )

        assert isinstance(result, list), "build_system_prompt should return a list"
        assert len(result) > 0, "build_system_prompt should return non-empty list"

    # ── 14. _call_llm_and_execute — loads customer profile ───────────────────

    def test_chat_loads_customer_profile_at_start(self, monkeypatch):
        """chat() calls upsert_profile_from_message and get_profile for each message.

        Simplified version: we patch build_system_prompt to intercept the
        customer_context kwarg, and verify it received a non-None value.
        This avoids the complexity of deep-mocking the full LLM chain while
        still proving the profile-load path is wired correctly.
        """
        import app.services.agent as agent_mod
        import app.repositories.customer_profiles_repo as repo
        from app.services import database as db

        bot_number = "+573009876543"
        _patch_db_for_chat(monkeypatch, bot_number)

        # Patch profile repo functions
        upsert_mock = AsyncMock(return_value={
            "id": 1, "restaurant_id": 1, "phone": "+573001234567",
            "display_name": "Miguel", "preferences": {},
            "last_order_summary": None, "total_orders": 5,
            "total_spent": Decimal("100000"),
            "first_seen": "2026-01-01T10:00:00+00:00",
            "last_seen": "2026-04-13T10:00:00+00:00",
        })
        get_profile_mock = AsyncMock(return_value={
            "id": 1, "restaurant_id": 1, "phone": "+573001234567",
            "display_name": "Miguel", "preferences": {},
            "last_order_summary": "2x Hamburguesa", "total_orders": 5,
            "total_spent": Decimal("100000"),
            "first_seen": "2026-01-01T10:00:00+00:00",
            "last_seen": "2026-04-13T10:00:00+00:00",
        })

        monkeypatch.setattr(repo, "upsert_profile_from_message", upsert_mock)
        monkeypatch.setattr(repo, "get_profile", get_profile_mock)

        # Track what customer_context build_system_prompt received
        captured_ctx: list = []
        original_build = agent_mod.build_system_prompt

        async def capturing_build_system_prompt(features=None, table_context=None,
                                                 restaurant_id=None, customer_context="",
                                                 **kwargs):
            captured_ctx.append(customer_context)
            return await original_build(features, table_context, restaurant_id, customer_context,
                                        **kwargs)

        monkeypatch.setattr(agent_mod, "build_system_prompt", capturing_build_system_prompt)

        # Mock the Anthropic client
        monkeypatch.setattr(agent_mod, "client", _build_anthropic_mock("Hola, bienvenido."))

        # Suppress NPS / checkout flows
        monkeypatch.setattr("app.services.agent.state_store.nps_get",
                            AsyncMock(return_value=None))
        monkeypatch.setattr("app.services.agent.state_store.checkout_get",
                            AsyncMock(return_value=None))

        result = _run(agent_mod.chat("+573001234567", "Hola", bot_number))

        assert isinstance(result, dict), "chat() should return a dict"
        assert upsert_mock.called, "upsert_profile_from_message was not called"
        assert get_profile_mock.called, "get_profile was not called"
        # The customer_context passed to build_system_prompt should be non-empty
        # (since the profile has total_orders=5, serialize_for_prompt returns a string)
        assert captured_ctx, "build_system_prompt was never called"
        # At least one call should have non-empty context (profile has orders)
        non_empty = [ctx for ctx in captured_ctx if ctx]
        assert non_empty, \
            "customer_context passed to build_system_prompt was always empty — profile not wired"

    # ── 15. chat — continues when profile load fails ──────────────────────────

    def test_chat_continues_when_profile_load_fails(self, monkeypatch):
        """chat() returns a valid response dict even when the profile layer raises.
        The graceful fallback (customer_context='') must be exercised."""
        import app.services.agent as agent_mod
        import app.repositories.customer_profiles_repo as repo

        bot_number = "+573009876543"
        _patch_db_for_chat(monkeypatch, bot_number)

        # Make upsert raise to simulate DB failure
        monkeypatch.setattr(
            repo,
            "upsert_profile_from_message",
            AsyncMock(side_effect=RuntimeError("db error")),
        )

        # Track customer_context to verify fallback to ""
        captured_ctx: list = []
        original_build = agent_mod.build_system_prompt

        async def capturing_build(features=None, table_context=None,
                                   restaurant_id=None, customer_context="", **kwargs):
            captured_ctx.append(customer_context)
            return await original_build(features, table_context, restaurant_id, customer_context,
                                        **kwargs)

        monkeypatch.setattr(agent_mod, "build_system_prompt", capturing_build)
        monkeypatch.setattr(agent_mod, "client", _build_anthropic_mock("Hola."))
        monkeypatch.setattr("app.services.agent.state_store.nps_get",
                            AsyncMock(return_value=None))
        monkeypatch.setattr("app.services.agent.state_store.checkout_get",
                            AsyncMock(return_value=None))

        # Must NOT raise — the chat should still succeed
        result = _run(agent_mod.chat("+573001234567", "Hola", bot_number))

        assert isinstance(result, dict), \
            "chat() should return dict even when profile load fails"
        # When the profile load fails, customer_context falls back to ""
        assert "" in captured_ctx, \
            "customer_context should fall back to '' when profile load raises"

    # ── 16. create_order — increments customer profile after success ──────────

    def test_order_commit_increments_customer_profile(self, monkeypatch):
        """create_order() calls increment_after_order after a successful commit.

        We mock: cart_lock (bypass), db_get_cart (has items), db_get_restaurant_by_phone,
        get_pool (conn returns no base_order), commit_order_transaction (success),
        and increment_after_order. Then we verify increment_after_order was awaited.
        """
        import app.services.orders as orders_mod
        import app.services.database as db
        import app.repositories.customer_profiles_repo as repo
        import app.repositories.orders_repo as orders_repo

        phone = "+573001234567"
        bot_number = "+573009876543"

        # Cart with one item
        cart = {
            "items": [{
                "name": "Hamburguesa",
                "qty": 1,
                "quantity": 1,
                "price": 25000,
                "subtotal": 25000,
            }]
        }
        monkeypatch.setattr(db, "db_get_cart", AsyncMock(return_value=cart))
        monkeypatch.setattr(db, "db_get_restaurant_by_phone", AsyncMock(return_value={
            "id": 1,
            "name": "Restaurante Test",
            "features": {"delivery_fee": 0, "timezone": "UTC"},
        }))

        # Pool / conn: fetchrow returns None (no existing base_order),
        # fetchval also returns None. tenant_connection() wraps conn.transaction()
        # as an async CM, so the mock needs to support that.
        import contextlib
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetchval = AsyncMock(return_value=None)
        conn.execute  = AsyncMock(return_value=None)

        @contextlib.asynccontextmanager
        async def _fake_txn():
            yield
        conn.transaction = MagicMock(side_effect=lambda: _fake_txn())

        pool_mock = AsyncMock()
        pool_mock.acquire = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=conn),
            __aexit__=AsyncMock(return_value=False),
        ))
        monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=pool_mock))

        # Bypass the cart lock entirely
        @contextlib.asynccontextmanager
        async def _fake_cart_lock(phone, bot_number, ttl_seconds=30):
            yield
        monkeypatch.setattr(orders_mod, "_cart_lock", _fake_cart_lock)

        # commit_order_transaction succeeds (no exception)
        monkeypatch.setattr(orders_repo, "commit_order_transaction", AsyncMock())

        # increment_after_order — the function we want to verify is called
        increment_mock = AsyncMock()
        monkeypatch.setattr(repo, "increment_after_order", increment_mock)

        # orders.py:266 now uses tenant_connection() (RLS-safe) instead of
        # raw pool.acquire() — caller must be inside tenant_scope.
        with tenant_scope(1):
            result = _run(
                orders_mod.create_order(
                    phone=phone,
                    order_type="domicilio",
                    address="Calle 1 #2-3",
                    notes="",
                    bot_number=bot_number,
                    payment_method="efectivo",
                )
            )

        assert result.get("success") is True, \
            f"create_order should succeed; got: {result}"
        assert increment_mock.called, \
            "increment_after_order was not called after successful order commit"

    # ── 17. create_order — survives increment error ───────────────────────────

    def test_order_commit_survives_increment_error(self, monkeypatch):
        """create_order() returns success even when increment_after_order raises.
        The order must succeed — memory update is best-effort.
        """
        import app.services.orders as orders_mod
        import app.services.database as db
        import app.repositories.customer_profiles_repo as repo
        import app.repositories.orders_repo as orders_repo

        phone = "+573001234567"
        bot_number = "+573009876543"

        cart = {
            "items": [{
                "name": "Pizza",
                "qty": 1,
                "quantity": 1,
                "price": 30000,
                "subtotal": 30000,
            }]
        }
        monkeypatch.setattr(db, "db_get_cart", AsyncMock(return_value=cart))
        monkeypatch.setattr(db, "db_get_restaurant_by_phone", AsyncMock(return_value={
            "id": 1,
            "name": "Restaurante Test",
            "features": {"delivery_fee": 0, "timezone": "UTC"},
        }))

        import contextlib
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetchval = AsyncMock(return_value=None)
        conn.execute  = AsyncMock(return_value=None)

        @contextlib.asynccontextmanager
        async def _fake_txn():
            yield
        conn.transaction = MagicMock(side_effect=lambda: _fake_txn())

        pool_mock = AsyncMock()
        pool_mock.acquire = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=conn),
            __aexit__=AsyncMock(return_value=False),
        ))
        monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=pool_mock))

        @contextlib.asynccontextmanager
        async def _fake_cart_lock(phone, bot_number, ttl_seconds=30):
            yield
        monkeypatch.setattr(orders_mod, "_cart_lock", _fake_cart_lock)

        monkeypatch.setattr(orders_repo, "commit_order_transaction", AsyncMock())

        # increment_after_order raises — order must still succeed
        monkeypatch.setattr(
            repo,
            "increment_after_order",
            AsyncMock(side_effect=RuntimeError("db timeout")),
        )

        with tenant_scope(1):
            result = _run(
                orders_mod.create_order(
                    phone=phone,
                    order_type="domicilio",
                    address="Calle 1 #2-3",
                    notes="",
                    bot_number=bot_number,
                    payment_method="efectivo",
                )
            )

        assert result.get("success") is True, \
            "create_order should succeed even when increment_after_order raises"
