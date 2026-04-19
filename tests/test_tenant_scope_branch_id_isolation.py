"""
tests/test_tenant_scope_branch_id_isolation.py

Locks down the fix for the Wave-2 X-Branch-ID conflation bug: when an
admin / owner sends an X-Branch-ID header, that integer is a LOCATION_ID
(the sede they picked from the dropdown), NOT an org_id.

Pre-fix several routes were doing:
    restaurant_id = restaurant["id"]                # = org_id
    if branch_header.isdigit():
        restaurant_id = int(branch_header)          # = location_id (BUG)
    with tenant_scope(restaurant_id):               # passes location_id
        ...

`tenant_scope()` sets the `app.org_id` GUC; if we feed it a location_id
the RLS policy filters by the wrong key. For Matriz invariant tenants
(single sede, where org_id == location_id) this masked the bug. For
multi-branch Matriz the bug returned zero or wrong rows.

These tests don't exercise RLS directly — they assert the right INTEGER
reaches `tenant_scope`. We patch `_current_tenant.set` (the ContextVar
that backs `tenant_scope`) and capture its arguments, then drive the
endpoint and confirm the captured value matches the org_id we expect,
NOT the location_id from the header.

Covers:
  POST /api/tables                  (tables.py:144 fix)
  GET  /api/menu/availability       (stats.py:275 fix)
  POST /api/menu/availability       (stats.py:293 fix)
  GET  /api/table-sessions/closed   (settings_routes.py:531 fix)
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ── Constants — the integers MUST be different to expose the bug ─────────────

ORG_ID = 11        # tenant key — what tenant_scope MUST receive
LOCATION_ID = 22   # sede id — what X-Branch-ID header would pass


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


@pytest.fixture
def matriz_org_dict():
    """The Matriz restaurant_obj as returned by db_get_restaurant_by_id —
    `id` field is normalized to org_id, location_id is preserved."""
    return {
        "id": ORG_ID,                     # normalized to org_id
        "org_id": ORG_ID,
        "location_id": LOCATION_ID,
        "name": "Test Restaurant",
        "whatsapp_number": "+57300",
        "parent_restaurant_id": None,
        "features": {},
    }


@pytest.fixture
def admin_user():
    return {"username": "owner", "role": "owner", "branch_id": LOCATION_ID, "restaurant_name": "Test"}


# ── Helper: spy on tenant_scope without triggering real DB ────────────────────

def _patch_tenant_scope_capture(captured: list):
    """Replace tenant_scope with a spy that records the integer it received.

    Returns the patch context manager.
    """
    from contextlib import contextmanager

    @contextmanager
    def _spy(rid):
        captured.append(rid)
        yield

    return [
        patch("app.routes.tables.tenant_scope", _spy),
        patch("app.routes.stats.tenant_scope", _spy),
        patch("app.routes.settings_routes.tenant_scope", _spy),
    ]


# ── Test 1 — POST /api/tables (tables.py:144) ─────────────────────────────────

def test_create_table_passes_org_id_to_tenant_scope_not_location_id(
    client, matriz_org_dict, admin_user
):
    """X-Branch-ID header carries a location_id; tenant_scope must STILL get org_id."""
    captured: list = []

    patches = _patch_tenant_scope_capture(captured) + [
        patch("app.routes.tables.require_auth", AsyncMock(return_value=None)),
        patch("app.routes.tables.get_current_user", AsyncMock(return_value=admin_user)),
        patch("app.routes.tables.get_current_restaurant", AsyncMock(return_value=matriz_org_dict)),
        patch("app.routes.tables.db.db_get_restaurant_by_id",
              AsyncMock(return_value={**matriz_org_dict, "id": ORG_ID, "location_id": LOCATION_ID})),
        patch("app.routes.tables.db.db_auto_create_table",
              AsyncMock(return_value={"id": "t1", "name": "Mesa 1", "number": 1})),
    ]
    for p in patches:
        p.start()
    try:
        resp = client.post("/api/tables", headers={
            "X-Branch-ID": str(LOCATION_ID),
            "Authorization": "Bearer test",
        })
    finally:
        for p in patches:
            p.stop()

    assert resp.status_code == 200, resp.text
    assert captured == [ORG_ID], (
        f"tenant_scope must receive ORG_ID ({ORG_ID}) — got {captured}. "
        "If LOCATION_ID leaks here, RLS will filter by the wrong key."
    )


# ── Test 2 — GET /api/menu/availability (stats.py:275) ────────────────────────

def test_get_menu_availability_passes_org_id_to_tenant_scope(
    client, matriz_org_dict, admin_user
):
    captured: list = []

    patches = _patch_tenant_scope_capture(captured) + [
        patch("app.routes.stats.require_auth", AsyncMock(return_value=None)),
        patch("app.routes.stats.get_current_user", AsyncMock(return_value=admin_user)),
        patch("app.routes.stats.get_current_restaurant", AsyncMock(return_value=matriz_org_dict)),
        patch("app.routes.stats.db.db_get_menu_availability", AsyncMock(return_value={})),
    ]
    for p in patches:
        p.start()
    try:
        # Even with the X-Branch-ID header trying to override, must scope by org_id
        resp = client.get("/api/menu/availability", headers={
            "X-Branch-ID": str(LOCATION_ID),
            "Authorization": "Bearer test",
        })
    finally:
        for p in patches:
            p.stop()

    assert resp.status_code == 200, resp.text
    assert captured == [ORG_ID], (
        f"GET /api/menu/availability must scope by ORG_ID ({ORG_ID}) — got {captured}. "
        "menu_availability is keyed by (dish_name, org_id), not per-branch."
    )


# ── Test 3 — POST /api/menu/availability (stats.py:293) ───────────────────────

def test_set_dish_availability_passes_org_id_to_tenant_scope_AND_repo(
    client, matriz_org_dict, admin_user
):
    captured: list = []
    set_dish_mock = AsyncMock(return_value=None)

    patches = _patch_tenant_scope_capture(captured) + [
        patch("app.routes.stats.require_auth", AsyncMock(return_value=None)),
        patch("app.routes.stats.get_current_user", AsyncMock(return_value=admin_user)),
        patch("app.routes.stats.get_current_restaurant", AsyncMock(return_value=matriz_org_dict)),
        patch("app.routes.stats.db.db_set_dish_availability", set_dish_mock),
    ]
    for p in patches:
        p.start()
    try:
        resp = client.post(
            "/api/menu/availability",
            headers={"X-Branch-ID": str(LOCATION_ID), "Authorization": "Bearer test"},
            json={"dish_name": "Pizza", "available": False},
        )
    finally:
        for p in patches:
            p.stop()

    assert resp.status_code == 200, resp.text
    assert captured == [ORG_ID], f"tenant_scope got {captured} not [{ORG_ID}]"

    # Also: db_set_dish_availability must receive org_id (not location_id) as
    # restaurant_id, otherwise the INSERT writes the row keyed to the wrong tenant
    set_dish_mock.assert_awaited_once()
    kwargs = set_dish_mock.await_args.kwargs
    assert kwargs["restaurant_id"] == ORG_ID, (
        f"db_set_dish_availability must receive ORG_ID, got {kwargs['restaurant_id']}"
    )


# ── Test 4 — GET /api/table-sessions/closed (settings_routes.py:531) ──────────

def test_closed_sessions_resolves_org_id_from_branch_id_for_tenant_scope(
    client, matriz_org_dict, admin_user
):
    """get_dashboard_filters returns a location_id; the route must resolve org_id
    via db_get_restaurant_by_id BEFORE passing to tenant_scope."""
    captured: list = []

    patches = _patch_tenant_scope_capture(captured) + [
        patch("app.routes.settings_routes.require_auth", AsyncMock(return_value=None)),
        # get_dashboard_filters returns (branch_id=LOCATION_ID, bot_number, ...)
        patch("app.routes.settings_routes.get_dashboard_filters",
              AsyncMock(return_value=(LOCATION_ID, "+57300", None, None))),
        # db_get_restaurant_by_id returns a dict with `id` normalized to ORG_ID
        patch("app.routes.settings_routes.db.db_get_restaurant_by_id",
              AsyncMock(return_value={"id": ORG_ID, "org_id": ORG_ID, "location_id": LOCATION_ID})),
        patch("app.routes.settings_routes.tr.db_get_closed_sessions",
              AsyncMock(return_value=[])),
    ]
    for p in patches:
        p.start()
    try:
        resp = client.get("/api/table-sessions/closed", headers={
            "X-Branch-ID": str(LOCATION_ID),
            "Authorization": "Bearer test",
        })
    finally:
        for p in patches:
            p.stop()

    assert resp.status_code == 200, resp.text
    assert captured == [ORG_ID], (
        f"tenant_scope received {captured} — expected [{ORG_ID}]. The fix resolves "
        "org_id from db_get_restaurant_by_id(location_id) before scoping."
    )


# ── Test 5 — GET /api/pos/tables-status (Paso 6: inverse audit) ───────────────

def test_pos_tables_status_passes_location_id_to_branch_query_and_org_id_to_scope(
    client, matriz_org_dict, admin_user
):
    """The POS map endpoint must:
      - tenant_scope(org_id) — for RLS
      - db_get_tables(branch_id=location_id) — to filter by sede
      - db_get_pending_orders_by_branch(location_id) — same

    Pre-fix, both DB calls received org_id. For multi-branch tenants where
    org_id != location_id_of_branches the queries returned wrong rows or
    none at all (staff at sub-sucursal saw matriz tables, not their own).
    """
    captured_scope: list = []
    db_get_tables_mock = AsyncMock(return_value=[])
    db_pending_mock = AsyncMock(return_value=[])

    patches = _patch_tenant_scope_capture(captured_scope) + [
        patch("app.routes.tables.require_auth", AsyncMock(return_value=None)),
        patch("app.routes.tables.get_current_restaurant", AsyncMock(return_value=matriz_org_dict)),
        patch("app.routes.tables.db.db_get_tables", db_get_tables_mock),
        patch("app.routes.tables.tr.db_get_pending_orders_by_branch", db_pending_mock),
        patch("app.routes.tables.tr.db_get_active_session_table_ids", AsyncMock(return_value=set())),
    ]
    for p in patches:
        p.start()
    try:
        resp = client.get("/api/pos/tables-status",
                          headers={"Authorization": "Bearer test"})
    finally:
        for p in patches:
            p.stop()

    assert resp.status_code == 200, resp.text
    assert captured_scope == [ORG_ID], (
        f"tenant_scope must receive ORG_ID ({ORG_ID}), got {captured_scope}"
    )

    # The branch-filter calls MUST receive location_id (not org_id)
    db_get_tables_mock.assert_awaited_once()
    assert db_get_tables_mock.await_args.kwargs.get("branch_id") == LOCATION_ID, (
        f"db_get_tables expected branch_id={LOCATION_ID} (location_id), "
        f"got {db_get_tables_mock.await_args.kwargs.get('branch_id')} — "
        "regression of the Paso 6 inverse-conflation fix"
    )

    db_pending_mock.assert_awaited_once_with(LOCATION_ID)


def test_pos_tables_status_falls_back_to_org_id_when_location_id_missing(
    client, admin_user
):
    """If the restaurant dict somehow lacks location_id (legacy callers,
    pre-Wave-2 cached sessions), fall back to org_id so we don't crash with
    None being passed into the SQL filter."""
    captured_scope: list = []
    db_get_tables_mock = AsyncMock(return_value=[])
    matriz_no_loc = {
        "id": ORG_ID,
        "org_id": ORG_ID,
        # location_id intentionally absent
        "name": "Test", "whatsapp_number": "+57300",
    }

    patches = _patch_tenant_scope_capture(captured_scope) + [
        patch("app.routes.tables.require_auth", AsyncMock(return_value=None)),
        patch("app.routes.tables.get_current_restaurant", AsyncMock(return_value=matriz_no_loc)),
        patch("app.routes.tables.db.db_get_tables", db_get_tables_mock),
        patch("app.routes.tables.tr.db_get_pending_orders_by_branch", AsyncMock(return_value=[])),
        patch("app.routes.tables.tr.db_get_active_session_table_ids", AsyncMock(return_value=set())),
    ]
    for p in patches:
        p.start()
    try:
        resp = client.get("/api/pos/tables-status",
                          headers={"Authorization": "Bearer test"})
    finally:
        for p in patches:
            p.stop()

    assert resp.status_code == 200, resp.text
    # Fallback: branch_id == org_id (correct under Matriz invariant)
    assert db_get_tables_mock.await_args.kwargs.get("branch_id") == ORG_ID
