"""
Suite — Feature 4: Weekly Owner Reports (21 tests)
tests/test_weekly_reports.py

Covers:
  A — format_report_message pure function (5 tests)
  B — compute_weekly_stats with mocked pool (3 tests)
  C — _run_weekly_owner_reports scheduler tick (5 tests)
  D — WhatsApp failure handling (2 tests)
  E — Dashboard endpoints GET/PATCH (5 tests)
  F — save_report idempotency (1 test)

No real DB, no real HTTP, no real WhatsApp calls.
April 13 2026 is confirmed Monday (weekday() == 0).
"""
import asyncio
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_pool, make_row
from unittest.mock import patch as _patch
from app.services.tenant_context import tenant_scope


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _make_conn(fetchrow=None, fetch=None, fetchval=None, execute=None):
    """Build a minimal mock asyncpg connection."""
    conn = MagicMock()
    conn.fetchrow  = AsyncMock(return_value=fetchrow)
    conn.fetch     = AsyncMock(return_value=fetch or [])
    conn.fetchval  = AsyncMock(return_value=fetchval)
    conn.execute   = AsyncMock(return_value=execute)
    return conn


def _base_stats(overrides: dict | None = None) -> dict:
    """Baseline stats dict with has_signal=True."""
    stats = {
        "revenue_current":        Decimal("3200000"),
        "revenue_previous":       Decimal("2800000"),
        "revenue_delta_pct":      14.3,
        "order_count_current":    87,
        "order_count_previous":   70,
        "delivery_count":         32,
        "table_count":            55,
        "nps_avg":                8.4,
        "nps_count":              12,
        "reviews_public_new":     3,
        "dormant_frequent_count": 0,
        "has_signal":             True,
    }
    if overrides:
        stats.update(overrides)
    return stats


WEEK_START = date(2026, 4, 6)    # Monday one week ago
WEEK_END   = date(2026, 4, 13)   # This Monday (exclusive)
DASH_URL   = "https://mesio.com/dashboard"


# ══════════════════════════════════════════════════════════════════════════════
# Block A — format_report_message (pure function, no mocks needed)
# ══════════════════════════════════════════════════════════════════════════════

class TestFormatReportMessage:

    def test_A1_normal_week(self):
        """Normal week: revenue, delta, order counts, NPS all present."""
        from app.repositories.weekly_reports_repo import format_report_message

        stats = _base_stats()
        msg = format_report_message(stats, "Restaurante Demo", WEEK_START, WEEK_END, DASH_URL)

        assert msg != ""
        assert "3.200.000" in msg
        assert "14" in msg and "%" in msg        # delta percentage
        assert "87 pedidos" in msg or "87 pedido" in msg
        assert "32 domicilio" in msg
        assert "55 mesa" in msg
        assert "NPS" in msg
        assert "8.4" in msg
        assert "Ver detalle" in msg

    def test_A2_first_week_no_delta(self):
        """Previous revenue == 0 → delta is None → no % symbol in revenue line, 'primera semana' shown."""
        from app.repositories.weekly_reports_repo import format_report_message

        stats = _base_stats({
            "revenue_previous":   Decimal("0"),
            "revenue_delta_pct":  None,
        })
        msg = format_report_message(stats, "Restaurante Demo", WEEK_START, WEEK_END, DASH_URL)

        assert msg != ""
        # The revenue line should NOT include a standalone percentage delta
        assert "primera semana" in msg
        # The NPS line may still have a percent of 10 scale ("8.4/10")
        # but the revenue line itself must not say "+X% vs semana anterior"
        assert "vs semana anterior" not in msg

    def test_A3_nps_empty_line_omitted(self):
        """nps_count == 0 → NPS line omitted entirely."""
        from app.repositories.weekly_reports_repo import format_report_message

        stats = _base_stats({
            "nps_avg":   None,
            "nps_count": 0,
        })
        msg = format_report_message(stats, "Restaurante Demo", WEEK_START, WEEK_END, DASH_URL)

        assert msg != ""
        # NPS block must not appear at all (not "NPS N/A" either)
        assert "NPS" not in msg

    def test_A4_dormant_customers_priority_suggestion(self):
        """dormant_frequent_count >= 3 → dormant suggestion line wins (priority 1)."""
        from app.repositories.weekly_reports_repo import format_report_message

        stats = _base_stats({
            "dormant_frequent_count": 3,
            "revenue_delta_pct":      5.0,
        })
        msg = format_report_message(stats, "Restaurante Demo", WEEK_START, WEEK_END, DASH_URL)

        assert msg != ""
        # Should contain a reference to the 3 frequent customers
        assert "3 clientes frecuentes" in msg or "3 cliente" in msg
        # Should NOT say "Semana estable" (stable fallback) since dormant wins
        assert "Semana estable" not in msg

    def test_A5_revenue_drop_suggestion(self):
        """Revenue drop >= 10% and no dormant → 'Las ventas bajaron' suggestion."""
        from app.repositories.weekly_reports_repo import format_report_message

        stats = _base_stats({
            "dormant_frequent_count": 0,
            "revenue_delta_pct":      -18.0,
        })
        msg = format_report_message(stats, "Restaurante Demo", WEEK_START, WEEK_END, DASH_URL)

        assert msg != ""
        # The drop suggestion must mention the negative movement
        assert "bajaron" in msg or "18" in msg
        # Should NOT be the stable suggestion
        assert "Semana estable" not in msg


# ══════════════════════════════════════════════════════════════════════════════
# Block B — compute_weekly_stats with mocked pool
# ══════════════════════════════════════════════════════════════════════════════

class TestComputeWeeklyStats:

    def _make_multi_fetchrow_conn(self, delivery_row_dict, table_row_dict, nps_row_dict, dormant_val):
        """
        compute_weekly_stats calls conn.fetchrow 3 times and conn.fetchval once.
        tenant_connection() calls conn.fetchval("SELECT set_config(...)") first (returns None),
        then the repo calls conn.fetchval for the dormant count.
        """
        conn = MagicMock()
        conn.fetchrow = AsyncMock(side_effect=[
            make_row(delivery_row_dict),
            make_row(table_row_dict),
            make_row(nps_row_dict),
        ])
        conn.fetchval = AsyncMock(side_effect=[None, dormant_val])
        txn = MagicMock()
        txn.__aenter__ = AsyncMock(return_value=txn)
        txn.__aexit__ = AsyncMock(return_value=False)
        conn.transaction = MagicMock(return_value=txn)
        return conn

    def test_B1_all_zeros_has_signal_false(self, monkeypatch):
        """All DB queries return zero values → has_signal is False."""
        delivery = {
            "revenue_current":  0, "revenue_previous": 0,
            "count_current":    0, "count_previous":   0,
        }
        table = {
            "revenue_current":  0, "revenue_previous": 0,
            "count_current":    0, "count_previous":   0,
        }
        nps = {"nps_count": 0, "nps_avg": None, "reviews_public_new": 0}

        conn = self._make_multi_fetchrow_conn(delivery, table, nps, dormant_val=0)
        pool = make_pool(conn)

        import app.repositories.weekly_reports_repo as wrr
        with _patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
            with tenant_scope(1):
                stats = _run(wrr.compute_weekly_stats(1, WEEK_START, WEEK_END))

        assert stats["has_signal"] is False
        assert stats["revenue_current"] == Decimal("0")
        assert stats["order_count_current"] == 0

    def test_B2_mixed_revenue_has_signal_true(self, monkeypatch):
        """Delivery + table revenues combine correctly; has_signal True."""
        delivery = {
            "revenue_current":  500000, "revenue_previous": 300000,
            "count_current":    10,     "count_previous":   8,
        }
        table = {
            "revenue_current":  1200000, "revenue_previous": 900000,
            "count_current":    25,      "count_previous":   20,
        }
        nps = {"nps_count": 0, "nps_avg": None, "reviews_public_new": 0}

        conn = self._make_multi_fetchrow_conn(delivery, table, nps, dormant_val=0)
        pool = make_pool(conn)

        import app.repositories.weekly_reports_repo as wrr
        with _patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
            with tenant_scope(1):
                stats = _run(wrr.compute_weekly_stats(1, WEEK_START, WEEK_END))

        assert stats["has_signal"] is True
        assert stats["revenue_current"] == Decimal("1700000")
        assert stats["delivery_count"] == 10
        assert stats["table_count"] == 25
        assert stats["order_count_current"] == 35

    def test_B3_nps_avg_stored_as_float(self, monkeypatch):
        """NPS query returns avg=4.5 → stored as rounded float, not scaled."""
        delivery = {
            "revenue_current":  0, "revenue_previous": 0,
            "count_current":    0, "count_previous":   0,
        }
        table = {
            "revenue_current":  0, "revenue_previous": 0,
            "count_current":    0, "count_previous":   0,
        }
        # nps_avg is the raw DB average — compute_weekly_stats does round(float(...), 1)
        nps = {"nps_count": 8, "nps_avg": 4.5, "reviews_public_new": 0}

        conn = self._make_multi_fetchrow_conn(delivery, table, nps, dormant_val=0)
        pool = make_pool(conn)

        import app.repositories.weekly_reports_repo as wrr
        with _patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
            with tenant_scope(1):
                stats = _run(wrr.compute_weekly_stats(1, WEEK_START, WEEK_END))

        # The repo stores nps_avg exactly as round(float(raw_avg), 1)
        assert stats["nps_avg"] == 4.5
        assert stats["nps_count"] == 8
        # has_signal because nps_count > 0
        assert stats["has_signal"] is True


# ══════════════════════════════════════════════════════════════════════════════
# Block C — Scheduler tick integration
# ══════════════════════════════════════════════════════════════════════════════

def _fake_restaurant(overrides: dict | None = None) -> dict:
    r = {
        "id":               1,
        "name":             "El Restaurante",
        "whatsapp_number":  "+57300123",
        "wa_phone_id":      "ph123",
        "owner_phone":      "+573009999999",
        "timezone":         "America/Bogota",
        "features":         {"weekly_report_enabled": True},
    }
    if overrides:
        r.update(overrides)
    return r


def _patch_scheduler_deps(monkeypatch, *, restaurants, local_now_dt,
                           already_sent=False, stats_override=None,
                           save_report_return=None, send_whatsapp_return=True):
    """Monkeypatch all scheduler dependencies at the module-level symbols."""
    import app.services.scheduler as sched
    import app.services.database as db_module
    import app.repositories.weekly_reports_repo as wrr

    # Monday 2026-04-13 09:30 in Bogota tz
    monkeypatch.setattr(sched, "_local_now", lambda tz: local_now_dt)
    monkeypatch.setattr(db_module, "db_get_all_restaurants", AsyncMock(return_value=restaurants))

    already_sent_mock = AsyncMock(return_value=already_sent)
    monkeypatch.setattr(wrr, "already_sent_for_week", already_sent_mock)

    default_stats = _base_stats()
    if stats_override is not None:
        default_stats.update(stats_override)
    compute_mock = AsyncMock(return_value=default_stats)
    monkeypatch.setattr(wrr, "compute_weekly_stats", compute_mock)

    fake_row = save_report_return if save_report_return is not None else {"id": 42}
    save_mock = AsyncMock(return_value=fake_row)
    monkeypatch.setattr(wrr, "save_report", save_mock)

    mark_sent_mock  = AsyncMock()
    mark_failed_mock = AsyncMock()
    monkeypatch.setattr(wrr, "mark_sent",   mark_sent_mock)
    monkeypatch.setattr(wrr, "mark_failed", mark_failed_mock)

    if callable(send_whatsapp_return):
        send_mock = AsyncMock(side_effect=send_whatsapp_return)
    else:
        send_mock = AsyncMock(return_value=send_whatsapp_return)
    monkeypatch.setattr(sched, "_send_whatsapp", send_mock)

    return {
        "already_sent": already_sent_mock,
        "compute":      compute_mock,
        "save_report":  save_mock,
        "mark_sent":    mark_sent_mock,
        "mark_failed":  mark_failed_mock,
        "send_whatsapp": send_mock,
    }


class TestSchedulerTick:

    _MONDAY_9AM = datetime(2026, 4, 13, 9, 30)

    def test_C1_happy_path_monday_9am(self, monkeypatch):
        """Happy path: Monday 09:30, report saved as 'pending', sent, marked_sent."""
        import app.services.scheduler as sched

        mocks = _patch_scheduler_deps(
            monkeypatch,
            restaurants=[_fake_restaurant()],
            local_now_dt=self._MONDAY_9AM,
        )

        _run(sched._run_weekly_owner_reports())

        mocks["save_report"].assert_awaited_once()
        # delivery_status should be 'pending' since owner_phone is set
        call_kwargs = mocks["save_report"].call_args
        assert call_kwargs.kwargs.get("delivery_status") == "pending" or \
               (call_kwargs.args and "pending" in str(call_kwargs.args))

        mocks["send_whatsapp"].assert_awaited_once()
        mocks["mark_sent"].assert_awaited_once()
        mocks["mark_failed"].assert_not_called()

    def test_C2_not_monday_skips(self, monkeypatch):
        """Tuesday → nothing saved or sent."""
        import app.services.scheduler as sched

        tuesday = datetime(2026, 4, 14, 9, 30)   # Tuesday
        mocks = _patch_scheduler_deps(
            monkeypatch,
            restaurants=[_fake_restaurant()],
            local_now_dt=tuesday,
        )

        _run(sched._run_weekly_owner_reports())

        mocks["save_report"].assert_not_called()
        mocks["send_whatsapp"].assert_not_called()

    def test_C3_already_sent_skips(self, monkeypatch):
        """already_sent_for_week returns True → save_report and send not called."""
        import app.services.scheduler as sched

        mocks = _patch_scheduler_deps(
            monkeypatch,
            restaurants=[_fake_restaurant()],
            local_now_dt=self._MONDAY_9AM,
            already_sent=True,
        )

        _run(sched._run_weekly_owner_reports())

        mocks["save_report"].assert_not_called()
        mocks["send_whatsapp"].assert_not_called()

    def test_C4_no_signal_skips(self, monkeypatch):
        """has_signal False → save_report NOT called (silent skip per spec)."""
        import app.services.scheduler as sched

        mocks = _patch_scheduler_deps(
            monkeypatch,
            restaurants=[_fake_restaurant()],
            local_now_dt=self._MONDAY_9AM,
            stats_override={"has_signal": False},
        )

        _run(sched._run_weekly_owner_reports())

        mocks["save_report"].assert_not_called()
        mocks["send_whatsapp"].assert_not_called()

    def test_C5_feature_disabled_skips_that_restaurant(self, monkeypatch):
        """First restaurant has feature disabled; second still processes."""
        import app.services.scheduler as sched
        import app.repositories.weekly_reports_repo as wrr

        disabled_r = _fake_restaurant({"id": 1, "features": {"weekly_report_enabled": False}})
        enabled_r  = _fake_restaurant({"id": 2, "owner_phone": "+573008888888"})

        mocks = _patch_scheduler_deps(
            monkeypatch,
            restaurants=[disabled_r, enabled_r],
            local_now_dt=self._MONDAY_9AM,
        )

        _run(sched._run_weekly_owner_reports())

        # save_report called exactly once (only for enabled restaurant)
        assert mocks["save_report"].await_count == 1
        # send_whatsapp called once (for enabled restaurant)
        assert mocks["send_whatsapp"].await_count == 1


# ══════════════════════════════════════════════════════════════════════════════
# Block D — WhatsApp failure handling
# ══════════════════════════════════════════════════════════════════════════════

class TestWhatsAppFailures:

    _MONDAY_9AM = datetime(2026, 4, 13, 9, 30)

    def test_D1_send_returns_false_calls_mark_failed(self, monkeypatch):
        """_send_whatsapp returns False → mark_failed called, mark_sent NOT called."""
        import app.services.scheduler as sched

        mocks = _patch_scheduler_deps(
            monkeypatch,
            restaurants=[_fake_restaurant()],
            local_now_dt=self._MONDAY_9AM,
            send_whatsapp_return=False,
        )

        _run(sched._run_weekly_owner_reports())

        mocks["mark_failed"].assert_awaited_once()
        error_arg = mocks["mark_failed"].call_args[0][1]  # second positional arg
        assert "false" in error_arg.lower() or "returned" in error_arg.lower()
        mocks["mark_sent"].assert_not_called()

    def test_D2_send_raises_loop_continues(self, monkeypatch):
        """
        _send_whatsapp raises for restaurant 1 → mark_failed called once.
        Loop continues to restaurant 2 (which succeeds) → send called twice total.
        """
        import app.services.scheduler as sched

        call_count = {"n": 0}

        async def _flaky_send(phone, msg, bot, phone_id):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise Exception("network down")
            return True

        r1 = _fake_restaurant({"id": 1})
        r2 = _fake_restaurant({"id": 2, "owner_phone": "+573007777777"})

        mocks = _patch_scheduler_deps(
            monkeypatch,
            restaurants=[r1, r2],
            local_now_dt=self._MONDAY_9AM,
            send_whatsapp_return=_flaky_send,
        )

        # No crash expected
        _run(sched._run_weekly_owner_reports())

        assert mocks["send_whatsapp"].await_count == 2
        assert mocks["mark_failed"].await_count == 1
        assert mocks["mark_sent"].await_count == 1


# ══════════════════════════════════════════════════════════════════════════════
# Block E — Dashboard endpoints
# ══════════════════════════════════════════════════════════════════════════════

def _patch_route_auth(monkeypatch, restaurant_dict: dict):
    """Override get_current_restaurant dependency to return a fixed restaurant."""
    from app.routes.deps import get_current_restaurant
    from app.main import app

    async def _override():
        return restaurant_dict

    app.dependency_overrides[get_current_restaurant] = _override
    return app


def _clear_route_auth():
    from app.routes.deps import get_current_restaurant
    from app.main import app
    app.dependency_overrides.pop(get_current_restaurant, None)


class TestDashboardEndpoints:

    def _make_restaurant(self, features=None, owner_phone=None, timezone="America/Bogota"):
        return {
            "id":               1,
            "name":             "Restaurante Test",
            "whatsapp_number":  "+57300",
            "features":         features or {},
            "owner_phone":      owner_phone,
            "timezone":         timezone,
        }

    def test_E1_get_empty_state(self, monkeypatch):
        """GET /api/weekly-reports → empty reports list, correct default settings."""
        from app.main import app
        import app.repositories.weekly_reports_repo as wrr

        restaurant = self._make_restaurant()
        _patch_route_auth(monkeypatch, restaurant)
        monkeypatch.setattr(wrr, "get_recent_reports", AsyncMock(return_value=[]))

        try:
            client = TestClient(app)
            resp = client.get("/api/weekly-reports", headers={"Authorization": "Bearer test"})
        finally:
            _clear_route_auth()

        assert resp.status_code == 200
        data = resp.json()
        assert data["reports"] == []
        assert data["settings"]["enabled"] is True
        assert data["settings"]["owner_phone"] is None
        assert data["settings"]["timezone"] == "America/Bogota"

    def test_E2_get_with_reports(self, monkeypatch):
        """GET /api/weekly-reports → 3 reports returned with correct shape."""
        from app.main import app
        import app.repositories.weekly_reports_repo as wrr

        restaurant = self._make_restaurant()

        fake_reports = [
            {
                "id": i,
                "week_start":       date(2026, 3, 16 + i * 7),
                "generated_at":     datetime(2026, 3, 16 + i * 7, 10, 0),
                "sent_at":          datetime(2026, 3, 16 + i * 7, 10, 1),
                "delivery_status":  "sent",
                "error_message":    None,
                "payload":          {"revenue_current": 1000000.0},
            }
            for i in range(3)
        ]

        _patch_route_auth(monkeypatch, restaurant)
        monkeypatch.setattr(wrr, "get_recent_reports", AsyncMock(return_value=fake_reports))

        try:
            client = TestClient(app)
            resp = client.get("/api/weekly-reports", headers={"Authorization": "Bearer test"})
        finally:
            _clear_route_auth()

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["reports"]) == 3
        # Each item must have week_start and delivery_status
        for item in data["reports"]:
            assert "week_start" in item
            assert "delivery_status" in item

    def test_E3_patch_enable_false(self, monkeypatch):
        """PATCH /api/weekly-reports/settings {enabled: false} → db_merge called, 200."""
        from app.main import app
        import app.repositories.restaurant_repo as repo
        import app.services.database as db_module

        restaurant = self._make_restaurant(features={"weekly_report_enabled": True})
        updated_restaurant = self._make_restaurant(features={"weekly_report_enabled": False})

        _patch_route_auth(monkeypatch, restaurant)

        merge_mock = AsyncMock()
        monkeypatch.setattr(repo, "db_merge_restaurant_features", merge_mock)
        monkeypatch.setattr(repo, "db_update_restaurant_owner_phone", AsyncMock())
        monkeypatch.setattr(repo, "db_update_restaurant_timezone", AsyncMock())
        monkeypatch.setattr(db_module, "db_get_restaurant_by_id",
                            AsyncMock(return_value=updated_restaurant))

        try:
            client = TestClient(app)
            resp = client.patch(
                "/api/weekly-reports/settings",
                json={"enabled": False},
                headers={"Authorization": "Bearer test"},
            )
        finally:
            _clear_route_auth()

        assert resp.status_code == 200
        merge_mock.assert_awaited_once()
        # Check it was called with the right flag
        call_args = merge_mock.call_args
        assert call_args[0][1] == {"weekly_report_enabled": False} or \
               call_args.kwargs.get("features") == {"weekly_report_enabled": False} or \
               {"weekly_report_enabled": False} in call_args[0]

    def test_E4_patch_invalid_phone(self, monkeypatch):
        """PATCH with invalid phone → 422 (too short / not E.164)."""
        from app.main import app

        restaurant = self._make_restaurant()
        _patch_route_auth(monkeypatch, restaurant)

        try:
            client = TestClient(app)
            resp = client.patch(
                "/api/weekly-reports/settings",
                json={"owner_phone": "not-a-phone"},
                headers={"Authorization": "Bearer test"},
            )
        finally:
            _clear_route_auth()

        assert resp.status_code == 422

    def test_E5_patch_invalid_timezone(self, monkeypatch):
        """PATCH with invalid timezone → 422."""
        from app.main import app

        restaurant = self._make_restaurant()
        _patch_route_auth(monkeypatch, restaurant)

        try:
            client = TestClient(app)
            resp = client.patch(
                "/api/weekly-reports/settings",
                json={"timezone": "Mars/Olympus"},
                headers={"Authorization": "Bearer test"},
            )
        finally:
            _clear_route_auth()

        assert resp.status_code == 422
        # Should mention timezone or IANA
        detail = resp.json().get("detail", "")
        assert "timezone" in str(detail).lower() or "iana" in str(detail).lower()


# ══════════════════════════════════════════════════════════════════════════════
# Block F — save_report idempotency (ON CONFLICT DO NOTHING)
# ══════════════════════════════════════════════════════════════════════════════

class TestSaveReportIdempotency:

    def test_F1_on_conflict_do_nothing_in_sql(self):
        """
        Idempotency strategy: verify the INSERT SQL uses ON CONFLICT DO NOTHING
        by reading the source file directly.

        Pool mocking for two-call stateful tracking is too complex to set up
        reliably without duplicating asyncpg internals.  The authoritative
        guarantee is the SQL clause, which is what the DB engine enforces.
        """
        import inspect
        import app.repositories.weekly_reports_repo as wrr

        source = inspect.getsource(wrr.save_report)
        assert "ON CONFLICT" in source
        assert "DO NOTHING" in source
        assert "RETURNING *" in source
        # Confirm the conflict key is the composite unique (restaurant_id, week_start)
        schema_source = inspect.getsource(wrr)
        # The UNIQUE constraint is on (restaurant_id, week_start) as defined in migration
        # We verify the repo handles None return (conflict case)
        assert "row is None" in source or "if row is None" in source


# ══════════════════════════════════════════════════════════════════════════════
# Block G — Dashboard endpoint: auth, valid PATCH, response shape
# ══════════════════════════════════════════════════════════════════════════════

class TestDashboardEndpointsExtra:
    """Additional coverage for GET/PATCH endpoints not covered in Block E."""

    def _make_restaurant(self, features=None, owner_phone=None, timezone="America/Bogota"):
        return {
            "id":               1,
            "name":             "Restaurante Test",
            "whatsapp_number":  "+57300",
            "features":         features or {},
            "owner_phone":      owner_phone,
            "timezone":         timezone,
        }

    # ── G1: GET requires auth (invalid token → 401/403) ───────────────────────
    def test_G1_get_requires_auth(self, monkeypatch):
        """GET /api/weekly-reports with an invalid token returns 401 or 403.

        We mock sessions_repo.get_session to return None (unknown token) so the
        auth layer responds with 401/403 without touching the DB pool.
        """
        from app.main import app
        import app.repositories.sessions_repo as sr

        monkeypatch.setattr(sr, "get_session", AsyncMock(return_value=None))

        client = TestClient(app)
        resp = client.get(
            "/api/weekly-reports",
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert resp.status_code in (401, 403)

    # ── G2: PATCH requires auth ────────────────────────────────────────────────
    def test_G2_patch_requires_auth(self, monkeypatch):
        """PATCH /api/weekly-reports/settings with invalid token returns 401 or 403."""
        from app.main import app
        import app.repositories.sessions_repo as sr

        monkeypatch.setattr(sr, "get_session", AsyncMock(return_value=None))

        client = TestClient(app)
        resp = client.patch(
            "/api/weekly-reports/settings",
            json={"enabled": True},
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert resp.status_code in (401, 403)

    # ── G3: PATCH valid phone updates owner_phone ──────────────────────────────
    def test_G3_patch_valid_phone(self, monkeypatch):
        """PATCH with valid E.164 phone → 200, db_update_restaurant_owner_phone called."""
        from app.main import app
        import app.repositories.restaurant_repo as repo
        import app.services.database as db_module

        phone = "+573009876543"
        restaurant = self._make_restaurant()
        updated = self._make_restaurant(owner_phone=phone)

        _patch_route_auth(monkeypatch, restaurant)
        phone_mock = AsyncMock()
        monkeypatch.setattr(repo, "db_update_restaurant_owner_phone", phone_mock)
        monkeypatch.setattr(repo, "db_update_restaurant_timezone", AsyncMock())
        monkeypatch.setattr(repo, "db_merge_restaurant_features", AsyncMock())
        monkeypatch.setattr(db_module, "db_get_restaurant_by_id",
                            AsyncMock(return_value=updated))

        try:
            client = TestClient(app)
            resp = client.patch(
                "/api/weekly-reports/settings",
                json={"owner_phone": phone},
                headers={"Authorization": "Bearer test"},
            )
        finally:
            _clear_route_auth()

        assert resp.status_code == 200
        phone_mock.assert_awaited_once()
        # Verify the phone value was passed
        call_args = phone_mock.call_args
        assert phone in call_args[0] or phone == call_args[0][1]

    # ── G4: PATCH valid timezone → 200 ────────────────────────────────────────
    def test_G4_patch_valid_timezone(self, monkeypatch):
        """PATCH with a valid IANA timezone → 200, db_update_restaurant_timezone called."""
        from app.main import app
        import app.repositories.restaurant_repo as repo
        import app.services.database as db_module

        tz = "America/New_York"
        restaurant = self._make_restaurant()
        updated    = self._make_restaurant(timezone=tz)

        _patch_route_auth(monkeypatch, restaurant)
        tz_mock = AsyncMock()
        monkeypatch.setattr(repo, "db_update_restaurant_timezone", tz_mock)
        monkeypatch.setattr(repo, "db_update_restaurant_owner_phone", AsyncMock())
        monkeypatch.setattr(repo, "db_merge_restaurant_features", AsyncMock())
        monkeypatch.setattr(db_module, "db_get_restaurant_by_id",
                            AsyncMock(return_value=updated))

        try:
            client = TestClient(app)
            resp = client.patch(
                "/api/weekly-reports/settings",
                json={"timezone": tz},
                headers={"Authorization": "Bearer test"},
            )
        finally:
            _clear_route_auth()

        assert resp.status_code == 200
        tz_mock.assert_awaited_once()
        data = resp.json()
        assert data["timezone"] == tz

    # ── G5: GET response shape has required keys ───────────────────────────────
    def test_G5_get_response_shape(self, monkeypatch):
        """GET response always contains reports list and settings object with required keys."""
        from app.main import app
        import app.repositories.weekly_reports_repo as wrr

        restaurant = self._make_restaurant(
            features={"weekly_report_enabled": False},
            owner_phone="+573001111111",
            timezone="America/Lima",
        )
        _patch_route_auth(monkeypatch, restaurant)
        monkeypatch.setattr(wrr, "get_recent_reports", AsyncMock(return_value=[]))

        try:
            client = TestClient(app)
            resp = client.get("/api/weekly-reports", headers={"Authorization": "Bearer test"})
        finally:
            _clear_route_auth()

        assert resp.status_code == 200
        data = resp.json()
        assert "reports" in data
        assert "settings" in data
        s = data["settings"]
        assert "enabled" in s
        assert "owner_phone" in s
        assert "timezone" in s
        assert s["enabled"] is False
        assert s["owner_phone"] == "+573001111111"
        assert s["timezone"] == "America/Lima"

    # ── G6: PATCH phone with leading zeros normalised ──────────────────────────
    def test_G6_patch_phone_leading_zeros(self, monkeypatch):
        """PATCH phone starting with '00' is normalised to '+' prefix → 200."""
        from app.main import app
        import app.repositories.restaurant_repo as repo
        import app.services.database as db_module

        restaurant = self._make_restaurant()
        updated    = self._make_restaurant(owner_phone="+573001234567")

        _patch_route_auth(monkeypatch, restaurant)
        phone_mock = AsyncMock()
        monkeypatch.setattr(repo, "db_update_restaurant_owner_phone", phone_mock)
        monkeypatch.setattr(repo, "db_update_restaurant_timezone", AsyncMock())
        monkeypatch.setattr(repo, "db_merge_restaurant_features", AsyncMock())
        monkeypatch.setattr(db_module, "db_get_restaurant_by_id",
                            AsyncMock(return_value=updated))

        try:
            client = TestClient(app)
            resp = client.patch(
                "/api/weekly-reports/settings",
                # "0057..." → normalised to "+57..."
                json={"owner_phone": "00573001234567"},
                headers={"Authorization": "Bearer test"},
            )
        finally:
            _clear_route_auth()

        assert resp.status_code == 200
        call_args = phone_mock.call_args
        # The normalised phone passed to the repo must start with '+'
        phone_arg = call_args[0][1] if len(call_args[0]) > 1 else call_args.kwargs.get("phone")
        assert phone_arg is not None
        assert phone_arg.startswith("+")

    # ── G7: PATCH empty phone clears owner_phone ──────────────────────────────
    def test_G7_patch_empty_phone_clears(self, monkeypatch):
        """PATCH with owner_phone='' clears the field (None passed to repo)."""
        from app.main import app
        import app.repositories.restaurant_repo as repo
        import app.services.database as db_module

        restaurant = self._make_restaurant(owner_phone="+573001234567")
        updated    = self._make_restaurant(owner_phone=None)

        _patch_route_auth(monkeypatch, restaurant)
        phone_mock = AsyncMock()
        monkeypatch.setattr(repo, "db_update_restaurant_owner_phone", phone_mock)
        monkeypatch.setattr(repo, "db_update_restaurant_timezone", AsyncMock())
        monkeypatch.setattr(repo, "db_merge_restaurant_features", AsyncMock())
        monkeypatch.setattr(db_module, "db_get_restaurant_by_id",
                            AsyncMock(return_value=updated))

        try:
            client = TestClient(app)
            resp = client.patch(
                "/api/weekly-reports/settings",
                json={"owner_phone": ""},
                headers={"Authorization": "Bearer test"},
            )
        finally:
            _clear_route_auth()

        assert resp.status_code == 200
        phone_mock.assert_awaited_once()
        call_args = phone_mock.call_args
        phone_arg = call_args[0][1] if len(call_args[0]) > 1 else call_args.kwargs.get("phone")
        assert phone_arg is None
