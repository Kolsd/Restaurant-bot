"""
tests/test_wompi_per_restaurant.py

Per-restaurant Wompi credential migration (legacy env-var fallback retained).

Covers:
  1. _wompi_credentials_from_restaurant returns (pk, integrity) when
     features.wompi is fully populated.
  2. Returns (None, None) when features.wompi is missing entirely.
  3. Partial config: returns (pk, None) or (None, integrity) so each side
     can fall back to env vars independently.
  4. Tolerates string-encoded JSON in features (asyncpg sometimes returns it).
  5. Returns (None, None) on malformed/None input — no crash.
  6. generate_wompi_payment_link uses explicit kwargs over env when provided.
  7. generate_wompi_payment_link falls back to env vars when kwargs are None.
  8. Raises RuntimeError when neither kwargs nor env supply credentials.
  9. Raises RuntimeError when only public_key resolves (no integrity).
  10. Settings GET response masks integrity_secret (returns last4 + bool flag).
  11. Settings POST preserves existing secret when client sends empty string.
  12. Settings POST overwrites secret when client sends a non-empty value.

Pure unit tests for credential extraction + link generation. Settings route
tests use FastAPI TestClient with mocked auth + repo stubs (no DB).
"""
from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from app.services.orders import (
    _wompi_credentials_from_restaurant,
    generate_wompi_payment_link,
)


# ── Credential extraction ────────────────────────────────────────────────────

def test_credentials_from_restaurant_returns_pair():
    """Fully-populated features.wompi returns both credentials."""
    restaurant = {
        "id": 42,
        "features": {
            "wompi": {
                "public_key": "pub_test_AbC123",
                "integrity_secret": "test_integrity_xyz999",
            }
        },
    }
    pk, integrity = _wompi_credentials_from_restaurant(restaurant)
    assert pk == "pub_test_AbC123"
    assert integrity == "test_integrity_xyz999"


def test_credentials_from_restaurant_missing_returns_nones():
    """No features.wompi → (None, None)."""
    assert _wompi_credentials_from_restaurant({"features": {}}) == (None, None)
    assert _wompi_credentials_from_restaurant({"features": None}) == (None, None)
    assert _wompi_credentials_from_restaurant({}) == (None, None)


def test_credentials_from_restaurant_partial_returns_independent():
    """Each credential falls back independently — partial returns the populated one only."""
    only_pk = {"features": {"wompi": {"public_key": "pub_test_AAA"}}}
    only_integrity = {"features": {"wompi": {"integrity_secret": "secret_BBB"}}}
    pk, integrity = _wompi_credentials_from_restaurant(only_pk)
    assert pk == "pub_test_AAA"
    assert integrity is None
    pk2, integrity2 = _wompi_credentials_from_restaurant(only_integrity)
    assert pk2 is None
    assert integrity2 == "secret_BBB"


def test_credentials_from_restaurant_handles_string_features():
    """asyncpg may deliver features as a JSON string — must parse."""
    restaurant = {
        "features": json.dumps({"wompi": {"public_key": "pub_x", "integrity_secret": "sec_x"}}),
    }
    pk, integrity = _wompi_credentials_from_restaurant(restaurant)
    assert pk == "pub_x"
    assert integrity == "sec_x"


def test_credentials_from_restaurant_tolerates_garbage():
    """Malformed inputs degrade gracefully to (None, None)."""
    assert _wompi_credentials_from_restaurant(None) == (None, None)
    assert _wompi_credentials_from_restaurant({"features": "not-json"}) == (None, None)
    assert _wompi_credentials_from_restaurant({"features": {"wompi": "not-a-dict"}}) == (None, None)
    # Whitespace-only credentials → None
    r = {"features": {"wompi": {"public_key": "   ", "integrity_secret": ""}}}
    assert _wompi_credentials_from_restaurant(r) == (None, None)


# ── Link generation ──────────────────────────────────────────────────────────

@patch.object(__import__("app.services.orders", fromlist=["WOMPI_PUBLIC_KEY"]), "WOMPI_PUBLIC_KEY", None)
@patch.object(__import__("app.services.orders", fromlist=["WOMPI_INTEGRITY_SECRET"]), "WOMPI_INTEGRITY_SECRET", None)
def test_generate_wompi_link_uses_explicit_credentials():
    """Explicit kwargs supply both credentials — env is ignored."""
    url = generate_wompi_payment_link(
        order_id="ORD-TEST-01",
        amount=10000,
        currency="COP",
        public_key="pub_test_FROM_KWARG",
        integrity_secret="secret_FROM_KWARG",
    )
    assert "public-key=pub_test_FROM_KWARG" in url
    assert "amount-in-cents=10000" in url
    assert "reference=ORD-TEST-01" in url
    # Signature is sha256(orderid + cents + currency + secret); we don't
    # recompute it here, but it MUST be present (32-byte hex)
    assert "signature:integrity=" in url
    # Make sure the env values (None) didn't leak in
    assert "public-key=None" not in url


def test_generate_wompi_link_falls_back_to_env(monkeypatch):
    """When kwargs are None, the env-var values are used."""
    import app.services.orders as orders_mod
    monkeypatch.setattr(orders_mod, "WOMPI_PUBLIC_KEY", "pub_env_KEY")
    monkeypatch.setattr(orders_mod, "WOMPI_INTEGRITY_SECRET", "secret_env_KEY")
    url = generate_wompi_payment_link(
        order_id="ORD-ENV-01",
        amount=5000,
        currency="COP",
    )
    assert "public-key=pub_env_KEY" in url
    assert "amount-in-cents=5000" in url


def test_generate_wompi_link_kwargs_override_env(monkeypatch):
    """When both env and kwargs are set, kwargs win."""
    import app.services.orders as orders_mod
    monkeypatch.setattr(orders_mod, "WOMPI_PUBLIC_KEY", "pub_env_LOSER")
    monkeypatch.setattr(orders_mod, "WOMPI_INTEGRITY_SECRET", "secret_env_LOSER")
    url = generate_wompi_payment_link(
        order_id="ORD-KW-01",
        amount=7500,
        currency="COP",
        public_key="pub_kw_WINNER",
        integrity_secret="secret_kw_WINNER",
    )
    assert "public-key=pub_kw_WINNER" in url
    assert "pub_env_LOSER" not in url


def test_generate_wompi_link_raises_when_neither_configured(monkeypatch):
    """No kwargs and no env → RuntimeError (caller falls back to manual proof)."""
    import app.services.orders as orders_mod
    monkeypatch.setattr(orders_mod, "WOMPI_PUBLIC_KEY", None)
    monkeypatch.setattr(orders_mod, "WOMPI_INTEGRITY_SECRET", None)
    with pytest.raises(RuntimeError, match="WOMPI_INTEGRITY_SECRET"):
        generate_wompi_payment_link("ORD-X", 1000)


def test_generate_wompi_link_raises_when_only_public_key(monkeypatch):
    """Public key without integrity → RuntimeError. Integrity is the gate."""
    import app.services.orders as orders_mod
    monkeypatch.setattr(orders_mod, "WOMPI_PUBLIC_KEY", "pub_test_X")
    monkeypatch.setattr(orders_mod, "WOMPI_INTEGRITY_SECRET", None)
    with pytest.raises(RuntimeError, match="WOMPI_INTEGRITY_SECRET"):
        generate_wompi_payment_link("ORD-X", 1000, public_key=None, integrity_secret=None)


def test_generate_wompi_link_signature_changes_with_secret():
    """Different integrity secrets produce different signatures (regression check)."""
    url1 = generate_wompi_payment_link(
        "ORD-A", 1000, "COP",
        public_key="pub_x", integrity_secret="secret_AAA",
    )
    url2 = generate_wompi_payment_link(
        "ORD-A", 1000, "COP",
        public_key="pub_x", integrity_secret="secret_BBB",
    )
    sig1 = url1.split("signature:integrity=")[1].split("&")[0]
    sig2 = url2.split("signature:integrity=")[1].split("&")[0]
    assert sig1 != sig2


# ── Settings response masking ────────────────────────────────────────────────

def test_mask_wompi_for_response_full():
    """Configured wompi → integrity_secret_set=True + last4 hint, no plaintext leaked."""
    from app.routes.settings_routes import _mask_wompi_for_response
    features = {
        "wompi": {
            "public_key": "pub_test_VISIBLE",
            "integrity_secret": "test_integrity_abc1234567",
        },
    }
    masked = _mask_wompi_for_response(features)
    assert masked["wompi"]["public_key"] == "pub_test_VISIBLE"
    assert masked["wompi"]["integrity_secret_set"] is True
    assert masked["wompi"]["integrity_secret_last4"] == "4567"
    # Plaintext secret MUST NOT appear anywhere
    assert "integrity_secret" not in masked["wompi"]
    assert "test_integrity_abc1234567" not in json.dumps(masked)


def test_mask_wompi_for_response_empty():
    """No wompi config → empty defaults; nothing leaks."""
    from app.routes.settings_routes import _mask_wompi_for_response
    masked = _mask_wompi_for_response({})
    assert masked["wompi"]["public_key"] == ""
    assert masked["wompi"]["integrity_secret_set"] is False
    assert masked["wompi"]["integrity_secret_last4"] == ""


def test_mask_wompi_for_response_partial():
    """Public key set, no secret → set=False, last4 empty."""
    from app.routes.settings_routes import _mask_wompi_for_response
    features = {"wompi": {"public_key": "pub_test_X"}}
    masked = _mask_wompi_for_response(features)
    assert masked["wompi"]["public_key"] == "pub_test_X"
    assert masked["wompi"]["integrity_secret_set"] is False


# ── Settings GET/POST endpoint integration ──────────────────────────────────

@pytest.fixture
def _wompi_auth_setup(monkeypatch):
    """Monkeypatch auth + db_get_restaurant_by_id and capture features merges."""
    from app.services import database as db_mod
    from app.repositories import restaurant_repo

    state = {"features": {"wompi": {"public_key": "pub_initial", "integrity_secret": "secret_initial_LAST"}}}

    async def _verify_token(_token):
        return "owner_test"

    async def _get_user(_username):
        return {
            "username": "owner_test",
            "branch_id": 1,
            "role": "owner",
            "restaurant_name": "Test",
        }

    async def _get_restaurant_by_id(_id):
        return {
            "id": 1,
            "org_id": 1,
            "location_id": 1,
            "name": "Test",
            "whatsapp_number": "+573000000000",
            "address": "",
            "features": dict(state["features"]),
        }

    async def _merge_features(_id, patch):
        # Mimic merge: shallow update
        merged = dict(state["features"])
        merged.update(patch)
        state["features"] = merged
        return merged

    async def _update_location(_id, **_kwargs):
        return None

    async def _check_module(_id, _name):
        return False

    monkeypatch.setattr("app.routes.deps.verify_token", _verify_token)
    monkeypatch.setattr(db_mod, "db_get_user", _get_user)
    monkeypatch.setattr(db_mod, "db_get_restaurant_by_id", _get_restaurant_by_id)
    monkeypatch.setattr(db_mod, "db_check_module", _check_module)
    monkeypatch.setattr(restaurant_repo, "db_merge_restaurant_features", _merge_features)
    monkeypatch.setattr(restaurant_repo, "db_update_location", _update_location)
    # Disable RLS scope side-effects by no-oping tenant_scope inside the route
    return state


def test_get_settings_masks_secret(client, _wompi_auth_setup):
    """GET /api/settings must NEVER return the integrity_secret in plaintext."""
    res = client.get("/api/settings", headers={"Authorization": "Bearer fake"})
    assert res.status_code == 200, res.text
    data = res.json()
    body = json.dumps(data)
    # Plaintext secret must not appear anywhere in the response
    assert "secret_initial_LAST" not in body
    # Response must include the masked summary
    assert data["wompi"]["public_key"] == "pub_initial"
    assert data["wompi"]["integrity_secret_set"] is True
    assert data["wompi"]["integrity_secret_last4"] == "LAST"


def test_post_settings_preserves_secret_when_empty(client, _wompi_auth_setup):
    """Empty integrity_secret in payload preserves the previously-stored value."""
    state = _wompi_auth_setup
    res = client.post(
        "/api/settings",
        headers={"Authorization": "Bearer fake", "Content-Type": "application/json"},
        json={
            "wompi": {
                "public_key": "pub_NEW",
                "integrity_secret": "",  # admin only changed the public key
            },
        },
    )
    assert res.status_code == 200, res.text
    # State should reflect: public_key updated, secret preserved
    saved = state["features"]["wompi"]
    assert saved["public_key"] == "pub_NEW"
    assert saved["integrity_secret"] == "secret_initial_LAST"


def test_post_settings_overwrites_secret_when_provided(client, _wompi_auth_setup):
    """Non-empty integrity_secret replaces the stored value."""
    state = _wompi_auth_setup
    res = client.post(
        "/api/settings",
        headers={"Authorization": "Bearer fake", "Content-Type": "application/json"},
        json={
            "wompi": {
                "public_key": "pub_NEWER",
                "integrity_secret": "secret_REPLACEMENT",
            },
        },
    )
    assert res.status_code == 200, res.text
    saved = state["features"]["wompi"]
    assert saved["public_key"] == "pub_NEWER"
    assert saved["integrity_secret"] == "secret_REPLACEMENT"


def test_post_settings_ignores_masked_placeholder(client, _wompi_auth_setup):
    """If the client somehow re-sends a masked placeholder, the existing secret
    is preserved (defense-in-depth — frontend shouldn't send these but we
    guard against it anyway)."""
    state = _wompi_auth_setup
    res = client.post(
        "/api/settings",
        headers={"Authorization": "Bearer fake", "Content-Type": "application/json"},
        json={
            "wompi": {
                "public_key": "pub_X",
                "integrity_secret": "xxxx_masked",
            },
        },
    )
    assert res.status_code == 200, res.text
    saved = state["features"]["wompi"]
    # Original secret preserved despite the masked-looking value
    assert saved["integrity_secret"] == "secret_initial_LAST"
