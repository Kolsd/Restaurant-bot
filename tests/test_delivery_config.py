"""
tests/test_delivery_config.py
==============================
Unit tests for app/services/delivery.py — the single source of truth for
delivery/pickup config resolution precedence:

    locations.delivery_config -> organizations.features (legacy) -> defaults

No database required — pure function tests against real dict/Decimal
values (never mocking the whole module, per CLAUDE.md test discipline).
"""

from decimal import Decimal

from app.services.delivery import delivery_config_to_json, get_delivery_config


def test_defaults_when_nothing_is_configured():
    config = get_delivery_config(org=None, location=None)

    assert config["delivery_enabled"] is False
    assert config["pickup_enabled"] is False
    assert config["delivery_fee"] == Decimal("0")
    assert config["min_order"] == Decimal("0")
    assert config["radius_km"] == Decimal("5")
    assert config["prep_minutes"] == 30
    assert config["payment_methods"] == []
    # Money keys must be real Decimal instances, never float/str/int.
    for key in ("delivery_fee", "min_order", "radius_km"):
        assert isinstance(config[key], Decimal), f"{key} must be Decimal, got {type(config[key])}"


def test_legacy_org_features_override_defaults():
    org = {
        "features": {
            "delivery_fee": 5000,
            "min_order": 20000,
            "delivery_radius_km": 8,
            "payment_methods": ["efectivo", "nequi"],
        }
    }

    config = get_delivery_config(org=org, location=None)

    assert config["delivery_fee"] == Decimal("5000")
    assert config["min_order"] == Decimal("20000")
    assert config["radius_km"] == Decimal("8")
    assert config["payment_methods"] == ["efectivo", "nequi"]
    # Legacy config never sets delivery_enabled/pickup_enabled — defaults stand.
    assert config["delivery_enabled"] is False
    assert config["pickup_enabled"] is False


def test_location_config_overrides_legacy_org_features():
    """Precedence: locations.delivery_config wins over organizations.features."""
    org = {
        "features": {
            "delivery_fee": 5000,
            "min_order": 20000,
            "delivery_radius_km": 8,
        }
    }
    location = {
        "delivery_config": {
            "delivery_enabled": True,
            "pickup_enabled": True,
            "delivery_fee": 3000,
            "prep_minutes": 45,
            "payment_methods": ["tarjeta"],
        }
    }

    config = get_delivery_config(org=org, location=location)

    # Overridden by the location.
    assert config["delivery_enabled"] is True
    assert config["pickup_enabled"] is True
    assert config["delivery_fee"] == Decimal("3000")
    assert config["prep_minutes"] == 45
    assert config["payment_methods"] == ["tarjeta"]
    # Not present in location config -> falls through to the org legacy value.
    assert config["min_order"] == Decimal("20000")
    assert config["radius_km"] == Decimal("8")


def test_location_config_partial_override_keeps_other_legacy_keys():
    """A location that only sets delivery_fee must still inherit min_order
    and radius_km from the org's legacy features, not silently reset them."""
    org = {"features": {"min_order": 15000, "delivery_radius_km": 4}}
    location = {"delivery_config": {"delivery_fee": 2500}}

    config = get_delivery_config(org=org, location=location)

    assert config["delivery_fee"] == Decimal("2500")
    assert config["min_order"] == Decimal("15000")
    assert config["radius_km"] == Decimal("4")


def test_features_as_json_string_is_tolerated():
    """asyncpg / the restaurants VIEW can hand back jsonb as a raw string —
    the helper must normalize it, matching the existing _features_dict
    convention (scheduler.py / subscription_guard.py)."""
    org = {"features": '{"delivery_fee": 4000, "min_order": 10000}'}

    config = get_delivery_config(org=org, location=None)

    assert config["delivery_fee"] == Decimal("4000")
    assert config["min_order"] == Decimal("10000")


def test_malformed_features_string_falls_back_to_defaults():
    org = {"features": "not-json-at-all"}

    config = get_delivery_config(org=org, location=None)

    assert config["delivery_fee"] == Decimal("0")
    assert config["min_order"] == Decimal("0")


def test_non_list_payment_methods_is_normalized_to_empty_list():
    location = {"delivery_config": {"payment_methods": "efectivo"}}

    config = get_delivery_config(org=None, location=location)

    assert config["payment_methods"] == []


def test_invalid_prep_minutes_falls_back_to_default():
    location = {"delivery_config": {"prep_minutes": "not-a-number"}}

    config = get_delivery_config(org=None, location=location)

    assert config["prep_minutes"] == 30


def test_delivery_config_to_json_converts_decimal_at_json_boundary():
    config = get_delivery_config(
        org={"features": {"delivery_fee": 5000}},
        location={"delivery_config": {"min_order": 12000}},
    )

    json_safe = delivery_config_to_json(config)

    assert isinstance(json_safe["delivery_fee"], float)
    assert json_safe["delivery_fee"] == 5000.0
    assert isinstance(json_safe["min_order"], float)
    assert json_safe["min_order"] == 12000.0
    # The original resolved config is untouched (still Decimal) — to_json
    # returns a copy, it doesn't mutate the input.
    assert isinstance(config["delivery_fee"], Decimal)
