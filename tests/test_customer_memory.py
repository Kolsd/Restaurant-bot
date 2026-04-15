"""
Suite — Customer Profiles Repository (14 tests)
tests/test_customer_memory.py

All tests are fully mocked — no real DB is touched.
Uses make_pool / make_row from tests/conftest.py.

Updated for Wave 2 RLS migration: _get_pool removed from repo.
DB calls are now gated by tenant_connection(); tests patch
app.services.database.get_pool and wrap calls in tenant_scope().
"""
import asyncio
import json
from decimal import Decimal
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from tests.conftest import make_pool, make_row
from app.services.tenant_context import tenant_scope, bypass_tenant_scope


# ── Helpers ────────────────────────────────────────────────────────────────────

def _run(coro):
    """Execute a coroutine in the current event loop."""
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_profile(overrides: dict | None = None) -> dict:
    base = {
        "id": 1,
        "restaurant_id": 1,
        "phone": "+57300",
        "display_name": "Miguel",
        "preferences": {"dietary": "vegetariano", "allergies": "sin maní"},
        "last_order_summary": "2 hamburguesas + papas",
        "total_orders": 14,
        "total_spent": Decimal("210000.00"),
        "first_seen": "2026-01-01T10:00:00+00:00",
        "last_seen": "2026-04-13T10:00:00+00:00",
    }
    if overrides:
        base.update(overrides)
    return base


def _make_conn(fetchrow_result=None, execute_result=None):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=execute_result)
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    return conn


def _patched_pool(conn):
    """Return a pool mock wrapped in a patch context for get_pool."""
    return patch("app.services.database.get_pool", AsyncMock(return_value=make_pool(conn)))


# ══════════════════════════════════════════════════════════════════════════════
# Class: TestCustomerProfilesRepo
# ══════════════════════════════════════════════════════════════════════════════

class TestCustomerProfilesRepo:

    # ── 1. get_profile returns None when row is missing ──────────────────────

    def test_get_profile_returns_none_when_missing(self):
        conn = _make_conn(fetchrow_result=None)
        import app.repositories.customer_profiles_repo as repo
        with _patched_pool(conn):
            with tenant_scope(1):
                result = _run(repo.get_profile(1, "+57300"))
        assert result is None

    # ── 2. get_profile returns dict when row is found ────────────────────────

    def test_get_profile_returns_dict_when_found(self):
        profile_data = _make_profile()
        conn = _make_conn(fetchrow_result=make_row(profile_data))
        import app.repositories.customer_profiles_repo as repo
        with _patched_pool(conn):
            with tenant_scope(1):
                result = _run(repo.get_profile(1, "+57300"))
        assert result is not None
        assert isinstance(result, dict)
        assert result["phone"] == "+57300"
        assert result["display_name"] == "Miguel"
        assert result["total_orders"] == 14

    # ── 3. upsert_profile passes display_name as SQL param ──────────────────

    def test_upsert_profile_passes_display_name(self):
        profile_data = _make_profile()
        conn = _make_conn(fetchrow_result=make_row(profile_data))
        import app.repositories.customer_profiles_repo as repo
        with _patched_pool(conn):
            with tenant_scope(1):
                result = _run(repo.upsert_profile_from_message(1, "+57300", display_name="Miguel"))

        conn.fetchrow.assert_awaited_once()
        call_args = conn.fetchrow.call_args
        # The SQL params should include the display_name as the third positional arg
        positional_params = call_args[0]  # (sql, $1, $2, $3)
        assert "Miguel" in positional_params

    # ── 4. upsert_profile without name doesn't break ─────────────────────────

    def test_upsert_profile_without_name(self):
        profile_data = _make_profile({"display_name": None})
        conn = _make_conn(fetchrow_result=make_row(profile_data))
        import app.repositories.customer_profiles_repo as repo
        with _patched_pool(conn):
            with tenant_scope(1):
                result = _run(repo.upsert_profile_from_message(1, "+57300", display_name=None))
        assert result is not None
        # None was passed for display_name — SQL params should include None
        call_args = conn.fetchrow.call_args[0]
        assert None in call_args

    # ── 5. update_preference rejects invalid key ─────────────────────────────

    def test_update_preference_rejects_invalid_key(self):
        # ValueError is raised before DB access — no pool patch needed
        import app.repositories.customer_profiles_repo as repo
        conn = _make_conn(fetchrow_result=make_row(_make_profile()))
        with _patched_pool(conn):
            with tenant_scope(1):
                with pytest.raises(ValueError, match="hacker_mode"):
                    _run(repo.update_preference(1, "+57300", key="hacker_mode", value="evil"))

    # ── 6. update_preference truncates long values to MAX_VALUE_CHARS ────────

    def test_update_preference_truncates_long_value(self):
        captured_args: list = []

        conn = _make_conn(fetchrow_result=make_row(_make_profile()))

        original_execute = conn.execute

        async def capture_execute(sql, *args):
            captured_args.extend(args)

        conn.execute = capture_execute

        import app.repositories.customer_profiles_repo as repo

        with _patched_pool(conn):
            with tenant_scope(1):
                long_value = "x" * 300
                _run(repo.update_preference(1, "+57300", key="notes", value=long_value))

        # The value passed to SQL must be capped at MAX_VALUE_CHARS (200)
        executed_value = next(
            (a for a in captured_args if isinstance(a, str) and len(a) <= 200 and a.startswith("x")),
            None,
        )
        assert executed_value is not None, "Truncated value not found in SQL params"
        assert len(executed_value) == 200

    # ── 7. update_preference calls upsert when profile is missing ────────────

    def test_update_preference_calls_upsert_when_profile_missing(self, monkeypatch):
        """When get_profile returns None, update_preference must upsert first, then update."""
        upsert_called: list[bool] = []

        async def mock_get_profile(rid, phone):
            return None  # profile does not exist

        async def mock_upsert(rid, phone, display_name=None):
            upsert_called.append(True)
            return _make_profile()

        execute_called: list[bool] = []
        conn = _make_conn(fetchrow_result=make_row(_make_profile()))

        async def capture_execute(sql, *args):
            execute_called.append(True)

        conn.execute = capture_execute

        import app.repositories.customer_profiles_repo as repo
        monkeypatch.setattr(repo, "get_profile", mock_get_profile)
        monkeypatch.setattr(repo, "upsert_profile_from_message", mock_upsert)

        with _patched_pool(conn):
            with tenant_scope(1):
                _run(repo.update_preference(1, "+57300", key="dietary", value="vegan"))

        assert upsert_called, "upsert_profile_from_message was not called"
        assert execute_called, "SQL UPDATE was not called after upsert"

    # ── 8. increment_after_order rejects float ───────────────────────────────

    def test_increment_after_order_rejects_float(self):
        # TypeError is raised before DB access
        import app.repositories.customer_profiles_repo as repo
        conn = _make_conn(fetchrow_result=make_row(_make_profile()))
        with _patched_pool(conn):
            with tenant_scope(1):
                with pytest.raises(TypeError, match="Decimal"):
                    _run(
                        repo.increment_after_order(
                            restaurant_id=1,
                            phone="+57300",
                            order_total=12.50,        # float — must be rejected
                            order_summary="Pizza",
                        )
                    )

    # ── 9. increment_after_order accepts Decimal and passes correct params ───

    def test_increment_after_order_accepts_decimal(self, monkeypatch):
        captured: list = []

        async def mock_get_profile(rid, phone):
            return _make_profile()  # profile exists, no upsert needed

        conn = _make_conn(fetchrow_result=make_row(_make_profile()))

        async def capture_execute(sql, *args):
            captured.extend(args)

        conn.execute = capture_execute

        import app.repositories.customer_profiles_repo as repo
        monkeypatch.setattr(repo, "get_profile", mock_get_profile)

        with _patched_pool(conn):
            with tenant_scope(1):
                _run(
                    repo.increment_after_order(
                        restaurant_id=1,
                        phone="+57300",
                        order_total=Decimal("12500.00"),
                        order_summary="2 pizzas + bebida",
                    )
                )

        assert 1 in captured, "restaurant_id not in SQL params"
        assert "+57300" in captured, "phone not in SQL params"
        # The Decimal total must have been passed (quantized)
        decimal_vals = [a for a in captured if isinstance(a, Decimal)]
        assert decimal_vals, "No Decimal value found in SQL params"

    # ── 10. serialize returns "" for None or zero-order profiles ─────────────

    def test_serialize_empty_for_new_customer(self):
        import app.repositories.customer_profiles_repo as repo
        assert repo.serialize_for_prompt(None) == ""
        assert repo.serialize_for_prompt(_make_profile({"total_orders": 0})) == ""

    # ── 11. serialize includes name and order count ───────────────────────────

    def test_serialize_includes_name_and_order_count(self):
        import app.repositories.customer_profiles_repo as repo
        profile = _make_profile({"total_orders": 5, "display_name": "Ana"})
        result = repo.serialize_for_prompt(profile)
        assert "Ana" in result
        assert "5" in result

    # ── 12. serialize truncates to max_chars ─────────────────────────────────

    def test_serialize_truncates_to_max_chars(self):
        import app.repositories.customer_profiles_repo as repo
        profile = _make_profile({
            "total_orders": 100,
            "display_name": "A" * 100,
            "last_order_summary": "B" * 300,
            "preferences": {
                "dietary": "C" * 200,
                "allergies": "D" * 200,
                "notes": "E" * 200,
            },
        })
        max_chars = 80
        result = repo.serialize_for_prompt(profile, max_chars=max_chars)
        assert len(result) <= max_chars

    # ── 13. serialize strips newlines from preference values ─────────────────

    def test_serialize_strips_newlines_from_preferences(self):
        import app.repositories.customer_profiles_repo as repo
        profile = _make_profile({
            "total_orders": 3,
            "preferences": {
                "dietary": "vegano\nsin gluten",
                "allergies": "maní\nleche",
            },
        })
        result = repo.serialize_for_prompt(profile)
        assert "\n" not in result, f"Newline found in output: {result!r}"

    # ── 14. serialize never leaks phone or timestamps ─────────────────────────

    def test_serialize_never_leaks_phone_or_timestamps(self):
        import app.repositories.customer_profiles_repo as repo
        phone = "+573001234567"
        first_seen = "2026-01-01T10:00:00+00:00"
        profile = _make_profile({
            "phone": phone,
            "first_seen": first_seen,
            "total_orders": 7,
        })
        result = repo.serialize_for_prompt(profile)
        assert phone not in result, f"Phone leaked into prompt: {result!r}"
        assert first_seen not in result, f"Timestamp leaked into prompt: {result!r}"
        assert "restaurant_id" not in result
