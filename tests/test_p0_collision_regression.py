"""
tests/test_p0_collision_regression.py

Regression suite for the P0 cross-tenant ambiguous-restaurant-lookup bug
(see memory/ambiguous-restaurant-lookup-p0.md and the users_org_location
migration). Every existing test before this fix only passed because the
seeded org_id and location_id happened not to collide — these tests seed
the collision ON PURPOSE: a location L belonging to org A, plus a
DIFFERENT org B whose own id equals L.

Covers the fix ticket's required regression list:
  (a) db_get_restaurant_by_location_id(L) returns the OWNING org (A), never
      the colliding org (B).
  (b) The bot's table flow (agent._load_restaurant_context) resolves the
      CORRECT org from a table_context branch_id that collides with
      another org — the diner gets THEIR OWN restaurant's name/menu, not
      the colliding tenant's.
  (c) Admin auth (deps.get_current_restaurant) resolves a location-assigned
      user to THEIR OWN org even when their location's raw id collides
      with another org's id, and never trusts a location_id that turns out
      to belong to a different org than the user's own org_id.
  (d) A user whose org_id could not be resolved is DENIED (403), never
      guessed via branch_id or name-match fallback.

(e) — the migration backfill on mixed data including a collision — was
validated by directly running alembic upgrade against an isolated
database (mesio_p0) seeded with all four id-kind scenarios (clean org-id,
clean location-id, collision resolved by name, collision resolved via
parent_user, and two unresolvable cases) and inspecting the per-user
result; see the fix report. It is not re-implemented as a pytest unit
here because the backfill logic runs through `op.execute()` bound to an
Alembic migration context, not a plain importable function — the
migration file itself is the artifact under test.
"""
from __future__ import annotations

import os

import asyncpg
import pytest
from fastapi import HTTPException

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DATABASE_URL not set — integration tests skipped",
)


def _reset_pool() -> None:
    from app.services import database as _db_mod
    _db_mod._pool = None


def _run(coro):
    """Run a coroutine on a fresh, throwaway event loop (mirrors the
    pattern in tests/test_waiter_alerts_location.py)."""
    import asyncio
    _reset_pool()
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        _reset_pool()


async def _seed_collision(conn) -> dict:
    """Seed org A (whatsapp-registered) with location L, plus a DIFFERENT
    org B whose own id is explicitly set to equal L. This is exactly the
    P0 shape: `db_get_restaurant_by_id(L)` used to return org B."""
    bot_number = f"57{os.urandom(4).hex()}"

    org_a = await conn.fetchval(
        "INSERT INTO organizations (name, whatsapp_number) VALUES ($1, $2) RETURNING id",
        "Restaurante A (collision regression)", bot_number,
    )
    org_b = await conn.fetchval(
        "INSERT INTO organizations (name, whatsapp_number) VALUES ($1, $2) RETURNING id",
        "Restaurante B (OTRO cliente)", f"57{os.urandom(4).hex()}",
    )

    # Explicit id assignment: location L's id is forced to equal org_b's id.
    # L genuinely belongs to org_a — org_b matching it is the collision.
    loc_l = await conn.fetchval(
        "INSERT INTO locations (id, org_id, name, code, active) "
        "VALUES ($1, $2, $3, 'principal', true) RETURNING id",
        org_b, org_a, "Sede A Principal",
    )
    assert loc_l == org_b, "setup invariant: location id must equal the colliding org id"

    # A forced explicit id does NOT advance the BIGSERIAL sequence. Without
    # this, the sequence eventually reaches loc_l's id and the very next
    # ordinary INSERT — here or in any later test sharing the database —
    # dies on "duplicate key value violates unique constraint locations_pkey".
    # That is how a deliberate-collision test passes alone and fails in a
    # full-suite run.
    await conn.execute(
        "SELECT setval("
        "  pg_get_serial_sequence('locations', 'id'),"
        "  GREATEST($1::bigint, (SELECT COALESCE(last_value, 1) FROM locations_id_seq))"
        ")",
        loc_l,
    )

    loc_b = await conn.fetchval(
        "INSERT INTO locations (org_id, name, code, active) VALUES ($1, $2, 'principal', true) RETURNING id",
        org_b, "Sede B Principal",
    )

    return {
        "org_a": org_a, "loc_l": loc_l, "org_b": org_b, "loc_b": loc_b,
        "bot_number": bot_number,
    }


async def _teardown_collision(seed: dict) -> None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute("DELETE FROM users WHERE org_id = ANY($1::bigint[])", [seed["org_a"], seed["org_b"]])
        await conn.execute("DELETE FROM locations WHERE org_id = ANY($1::bigint[])", [seed["org_a"], seed["org_b"]])
        await conn.execute("DELETE FROM organizations WHERE id = ANY($1::bigint[])", [seed["org_a"], seed["org_b"]])
    finally:
        await conn.close()


@pytest.fixture
def collision():
    async def _setup():
        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            return await _seed_collision(conn)
        finally:
            await conn.close()

    seed = _run(_setup())
    yield seed
    _run(_teardown_collision(seed))


# ── (a) db_get_restaurant_by_location_id never returns the colliding org ────

def test_location_id_lookup_returns_owning_org_not_collision(collision):
    from app.repositories.restaurant_repo import db_get_restaurant_by_location_id

    async def _call():
        return await db_get_restaurant_by_location_id(collision["loc_l"])

    result = _run(_call())
    assert result is not None
    assert result["org_id"] == collision["org_a"], (
        f"db_get_restaurant_by_location_id({collision['loc_l']}) must resolve to org A "
        f"({collision['org_a']}) — the location's real owner — but got org "
        f"{result['org_id']} (org B's id happens to equal the location id)."
    )
    assert result["name"] == "Restaurante A (collision regression)"
    # And the org-id lookup for the SAME integer must resolve to the OTHER
    # (colliding) org — proving the two functions never share ambiguity.
    from app.repositories.restaurant_repo import db_get_restaurant_by_org_id

    async def _call_org():
        return await db_get_restaurant_by_org_id(collision["loc_l"])  # == org_b's id

    org_result = _run(_call_org())
    assert org_result["org_id"] == collision["org_b"]
    assert org_result["name"] == "Restaurante B (OTRO cliente)"


# ── (b) Bot table flow resolves the diner's OWN restaurant ──────────────────

def test_bot_table_flow_resolves_own_restaurant_despite_collision(collision):
    from app.services.agent import _load_restaurant_context

    table_context = {"branch_id": collision["loc_l"], "id": "table-1"}

    async def _call():
        return await _load_restaurant_context(
            bot_number=collision["bot_number"],
            table_context=table_context,
            user_phone="+573000000001",
            meta_phone_id=None,
        )

    ctx = _run(_call())
    assert ctx is not None, "restaurant context resolution must not fail"
    restaurant_obj = ctx["restaurant_obj"]
    assert restaurant_obj["org_id"] == collision["org_a"], (
        "A diner sitting at org A's table must be served org A's own "
        f"restaurant_obj, not org B's (got org_id={restaurant_obj['org_id']})."
    )
    assert ctx["restaurant_name"] == "Restaurante A (collision regression)"
    # id is normalized to org_id (the tenant key used for downstream writes
    # like db_increment_token_usage / upsert_profile_from_message).
    assert restaurant_obj["id"] == collision["org_a"]


# ── (c) Admin auth resolves to the user's OWN org, never the collision ──────

def test_get_current_restaurant_resolves_own_org_despite_collision(collision, monkeypatch):
    from app.routes import deps

    class _FakeRequest:
        headers: dict = {}

    user_a = {
        "username": "gerente_a",
        "org_id": collision["org_a"],
        "location_id": collision["loc_l"],
        "role": "gerente",
    }

    async def _fake_get_current_user(_request):
        return user_a

    monkeypatch.setattr(deps, "get_current_user", _fake_get_current_user)
    restaurant = _run(deps.get_current_restaurant(_FakeRequest()))
    assert restaurant["org_id"] == collision["org_a"], (
        "A location-assigned user whose location id collides with another "
        f"org's id must resolve to THEIR OWN org (got {restaurant['org_id']})."
    )

    # Tamper case: location_id claims to be loc_l, but the real ownership
    # check must catch that loc_l does NOT belong to org_b if org_id=org_b
    # is asserted (defense in depth — never trust location_id blindly).
    user_b_tampered = {
        "username": "gerente_b_tampered",
        "org_id": collision["org_b"],
        "location_id": collision["loc_l"],  # actually belongs to org_a!
        "role": "gerente",
    }

    async def _fake_get_current_user_tampered(_request):
        return user_b_tampered

    monkeypatch.setattr(deps, "get_current_user", _fake_get_current_user_tampered)
    mismatched_result = _run(deps.get_current_restaurant(_FakeRequest()))
    # Must fall back to org_b's OWN default location — never return org_a's
    # data just because location_id said so.
    assert mismatched_result["org_id"] == collision["org_b"], (
        "A location_id that does not actually belong to the asserted org_id "
        "must be ignored — get_current_restaurant must never return the "
        "OTHER org's data."
    )


# ── (d) Unresolved org_id is denied, never guessed ───────────────────────────

def test_get_current_restaurant_denies_unresolved_org_id(monkeypatch):
    from app.routes import deps

    class _FakeRequest:
        headers: dict = {}

    # org_id intentionally absent — simulates a genuinely unresolvable
    # backfill (the migration logs these and leaves org_id NULL).
    user = {"username": "orphan_user", "branch_id": 999999, "role": "owner"}

    async def _fake_get_current_user(_request):
        return user

    monkeypatch.setattr(deps, "get_current_user", _fake_get_current_user)

    with pytest.raises(HTTPException) as exc_info:
        _run(deps.get_current_restaurant(_FakeRequest()))
    assert exc_info.value.status_code == 403
