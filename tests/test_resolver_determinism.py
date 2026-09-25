"""
tests/test_resolver_determinism.py

Locks down the SQL shape of the restaurant resolver functions after the
Wave-2 determinism fixes (Paso 8) and the P0 ambiguous-lookup fix (2026-09):

  db_get_restaurant_by_phone     — must have ORDER BY + LIMIT 1 and prefer
  the location with an explicit whatsapp_number match.

  db_get_restaurant_by_location_id / db_get_restaurant_by_org_id — replace
  the deleted db_get_restaurant_by_id. Each filters on EXACTLY ONE id kind
  (l.id for the former, l.org_id for the latter) — neither may accept both,
  which is exactly the ambiguity that caused the P0 cross-tenant bug (a
  location id colliding with an unrelated org's id could resolve to that
  org). See docs: memory/ambiguous-restaurant-lookup-p0.md.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_conn(row=None):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=row)
    return conn


def _make_pool(conn):
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool


# ── db_get_restaurant_by_phone ────────────────────────────────────────────────

async def test_by_phone_has_order_by_and_limit():
    """SQL must include ORDER BY ... LIMIT 1 so Postgres returns a single,
    deterministic row even when multiple locations share a whatsapp_number."""
    from app.repositories.restaurant_repo import db_get_restaurant_by_phone

    conn = _make_conn(row=None)
    pool = _make_pool(conn)
    with patch("app.repositories.restaurant_repo._get_pool", AsyncMock(return_value=pool)):
        await db_get_restaurant_by_phone("+573001234567")

    sql = str(conn.fetchrow.call_args.args[0]).lower()
    assert "order by" in sql, "Must have ORDER BY for deterministic resolution"
    assert "limit 1" in sql, "Must have LIMIT 1 to return exactly one row"


async def test_by_phone_prefers_explicit_location_override():
    """ORDER BY clause must rank the location with an explicit
    l.whatsapp_number match first (DESC NULLS LAST), then break ties by
    l.id ASC for full determinism."""
    from app.repositories.restaurant_repo import db_get_restaurant_by_phone

    conn = _make_conn(row=None)
    pool = _make_pool(conn)
    with patch("app.repositories.restaurant_repo._get_pool", AsyncMock(return_value=pool)):
        await db_get_restaurant_by_phone("+573001234567")

    sql = str(conn.fetchrow.call_args.args[0])
    assert "(l.whatsapp_number = $1) DESC NULLS LAST" in sql, (
        "Must prefer location with explicit whatsapp_number match"
    )
    assert "l.id ASC" in sql, "Must break remaining ties by l.id ASC"


# ── db_get_restaurant_by_location_id / db_get_restaurant_by_org_id ───────────

async def test_by_location_id_no_is_primary():
    """l.is_primary must NOT appear anywhere — it is vestigial per Paso 8."""
    from app.repositories.restaurant_repo import db_get_restaurant_by_location_id

    conn = _make_conn(row=None)
    pool = _make_pool(conn)
    with patch("app.repositories.restaurant_repo._get_pool", AsyncMock(return_value=pool)):
        await db_get_restaurant_by_location_id(42)

    sql = str(conn.fetchrow.call_args.args[0]).lower()
    assert "is_primary" not in sql, (
        "Must NOT use is_primary for tie-breaking (vestigial column)"
    )


async def test_by_location_id_filters_only_on_location():
    """P0: db_get_restaurant_by_location_id must filter ONLY on l.id — never
    on org_id — so it can never resolve to an unrelated org when the given
    id happens to collide with that org's own id."""
    from app.repositories.restaurant_repo import db_get_restaurant_by_location_id

    conn = _make_conn(row=None)
    pool = _make_pool(conn)
    with patch("app.repositories.restaurant_repo._get_pool", AsyncMock(return_value=pool)):
        await db_get_restaurant_by_location_id(42)

    sql = str(conn.fetchrow.call_args.args[0])
    assert "WHERE l.id = $1" in sql, "Must filter ONLY on l.id — no org_id branch"
    assert "org_id = $1" not in sql, (
        "Must NOT also match on org_id — that reintroduces the P0 ambiguity"
    )


async def test_by_org_id_filters_only_on_org():
    """P0: db_get_restaurant_by_org_id must filter ONLY on l.org_id — never
    on l.id — and pick a deterministic default location (l.id ASC, never
    is_primary)."""
    from app.repositories.restaurant_repo import db_get_restaurant_by_org_id

    conn = _make_conn(row=None)
    pool = _make_pool(conn)
    with patch("app.repositories.restaurant_repo._get_pool", AsyncMock(return_value=pool)):
        await db_get_restaurant_by_org_id(42)

    sql = str(conn.fetchrow.call_args.args[0])
    assert "WHERE l.org_id = $1" in sql, "Must filter ONLY on l.org_id"
    assert "r.id = $1" not in sql, (
        "Must NOT also match on the location's own id — that reintroduces the P0 ambiguity"
    )
    assert "l.id ASC" in sql, "Must break ties deterministically by l.id ASC"
    assert "is_primary" not in sql.lower(), "Must NOT use the vestigial is_primary column"


# ── agent.detect_table_context — same fix template applied inline ────────────

