"""
tests/test_pin_login_global_ratelimit.py
=========================================
Regression test for the PIN-login global IP rate-limit (defense-in-depth
against distributed brute-force iterating restaurant_id).

The per (restaurant_id, name, IP) bucket alone allowed 10 attempts per
bucket — an attacker iterating restaurant_id=1..1000 could try 10K
(name="Pedro", pin="1234") attempts before any single bucket triggered.
The global per-IP cap closes that window.
"""

import pytest
from unittest.mock import AsyncMock

from app.routes import staff as staff_route


@pytest.mark.asyncio
async def test_global_rate_limit_short_circuits_before_bucket(monkeypatch):
    """When the global per-IP limit is hit, the granular bucket check is NOT
    reached — the request fails fast with 429."""
    calls = []

    async def fake_rl(key, max_requests, window_seconds):
        calls.append(key)
        if key.startswith("pin_login_global:"):
            return False  # global limit exhausted
        return True  # bucket would still allow — but we should never get here

    monkeypatch.setattr(staff_route.state_store, "rate_limit_check", fake_rl)

    # Build a mock Request with a client.host attribute.
    class _MockClient:
        host = "1.2.3.4"
    class _MockRequest:
        client = _MockClient()

    with pytest.raises(staff_route.HTTPException) as exc_info:
        await staff_route._check_pin_rate_limit(_MockRequest(), 42, "Pedro")

    assert exc_info.value.status_code == 429
    # Only the global key was consulted — the granular bucket was never reached.
    assert calls == ["pin_login_global:1.2.3.4"]


@pytest.mark.asyncio
async def test_global_passes_then_bucket_checked(monkeypatch):
    """When global allows, the granular bucket IS checked next."""
    calls = []

    async def fake_rl(key, max_requests, window_seconds):
        calls.append(key)
        return True  # both layers allow

    monkeypatch.setattr(staff_route.state_store, "rate_limit_check", fake_rl)

    class _MockClient:
        host = "5.6.7.8"
    class _MockRequest:
        client = _MockClient()

    # No exception expected — both layers passed.
    await staff_route._check_pin_rate_limit(_MockRequest(), 99, "María")

    assert len(calls) == 2
    assert calls[0] == "pin_login_global:5.6.7.8"
    assert calls[1].startswith("pin_login:99:maría:5.6.7.8")


@pytest.mark.asyncio
async def test_bucket_blocks_after_global_passes(monkeypatch):
    """Bucket exhausted but global still allows: still 429 from bucket layer."""
    calls = []

    async def fake_rl(key, max_requests, window_seconds):
        calls.append(key)
        if key.startswith("pin_login_global:"):
            return True
        return False  # bucket exhausted

    monkeypatch.setattr(staff_route.state_store, "rate_limit_check", fake_rl)

    class _MockClient:
        host = "9.9.9.9"
    class _MockRequest:
        client = _MockClient()

    with pytest.raises(staff_route.HTTPException) as exc_info:
        await staff_route._check_pin_rate_limit(_MockRequest(), 1, "Juan")

    assert exc_info.value.status_code == 429
    assert len(calls) == 2  # Both layers were consulted.
