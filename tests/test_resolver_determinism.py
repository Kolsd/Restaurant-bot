"""
tests/test_resolver_determinism.py

Locks down the SQL shape of the two restaurant resolver functions after the
Wave-2 determinism fixes (Paso 8):

  db_get_restaurant_by_phone — must have ORDER BY + LIMIT 1 and prefer the
  location with an explicit whatsapp_number match.

  db_get_restaurant_by_id   — must use l.id ASC for tie-breaking (NOT the
  vestigial l.is_primary) while keeping the org_id preference.
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


# ── db_get_restaurant_by_id ───────────────────────────────────────────────────

async def test_by_id_no_is_primary():
    """l.is_primary must NOT appear in the ORDER BY — it is vestigial per
    Paso 8 and makes ordering non-deterministic across schema states."""
    from app.repositories.restaurant_repo import db_get_restaurant_by_id

    conn = _make_conn(row=None)
    pool = _make_pool(conn)
    with patch("app.repositories.restaurant_repo._get_pool", AsyncMock(return_value=pool)):
        await db_get_restaurant_by_id(42)

    sql = str(conn.fetchrow.call_args.args[0]).lower()
    assert "is_primary" not in sql, (
        "Must NOT use is_primary for tie-breaking (vestigial column)"
    )
    assert "l.id asc" in sql, "Must use l.id ASC as deterministic tie-breaker"


async def test_by_id_keeps_org_id_preference():
    """The org_id preference (put org-level matches first) must be preserved —
    it is the legitimate two-tier lookup logic, not vestigial."""
    from app.repositories.restaurant_repo import db_get_restaurant_by_id

    conn = _make_conn(row=None)
    pool = _make_pool(conn)
    with patch("app.repositories.restaurant_repo._get_pool", AsyncMock(return_value=pool)):
        await db_get_restaurant_by_id(42)

    sql = str(conn.fetchrow.call_args.args[0])
    assert "(l.org_id = $1) DESC" in sql, (
        "Must keep org_id preference for two-tier lookup (location_id OR org_id)"
    )


# ── agent.detect_table_context — same fix template applied inline ────────────

def test_detect_table_context_query_has_order_by_and_limit():
    """Source-level check: the inline `WHERE r.whatsapp_number=$1` query in
    agent.detect_table_context must mirror the deterministic pattern of
    db_get_restaurant_by_phone. Driving the function fully under tenant_scope
    is impractical for a focused regression on the SQL shape — read the
    source and assert the bot_rest fetchrow block contains ORDER BY + LIMIT 1.

    If anyone reintroduces a bare `WHERE r.whatsapp_number = $1` without
    deterministic ordering, this test fails immediately.
    """
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "app" / "services" / "agent.py"
    text = src.read_text(encoding="utf-8")

    idx = text.find("bot_rest = await conn.fetchrow")
    assert idx >= 0, "Could not locate bot_rest fetchrow in agent.py"
    block = text[idx : idx + 800]

    assert "ORDER BY" in block, (
        "detect_table_context.bot_rest query must include ORDER BY for "
        "deterministic resolution"
    )
    assert "LIMIT 1" in block, (
        "detect_table_context.bot_rest query must include LIMIT 1"
    )
