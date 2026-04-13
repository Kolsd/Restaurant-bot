"""
tests/test_inbox_metrics.py

Tests for inbox worker processing metrics.
"""
from app.services.inbox_worker import get_metrics, _metrics, _latencies


class TestInboxMetrics:

    def setup_method(self):
        """Reset metrics between tests."""
        _metrics["processed"] = 0
        _metrics["errors"] = 0
        _latencies.clear()

    def test_get_metrics_empty(self):
        """No data → zero metrics."""
        m = get_metrics()
        assert m["inbox_processed_total"] == 0
        assert m["inbox_errors_total"] == 0
        assert m["inbox_latency_avg_ms"] == 0.0
        assert m["inbox_latency_samples"] == 0

    def test_get_metrics_with_data(self):
        """After processing, metrics reflect the data."""
        _metrics["processed"] = 10
        _metrics["errors"] = 2
        _latencies.extend([0.1, 0.2, 0.3, 0.15, 0.25])

        m = get_metrics()
        assert m["inbox_processed_total"] == 10
        assert m["inbox_errors_total"] == 2
        assert m["inbox_latency_samples"] == 5
        assert m["inbox_latency_avg_ms"] > 0

    def test_rolling_window_maxlen(self):
        """Deque maxlen=100 caps the rolling window."""
        for i in range(150):
            _latencies.append(0.01 * i)
        assert len(_latencies) == 100
        m = get_metrics()
        assert m["inbox_latency_samples"] == 100
