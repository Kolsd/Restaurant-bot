"""
tests/test_alerts.py

Unit tests for app.services.alerts.

No real DB or network calls — everything is mocked.
All async tests use pytest-asyncio (already in the project via pytest).
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_pool(fetchval_return: int = 0, idle: int = 5, size: int = 20):
    """Build a minimal asyncpg pool mock."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=fetchval_return)

    acquire_cm = AsyncMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    pool.get_idle_size = MagicMock(return_value=idle)
    pool.get_size = MagicMock(return_value=size)
    return pool


def _healthy_metrics() -> dict:
    return {
        "inbox_processed_total": 100,
        "inbox_errors_total": 2,          # 2% — below 10% threshold
        "inbox_latency_avg_ms": 50.0,
        "inbox_latency_p95_ms": 120.0,    # below 500ms threshold
        "inbox_latency_samples": 80,
    }


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestCheckAlertsNoIssues:
    @pytest.mark.asyncio
    async def test_no_alerts_fired_when_healthy(self, monkeypatch):
        """Healthy metrics → _fire_alert is never called."""
        import app.services.alerts as alerts_mod

        # Clean cooldown state before test
        alerts_mod._last_alert.clear()

        healthy_pool = _make_pool(fetchval_return=0, idle=10, size=20)

        fired = []

        async def _mock_fire(key, severity, title, detail):
            fired.append(key)

        monkeypatch.setattr(alerts_mod, "_fire_alert", _mock_fire)

        # get_pool is a late import inside each check function, so patch at source
        with (
            patch("app.services.database.get_pool", AsyncMock(return_value=healthy_pool)),  # noqa: SIM117
            patch(
                "app.services.inbox_worker.get_metrics",
                MagicMock(return_value=_healthy_metrics()),
            ),
        ):
            from app.services.alerts import check_alerts
            await check_alerts()

        assert fired == [], f"Expected no alerts but got: {fired}"


class TestDeadLetterAlert:
    @pytest.mark.asyncio
    async def test_dead_letter_fires_high_alert(self, monkeypatch):
        """Dead letters > 0 → HIGH alert is fired."""
        import app.services.alerts as alerts_mod

        alerts_mod._last_alert.clear()

        # Pool returns 3 dead letters for the first fetchval call (dead_letters check)
        # and 0 for the second (queue depth check).
        dead_pool = _make_pool(fetchval_return=3, idle=10, size=20)

        fired: list[dict] = []

        async def _capture_fire(key, severity, title, detail):
            fired.append({"key": key, "severity": severity})

        monkeypatch.setattr(alerts_mod, "_fire_alert", _capture_fire)

        with patch("app.services.database.get_pool", AsyncMock(return_value=dead_pool)):
            await alerts_mod._check_dead_letters()

        assert len(fired) == 1
        assert fired[0]["key"] == "dead_letters"
        assert fired[0]["severity"] == "HIGH"

    @pytest.mark.asyncio
    async def test_no_dead_letter_alert_when_zero(self, monkeypatch):
        """Dead letters == 0 → no alert."""
        import app.services.alerts as alerts_mod

        alerts_mod._last_alert.clear()

        pool = _make_pool(fetchval_return=0)
        fired: list[str] = []

        async def _capture_fire(key, severity, title, detail):
            fired.append(key)

        monkeypatch.setattr(alerts_mod, "_fire_alert", _capture_fire)

        with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
            await alerts_mod._check_dead_letters()

        assert fired == []


class TestPoolExhaustionAlert:
    @pytest.mark.asyncio
    async def test_critical_alert_when_pool_free_is_zero(self, monkeypatch):
        """Pool free == 0 → CRITICAL alert."""
        import app.services.alerts as alerts_mod

        alerts_mod._last_alert.clear()

        exhausted_pool = _make_pool(idle=0, size=10)
        fired: list[dict] = []

        async def _capture_fire(key, severity, title, detail):
            fired.append({"key": key, "severity": severity})

        monkeypatch.setattr(alerts_mod, "_fire_alert", _capture_fire)

        with patch("app.services.database.get_pool", AsyncMock(return_value=exhausted_pool)):
            await alerts_mod._check_pool_exhaustion()

        assert len(fired) == 1
        assert fired[0]["key"] == "db_pool_exhausted"
        assert fired[0]["severity"] == "CRITICAL"

    @pytest.mark.asyncio
    async def test_no_alert_when_pool_has_free_connections(self, monkeypatch):
        """Pool free > 0 → no alert."""
        import app.services.alerts as alerts_mod

        alerts_mod._last_alert.clear()

        pool = _make_pool(idle=5, size=20)
        fired: list[str] = []

        async def _capture_fire(key, severity, title, detail):
            fired.append(key)

        monkeypatch.setattr(alerts_mod, "_fire_alert", _capture_fire)

        with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
            await alerts_mod._check_pool_exhaustion()

        assert fired == []


class TestAlertCooldown:
    @pytest.mark.asyncio
    async def test_second_alert_suppressed_within_cooldown(self, monkeypatch):
        """Fire same alert twice rapidly — second call must be suppressed."""
        import app.services.alerts as alerts_mod
        import time

        alerts_mod._last_alert.clear()

        posted: list[str] = []

        async def _mock_post(url, *, key, severity, title, detail):
            posted.append(key)

        monkeypatch.setattr(alerts_mod, "_post_webhook", _mock_post)
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://example.com/hook")

        # Patch structlog so we don't need a real logger
        log_warnings: list[str] = []
        mock_log = MagicMock()
        mock_log.warning = MagicMock(side_effect=lambda event, **kw: log_warnings.append(event))
        monkeypatch.setattr(alerts_mod, "log", mock_log)

        # First call — should fire
        await alerts_mod._fire_alert(
            key="test_alert", severity="HIGH", title="Test", detail="First"
        )

        # Second call immediately — still within cooldown
        await alerts_mod._fire_alert(
            key="test_alert", severity="HIGH", title="Test", detail="Second"
        )

        # Only one webhook POST and one log.warning
        assert posted.count("test_alert") == 1
        assert log_warnings.count("alert.fired") == 1

    @pytest.mark.asyncio
    async def test_alert_fires_again_after_cooldown(self, monkeypatch):
        """Alert fires again once cooldown has expired."""
        import app.services.alerts as alerts_mod
        import time

        alerts_mod._last_alert.clear()

        # Simulate last alert was 400 seconds ago (beyond 300s cooldown)
        alerts_mod._last_alert["old_alert"] = time.monotonic() - 400

        log_warnings: list[str] = []
        mock_log = MagicMock()
        mock_log.warning = MagicMock(side_effect=lambda event, **kw: log_warnings.append(event))
        monkeypatch.setattr(alerts_mod, "log", mock_log)
        monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)

        await alerts_mod._fire_alert(
            key="old_alert", severity="MEDIUM", title="Stale", detail="Should fire"
        )

        assert "alert.fired" in log_warnings


class TestWebhookCalled:
    @pytest.mark.asyncio
    async def test_webhook_post_made_when_url_set(self, monkeypatch):
        """ALERT_WEBHOOK_URL set → POST is made with correct payload."""
        import app.services.alerts as alerts_mod

        alerts_mod._last_alert.clear()
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example.com/alert")

        captured_payloads: list[dict] = []

        class _MockResponse:
            status_code = 200

        class _MockClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, *, json):
                captured_payloads.append({"url": url, "json": json})
                return _MockResponse()

        # Patch structlog to avoid needing the full logging stack
        mock_log = MagicMock()
        mock_log.warning = MagicMock()
        monkeypatch.setattr(alerts_mod, "log", mock_log)

        with patch("app.services.alerts.httpx.AsyncClient", return_value=_MockClient()):
            await alerts_mod._fire_alert(
                key="db_pool_exhausted",
                severity="CRITICAL",
                title="DB Pool Exhausted",
                detail="0 free connections out of 10.",
            )

        assert len(captured_payloads) == 1
        payload = captured_payloads[0]
        assert payload["url"] == "https://hooks.example.com/alert"
        body = payload["json"]
        assert body["severity"] == "CRITICAL"
        assert body["key"] == "db_pool_exhausted"
        assert "CRITICAL" in body["text"]
        assert "timestamp" in body

    @pytest.mark.asyncio
    async def test_no_webhook_when_url_not_set(self, monkeypatch):
        """ALERT_WEBHOOK_URL not set → no HTTP call is made."""
        import app.services.alerts as alerts_mod

        alerts_mod._last_alert.clear()
        monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)

        mock_log = MagicMock()
        mock_log.warning = MagicMock()
        monkeypatch.setattr(alerts_mod, "log", mock_log)

        post_called = []

        async def _mock_post_webhook(url, *, key, severity, title, detail):
            post_called.append(url)

        monkeypatch.setattr(alerts_mod, "_post_webhook", _mock_post_webhook)

        await alerts_mod._fire_alert(
            key="test_no_hook",
            severity="LOW",
            title="Test",
            detail="Should not POST",
        )

        assert post_called == []


class TestInboxLatencyAlert:
    @pytest.mark.asyncio
    async def test_high_latency_fires_medium_alert(self, monkeypatch):
        """p95 > 500ms → MEDIUM alert."""
        import app.services.alerts as alerts_mod

        alerts_mod._last_alert.clear()

        fired: list[dict] = []

        async def _capture(key, severity, title, detail):
            fired.append({"key": key, "severity": severity})

        monkeypatch.setattr(alerts_mod, "_fire_alert", _capture)

        high_latency_metrics = {**_healthy_metrics(), "inbox_latency_p95_ms": 750.0}

        with patch("app.services.inbox_worker.get_metrics", MagicMock(return_value=high_latency_metrics)):
            await alerts_mod._check_inbox_latency()

        assert len(fired) == 1
        assert fired[0]["key"] == "high_inbox_latency"
        assert fired[0]["severity"] == "MEDIUM"


class TestQueueDepthAlert:
    @pytest.mark.asyncio
    async def test_queue_depth_over_threshold_fires_alert(self, monkeypatch):
        """Queue depth > 50 → HIGH alert."""
        import app.services.alerts as alerts_mod

        alerts_mod._last_alert.clear()

        pool = _make_pool(fetchval_return=55)
        fired: list[dict] = []

        async def _capture(key, severity, title, detail):
            fired.append({"key": key, "severity": severity})

        monkeypatch.setattr(alerts_mod, "_fire_alert", _capture)

        with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
            await alerts_mod._check_queue_depth()

        assert len(fired) == 1
        assert fired[0]["key"] == "inbox_queue_backup"
        assert fired[0]["severity"] == "HIGH"


class TestErrorRateAlert:
    @pytest.mark.asyncio
    async def test_error_rate_spike_fires_alert(self, monkeypatch):
        """Error rate > 10% → HIGH alert."""
        import app.services.alerts as alerts_mod

        alerts_mod._last_alert.clear()

        fired: list[dict] = []

        async def _capture(key, severity, title, detail):
            fired.append({"key": key, "severity": severity})

        monkeypatch.setattr(alerts_mod, "_fire_alert", _capture)

        spike_metrics = {
            **_healthy_metrics(),
            "inbox_processed_total": 80,
            "inbox_errors_total": 20,  # 20% error rate
        }

        with patch("app.services.inbox_worker.get_metrics", MagicMock(return_value=spike_metrics)):
            await alerts_mod._check_error_rate()

        assert len(fired) == 1
        assert fired[0]["key"] == "worker_error_spike"
        assert fired[0]["severity"] == "HIGH"

    @pytest.mark.asyncio
    async def test_no_alert_when_no_traffic(self, monkeypatch):
        """Zero processed and zero errors → no alert (avoid divide-by-zero)."""
        import app.services.alerts as alerts_mod

        alerts_mod._last_alert.clear()

        fired: list[str] = []

        async def _capture(key, severity, title, detail):
            fired.append(key)

        monkeypatch.setattr(alerts_mod, "_fire_alert", _capture)

        no_traffic_metrics = {
            **_healthy_metrics(),
            "inbox_processed_total": 0,
            "inbox_errors_total": 0,
        }

        with patch("app.services.inbox_worker.get_metrics", MagicMock(return_value=no_traffic_metrics)):
            await alerts_mod._check_error_rate()

        assert fired == []
