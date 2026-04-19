"""
tests/test_db_get_all_orgs.py

Locks down db_get_all_orgs — the canonical Wave-2 enumeration primitive
for "every customer business".

Why this exists (Paso 8): db_get_all_restaurants leaned on the legacy
"matriz vs branch" mental model and the vestigial is_primary flag to
return one row per business. db_get_all_orgs returns ORGANIZATIONS
directly — no location join, no is_primary filter, no Matriz invariant
dependency. New code that conceptually iterates per-tenant should use
this primitive instead.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_conn(rows):
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    return conn


def _make_pool(conn):
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool


# ── SQL shape: queries organizations, NOT locations / restaurants VIEW ────────

async def test_returns_org_rows_not_locations():
    """Must SELECT FROM organizations — not from `restaurants` VIEW or
    `locations` table. Anything else perpetuates the old mental model."""
    from app.repositories.restaurant_repo import db_get_all_orgs

    conn = _make_conn([])
    pool = _make_pool(conn)
    with patch("app.repositories.restaurant_repo._get_pool", AsyncMock(return_value=pool)):
        await db_get_all_orgs()

    sql_blob = " ".join(
        str(c.args[0]) for c in conn.fetch.call_args_list if c.args
    ).lower()
    assert "from organizations" in sql_blob, "Must read from organizations table"
    assert "is_primary" not in sql_blob, (
        "Must NOT depend on is_primary (vestigial Matriz scaffold) — "
        "every location is equal in the Wave-2 model"
    )
    assert "join locations" not in sql_blob, (
        "Org-level enumeration must NOT join locations — that conflates "
        "the tenant boundary with the sede boundary"
    )


# ── active_only filter ────────────────────────────────────────────────────────

async def test_active_only_default_filters_subscription_status():
    """Default behavior: only active orgs (subscription_status='active')."""
    from app.repositories.restaurant_repo import db_get_all_orgs

    conn = _make_conn([])
    pool = _make_pool(conn)
    with patch("app.repositories.restaurant_repo._get_pool", AsyncMock(return_value=pool)):
        await db_get_all_orgs()  # default: active_only=True

    sql_blob = " ".join(str(c.args[0]) for c in conn.fetch.call_args_list)
    assert "subscription_status = 'active'" in sql_blob


async def test_active_only_false_omits_filter():
    """Mesio internal admin needs to see ALL orgs (including cancelled).
    The SELECT may still PROJECT subscription_status, but no WHERE filter."""
    from app.repositories.restaurant_repo import db_get_all_orgs

    conn = _make_conn([])
    pool = _make_pool(conn)
    with patch("app.repositories.restaurant_repo._get_pool", AsyncMock(return_value=pool)):
        await db_get_all_orgs(active_only=False)

    sql_blob = " ".join(str(c.args[0]) for c in conn.fetch.call_args_list)
    assert "WHERE" not in sql_blob.upper(), (
        f"Expected no WHERE filter when active_only=False, got SQL: {sql_blob}"
    )


# ── Returned dict shape ───────────────────────────────────────────────────────

async def test_returns_org_level_fields_only():
    """Each row must contain org-level fields. No location_id, no branch_id —
    those would imply we picked one specific sede, which we did NOT."""
    from app.repositories.restaurant_repo import db_get_all_orgs

    fake_row = {
        "id": 1, "name": "Test Org", "slug": "test",
        "whatsapp_number": "+57300", "wa_phone_id": "ph",
        "wa_access_token": "tok", "menu": {}, "features": {"timezone": "America/Bogota"},
        "subscription_plan": "free", "subscription_status": "active",
        "created_at": None, "updated_at": None,
    }
    conn = _make_conn([fake_row])
    pool = _make_pool(conn)
    with patch("app.repositories.restaurant_repo._get_pool", AsyncMock(return_value=pool)):
        result = await db_get_all_orgs()

    assert len(result) == 1
    org = result[0]
    # Org-level fields present
    for key in ("id", "name", "whatsapp_number", "features", "subscription_status"):
        assert key in org, f"Missing org-level field: {key}"
    # Location-level fields ABSENT (intentional — caller should not assume a sede)
    assert "location_id" not in org
    assert "branch_id" not in org
    assert "is_primary" not in org


async def test_features_jsonb_string_is_deserialized():
    """asyncpg may return JSONB as string depending on driver; result dict
    must always expose features as a dict, not a string."""
    from app.repositories.restaurant_repo import db_get_all_orgs

    fake_row = {
        "id": 1, "name": "Test", "slug": "t",
        "whatsapp_number": None, "wa_phone_id": None, "wa_access_token": None,
        "menu": "[]",
        "features": '{"locale": "es-CO"}',  # ← string form
        "subscription_plan": "free", "subscription_status": "active",
        "created_at": None, "updated_at": None,
    }
    conn = _make_conn([fake_row])
    pool = _make_pool(conn)
    with patch("app.repositories.restaurant_repo._get_pool", AsyncMock(return_value=pool)):
        result = await db_get_all_orgs()

    assert isinstance(result[0]["features"], dict)
    assert result[0]["features"]["locale"] == "es-CO"
    assert isinstance(result[0]["menu"], list)


# ── Ordering — deterministic for downstream loops ─────────────────────────────

async def test_orders_by_id_for_stable_iteration():
    """Scheduler ticks iterate orgs; deterministic order avoids race
    conditions when the same row is processed by leader rotation."""
    from app.repositories.restaurant_repo import db_get_all_orgs

    conn = _make_conn([])
    pool = _make_pool(conn)
    with patch("app.repositories.restaurant_repo._get_pool", AsyncMock(return_value=pool)):
        await db_get_all_orgs()

    sql_blob = " ".join(str(c.args[0]) for c in conn.fetch.call_args_list).lower()
    assert "order by id asc" in sql_blob
