"""
Tests para FASE 5: Split Checks y Pagos Mixtos.
Cubre: db_create_checks, db_finalize_check_payment,
       db_get_order_ticket_data, endpoints REST de checks.
No requiere base de datos ni credenciales reales.

Updated for tenant_connection() migration: direct repo calls now require
an active tenant_scope() or bypass_tenant_scope().  Tests that call repo
functions directly wrap them in bypass_tenant_scope (since the test has
no meaningful restaurant_id context).
"""
import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.tenant_context import bypass_tenant_scope as _bypass


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_row(d: dict):
    row = MagicMock()
    row.__iter__ = lambda s: iter(d.items())
    row.keys     = lambda: d.keys()
    row.__getitem__ = lambda s, k: d[k]
    row.get = lambda k, default=None: d.get(k, default)
    return row


def _make_pool(conn):
    mock_pool = AsyncMock()
    mock_pool.acquire = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=conn),
        __aexit__=AsyncMock(return_value=False),
    ))
    return mock_pool


# ══════════════════════════════════════════════════════════════════════════════
# 1. db_create_checks
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_create_checks_inserta_dos_checks():
    """Crear 2 checks debe hacer DELETE de abiertos + INSERT por cada check."""
    from app.services import database as db

    check_rows = [
        _make_row({"id": "BASE-001-CHK-1", "base_order_id": "BASE-001",
                   "check_number": 1, "items": "[]", "subtotal": 90000,
                   "tax_amount": 14369, "total": 90000, "payments": "[]",
                   "change_amount": 0, "status": "open",
                   "fiscal_invoice_id": None, "customer_name": None,
                   "customer_nit": None, "customer_email": None,
                   "created_at": None, "paid_at": None}),
        _make_row({"id": "BASE-001-CHK-2", "base_order_id": "BASE-001",
                   "check_number": 2, "items": "[]", "subtotal": 15000,
                   "tax_amount": 2395, "total": 15000, "payments": "[]",
                   "change_amount": 0, "status": "open",
                   "fiscal_invoice_id": None, "customer_name": None,
                   "customer_nit": None, "customer_email": None,
                   "created_at": None, "paid_at": None}),
    ]

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.fetch   = AsyncMock(return_value=check_rows)
    mock_conn.transaction = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None),
        __aexit__=AsyncMock(return_value=False),
    ))

    checks_input = [
        {"check_number": 1, "items": [{"name": "Pizza", "qty": 2, "unit_price": 45000, "subtotal": 90000}],
         "subtotal": 75630.25, "tax_amount": 14369.75, "total": 90000},
        {"check_number": 2, "items": [{"name": "Gaseosa", "qty": 3, "unit_price": 5000, "subtotal": 15000}],
         "subtotal": 12605.04, "tax_amount": 2394.96, "total": 15000},
    ]

    with patch.object(db, "get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
        with _bypass("test: db_create_checks direct call"):
            result = await db.db_create_checks("BASE-001", checks_input)

    # DELETE de checks open fue llamado
    delete_calls = [c for c in mock_conn.execute.call_args_list if "DELETE" in str(c)]
    assert len(delete_calls) >= 1

    # INSERT llamado 2 veces (una por check)
    insert_calls = [c for c in mock_conn.execute.call_args_list if "INSERT" in str(c)]
    assert len(insert_calls) == 2

    assert len(result) == 2
    assert result[0]["check_number"] == 1
    assert result[1]["check_number"] == 2


@pytest.mark.asyncio
async def test_create_checks_id_incluye_chk_numero():
    """El ID del check debe seguir el patrón {base_order_id}-CHK-{n}."""
    from app.services import database as db

    inserted_ids = []

    async def capture_execute(sql, *args):
        if "INSERT" in sql:
            inserted_ids.append(args[0])  # primer arg es el check_id
        return "INSERT 0 1"

    result_row = _make_row({
        "id": "ORD-XYZ-CHK-1", "base_order_id": "ORD-XYZ",
        "check_number": 1, "items": "[]", "subtotal": 28000,
        "tax_amount": 4470, "total": 28000, "payments": "[]",
        "change_amount": 0, "status": "open",
        "fiscal_invoice_id": None, "customer_name": None,
        "customer_nit": None, "customer_email": None,
        "created_at": None, "paid_at": None,
    })

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock(side_effect=capture_execute)
    mock_conn.fetch   = AsyncMock(return_value=[result_row])
    mock_conn.transaction = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch.object(db, "get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
        with _bypass("test: db_create_checks id format check"):
            await db.db_create_checks("ORD-XYZ", [
                {"check_number": 1, "items": [], "subtotal": 23529, "tax_amount": 4471, "total": 28000}
            ])

    assert any("ORD-XYZ-CHK-1" in str(i) for i in inserted_ids)


# ══════════════════════════════════════════════════════════════════════════════
# 2. db_finalize_check_payment
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_finalize_check_marca_invoiced():
    """db_finalize_check_payment debe UPDATE el check a status='invoiced'.

    Post race-fix: the UPDATE now uses RETURNING id (so fetchrow, not execute)
    and requires status='paying' as a precondition. The mock returns a row to
    simulate a successful state transition.
    """
    from app.services import database as db

    mock_conn = AsyncMock()
    mock_conn.execute  = AsyncMock()
    # Main UPDATE uses fetchrow now (RETURNING id). Non-None means success.
    mock_conn.fetchrow = AsyncMock(return_value=_make_row({"id": "BASE-001-CHK-1"}))
    mock_conn.fetchval = AsyncMock(return_value=0)  # 0 checks pendientes → cierra mesa
    mock_conn.transaction = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch.object(db, "get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
        with _bypass("test: db_finalize_check_payment invoiced"):
            result = await db.db_finalize_check_payment(
                check_id="BASE-001-CHK-1",
                base_order_id="BASE-001",
                payments=[{"method": "efectivo", "amount": 100000}],
                change_amount=10000,
                fiscal_invoice_id=42,
                customer_name="Juan",
                customer_nit="123456",
                customer_email="j@test.co",
            )

    assert result is True
    # Main UPDATE moved from execute to fetchrow. Verify it was called with
    # the expected SQL fragments.
    update_check_calls = [c for c in mock_conn.fetchrow.call_args_list
                          if "UPDATE table_checks" in str(c) and "SET payments=" in str(c)]
    assert len(update_check_calls) == 1
    # And it requires the 'paying' precondition for race safety.
    sql_text = str(update_check_calls[0])
    assert "status='paying'" in sql_text
    assert "RETURNING id" in sql_text
    # Args include fiscal_invoice_id and change_amount.
    args = update_check_calls[0].args
    assert 42 in args
    assert 10000.0 in args


@pytest.mark.asyncio
async def test_finalize_check_returns_false_when_not_paying():
    """Race protection: if check is not in 'paying' state, finalize returns False
    without touching the rest of the transaction (no proposal update, no mesa close)."""
    from app.services import database as db

    mock_conn = AsyncMock()
    mock_conn.execute  = AsyncMock()
    # fetchrow returns None — UPDATE matched 0 rows because status != 'paying'.
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetchval = AsyncMock(return_value=0)
    mock_conn.transaction = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch.object(db, "get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
        with _bypass("test: db_finalize_check_payment race"):
            result = await db.db_finalize_check_payment(
                check_id="BASE-001-CHK-1",
                base_order_id="BASE-001",
                payments=[{"method": "efectivo", "amount": 100000}],
                change_amount=0,
                fiscal_invoice_id=None,
            )

    assert result is False
    # Critical: no follow-up UPDATEs should have run (no proposal_status, no mesa close)
    follow_up = [c for c in mock_conn.execute.call_args_list
                 if "UPDATE" in str(c) and "table_checks" in str(c)]
    assert follow_up == []
    table_orders_updates = [c for c in mock_conn.execute.call_args_list
                            if "UPDATE table_orders" in str(c)]
    assert table_orders_updates == []


@pytest.mark.asyncio
async def test_finalize_check_cierra_mesa_si_todos_pagados():
    """Si todos los checks están invoiced, debe cerrar la mesa (UPDATE table_orders)."""
    from app.services import database as db

    mock_conn = AsyncMock()
    mock_conn.execute  = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=_make_row({"id": "BASE-001-CHK-2"}))
    mock_conn.fetchval = AsyncMock(return_value=0)  # ningún check pendiente
    mock_conn.transaction = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch.object(db, "get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
        with _bypass("test: db_finalize_check_payment closes mesa"):
            await db.db_finalize_check_payment(
                check_id="BASE-001-CHK-2",
                base_order_id="BASE-001",
                payments=[{"method": "tarjeta", "amount": 15000}],
                change_amount=0,
                fiscal_invoice_id=43,
            )

    # UPDATE table_orders debe haber sido llamado (cierre de mesa)
    close_calls = [c for c in mock_conn.execute.call_args_list
                   if "UPDATE table_orders" in str(c)]
    assert len(close_calls) == 1
    assert "factura_entregada" in str(close_calls[0])


@pytest.mark.asyncio
async def test_finalize_check_no_cierra_mesa_si_hay_pendientes():
    """Si aún hay checks open, NO debe cerrar la mesa."""
    from app.services import database as db

    mock_conn = AsyncMock()
    mock_conn.execute  = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=1)  # 1 check todavía pendiente
    mock_conn.transaction = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch.object(db, "get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
        with _bypass("test: db_finalize_check_payment pending checks"):
            await db.db_finalize_check_payment(
                check_id="BASE-001-CHK-1",
                base_order_id="BASE-001",
                payments=[{"method": "efectivo", "amount": 90000}],
                change_amount=0,
                fiscal_invoice_id=41,
            )

    close_calls = [c for c in mock_conn.execute.call_args_list
                   if "UPDATE table_orders" in str(c)]
    assert len(close_calls) == 0


# ══════════════════════════════════════════════════════════════════════════════
# 3. db_get_order_ticket_data
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_order_ticket_data_agrega_items():
    """db_get_order_ticket_data debe agregar ítems de todas las sub-órdenes."""
    from app.services import database as db

    items1 = json.dumps([{"name": "Pizza", "price": 45000, "quantity": 2}])
    items2 = json.dumps([{"name": "Gaseosa", "price": 5000, "quantity": 3}])

    rows = [
        _make_row({"id": "BASE-001", "base_order_id": None,
                   "table_name": "Mesa 5", "items": items1, "total": 90000}),
        _make_row({"id": "BASE-001-2", "base_order_id": "BASE-001",
                   "table_name": "Mesa 5", "items": items2, "total": 15000}),
    ]

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=rows)
    mock_conn.transaction = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch.object(db, "get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
        with _bypass("test: db_get_order_ticket_data aggregate"):
            result = await db.db_get_order_ticket_data("BASE-001", branch_id=None)

    assert result is not None
    assert result["total"] == 105000
    assert len(result["items"]) == 2
    names = {i["name"] for i in result["items"]}
    assert "Pizza" in names and "Gaseosa" in names


@pytest.mark.asyncio
async def test_get_order_ticket_data_retorna_none_si_no_existe():
    from app.services import database as db

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.transaction = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch.object(db, "get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
        with _bypass("test: db_get_order_ticket_data none case"):
            result = await db.db_get_order_ticket_data("INEXISTENTE", branch_id=None)

    assert result is None


# ══════════════════════════════════════════════════════════════════════════════
# 4. Endpoints HTTP
# ══════════════════════════════════════════════════════════════════════════════

def _mock_auth(monkeypatch):
    from app.services import database as db_mod
    async def mock_verify_token(token: str): return "caja_user"
    async def mock_get_user(username: str):
        return {"username": "caja_user", "branch_id": 1, "role": "caja", "restaurant_name": "R"}
    async def mock_get_restaurant(request):
        return {"id": 1, "whatsapp_number": "+57300", "name": "R"}
    monkeypatch.setattr("app.routes.deps.verify_token", mock_verify_token)
    monkeypatch.setattr(db_mod, "db_get_user", mock_get_user)
    monkeypatch.setattr("app.routes.tables.get_current_restaurant", mock_get_restaurant)
    # Needed for create_checks org_id ownership check (Wave-2)
    monkeypatch.setattr(db_mod, "db_get_restaurant_by_id",
                        AsyncMock(return_value={"id": 1, "org_id": 1, "location_id": 1}))
    return db_mod


def test_create_checks_endpoint_valida_cantidades(client, monkeypatch):
    """POST /checks debe retornar 400 si el check excede la qty del ticket."""
    from app.services import database as db_mod
    db_mod = _mock_auth(monkeypatch)

    async def mock_ticket_data(base_order_id, branch_id=None):
        return {
            "base_order_id": "BASE-001",
            "table_name": "Mesa 5",
            "items": [{"name": "Pizza", "price": 45000, "quantity": 2}],
            "total": 90000,
            "org_id": 1,
        }
    monkeypatch.setattr(db_mod, "db_get_order_ticket_data", mock_ticket_data)

    resp = client.post(
        "/api/table-orders/BASE-001/checks",
        json={
            "checks": [
                {"check_number": 1, "items": [{"name": "Pizza", "qty": 5, "unit_price": 45000}]}
            ],
            "tax_pct": 19.0
        },
        headers={"Authorization": "Bearer fake"}
    )
    assert resp.status_code == 400
    assert "supera" in resp.json()["detail"]


def test_create_checks_endpoint_ok(client, monkeypatch):
    """POST /checks con cantidades válidas debe retornar 200 y checks creados."""
    from app.services import database as db_mod
    db_mod = _mock_auth(monkeypatch)

    async def mock_ticket_data(base_order_id, branch_id=None):
        return {
            "base_order_id": "BASE-001",
            "table_name": "Mesa 5",
            "items": [{"name": "Pizza", "price": 45000, "quantity": 2}],
            "total": 90000,
            "org_id": 1,
        }
    async def mock_create_checks(base_order_id, checks):
        return [{"id": "BASE-001-CHK-1", "check_number": 1, "total": 90000, "status": "open", "items": []}]

    monkeypatch.setattr(db_mod, "db_get_order_ticket_data", mock_ticket_data)
    monkeypatch.setattr(db_mod, "db_create_checks", mock_create_checks)

    resp = client.post(
        "/api/table-orders/BASE-001/checks",
        json={
            "checks": [
                {"check_number": 1, "items": [{"name": "Pizza", "qty": 2, "unit_price": 45000}]}
            ],
            "tax_pct": 19.0
        },
        headers={"Authorization": "Bearer fake"}
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert len(resp.json()["checks"]) == 1


def test_pay_check_endpoint_pago_insuficiente(client, monkeypatch):
    """POST /checks/{id}/pay debe retornar 400 si el pago no cubre el total."""
    from app.services import database as db_mod
    db_mod = _mock_auth(monkeypatch)

    # New flow: pay_check uses db_claim_check_for_payment, which transitions
    # open → paying atomically. Mock it to return a 'paying' check.
    async def mock_claim(check_id, base_order_id):
        return {"id": check_id, "base_order_id": base_order_id,
                "status": "paying", "total": 90000, "items": "[]"}

    async def mock_release(check_id):
        return True

    monkeypatch.setattr(db_mod, "db_claim_check_for_payment", mock_claim)
    monkeypatch.setattr(db_mod, "db_release_check", mock_release)

    # billing config returns None → payment would fail before billing
    async def mock_get_billing_config(restaurant_id):
        return {"provider": "mesio_native", "tax_regime": "iva", "tax_percentage": 19.0}

    monkeypatch.setattr("app.routes.tables.billing.get_billing_config", mock_get_billing_config)

    resp = client.post(
        "/api/table-orders/BASE-001/checks/BASE-001-CHK-1/pay",
        json={"payments": [{"method": "efectivo", "amount": 50000}]},
        headers={"Authorization": "Bearer fake"}
    )
    assert resp.status_code == 400
    assert "insuficiente" in resp.json()["detail"].lower()


def test_pay_check_endpoint_check_ya_cobrado(client, monkeypatch):
    """POST /checks/{id}/pay returns 409 (Conflict) if the check is already invoiced.

    Previously this returned 400; the race-free flow now uses 409 because the
    request conflicts with the current resource state — the more accurate HTTP
    semantic. The error detail still mentions 'procesado'.
    """
    from app.services import database as db_mod
    db_mod = _mock_auth(monkeypatch)

    # New flow: db_claim_check_for_payment returns None when status != 'open'.
    # Then the route falls back to db_get_check for error messaging.
    async def mock_claim(check_id, base_order_id):
        return None  # claim refused — already paying/invoiced/cancelled

    async def mock_get_check(check_id):
        return {"id": check_id, "base_order_id": "BASE-001",
                "status": "invoiced", "total": 90000, "items": "[]"}

    monkeypatch.setattr(db_mod, "db_claim_check_for_payment", mock_claim)
    monkeypatch.setattr(db_mod, "db_get_check", mock_get_check)

    resp = client.post(
        "/api/table-orders/BASE-001/checks/BASE-001-CHK-1/pay",
        json={"payments": [{"method": "efectivo", "amount": 90000}]},
        headers={"Authorization": "Bearer fake"}
    )
    assert resp.status_code == 409
    assert "procesado" in resp.json()["detail"].lower()


def test_delete_check_endpoint_ok(client, monkeypatch):
    """DELETE /checks/{id} debe retornar 200 si el check estaba open."""
    from app.services import database as db_mod
    db_mod = _mock_auth(monkeypatch)

    async def mock_delete(check_id): return True
    monkeypatch.setattr(db_mod, "db_delete_open_check", mock_delete)

    resp = client.delete(
        "/api/table-orders/BASE-001/checks/BASE-001-CHK-1",
        headers={"Authorization": "Bearer fake"}
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_delete_check_endpoint_ya_cobrado(client, monkeypatch):
    """DELETE /checks/{id} debe retornar 400 si el check ya fue cobrado."""
    from app.services import database as db_mod
    db_mod = _mock_auth(monkeypatch)

    async def mock_delete(check_id): return False  # no se pudo eliminar
    monkeypatch.setattr(db_mod, "db_delete_open_check", mock_delete)

    resp = client.delete(
        "/api/table-orders/BASE-001/checks/BASE-001-CHK-1",
        headers={"Authorization": "Bearer fake"}
    )
    assert resp.status_code == 400


def test_get_check_ticket_endpoint(client, monkeypatch):
    """GET /checks/{id}/ticket debe retornar los datos del check."""
    from app.services import database as db_mod
    db_mod = _mock_auth(monkeypatch)

    async def mock_get_ticket(check_id):
        return {
            "id": check_id, "check_number": 1, "base_order_id": "BASE-001",
            "table_name": "Mesa 5", "items": [], "total": 90000,
            "payments": [], "change_amount": 0, "status": "invoiced",
            "cufe": "a" * 96, "invoice_number": "FE0000001", "dian_status": "accepted",
        }
    monkeypatch.setattr(db_mod, "db_get_check_ticket", mock_get_ticket)

    resp = client.get(
        "/api/table-orders/BASE-001/checks/BASE-001-CHK-1/ticket",
        headers={"Authorization": "Bearer fake"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["check_number"] == 1
    assert data["dian_status"] == "accepted"
    assert len(data["cufe"]) == 96


# ══════════════════════════════════════════════════════════════════════════════
# 5. Regresión: tip cap con service_charge (bug fix v10.0)
#
# Before the fix, the 50% cap was checked against check["total"] alone.
# After the fix, cap_base = check["total"] + service_charge.
#
# These three tests pin the corrected behaviour permanently.
# ══════════════════════════════════════════════════════════════════════════════

def _pay_check_mocks(monkeypatch, check_total: float):
    """
    Shared setup for pay_check regression tests.

    Patches:
      - auth / restaurant (features={} so dian_active=False, no fiscal path)
      - db.db_claim_check_for_payment → returns "paying" check with the given total
      - db.db_get_check               → fallback for error messaging
      - billing.get_billing_config    → None (no DIAN path)
      - db.db_finalize_check_payment  → returns True (success path)
      - db.db_release_check           → no-op
      - loyalty_svc.accrue_on_check   → no-op (avoid AttributeError)
    """
    from app.services import database as db_mod

    async def mock_verify_token(token: str):
        return "caja_user"

    async def mock_get_user(username: str):
        return {"username": "caja_user", "branch_id": 1, "role": "caja", "restaurant_name": "R"}

    async def mock_get_restaurant(request):
        # features={} → dian_active=False, currency=None
        return {"id": 1, "whatsapp_number": "+57300", "name": "R", "features": {}}

    def _check_dict(check_id, status="paying"):
        return {
            "id":           check_id,
            "base_order_id": "BASE-001",
            "status":       status,
            "total":        check_total,
            "items":        "[]",
            "proposed_payments": None,
            "proposed_tip":     None,
        }

    async def mock_claim(check_id, base_order_id):
        # Race-free claim succeeded — returns the check with status='paying'.
        return _check_dict(check_id, status="paying")

    async def mock_get_check(check_id):
        # Used only for error messaging when claim returns None (not in this happy-path test).
        return _check_dict(check_id, status="open")

    async def mock_get_billing_config(restaurant_id):
        return None  # no fiscal path

    async def mock_finalize(*args, **kwargs):
        return True  # success path

    async def mock_release(check_id):
        return True

    async def mock_get_first_table_order(base_order_id):
        # Return None → skips the farewell/NPS block entirely
        return None

    monkeypatch.setattr("app.routes.deps.verify_token", mock_verify_token)
    monkeypatch.setattr(db_mod, "db_get_user", mock_get_user)
    monkeypatch.setattr("app.routes.tables.get_current_restaurant", mock_get_restaurant)
    monkeypatch.setattr(db_mod, "db_claim_check_for_payment", mock_claim)
    monkeypatch.setattr(db_mod, "db_get_check", mock_get_check)
    monkeypatch.setattr("app.routes.tables.billing.get_billing_config", mock_get_billing_config)
    monkeypatch.setattr(db_mod, "db_finalize_check_payment", mock_finalize)
    monkeypatch.setattr(db_mod, "db_release_check", mock_release)
    monkeypatch.setattr(db_mod, "db_get_first_table_order", mock_get_first_table_order)
    # Avoid loyalty AttributeError if the module has no accrue_on_check
    monkeypatch.setattr("app.routes.tables.loyalty_svc", MagicMock(spec=[]), raising=False)

    return db_mod


def test_tip_accepted_with_service_charge(client, monkeypatch):
    """
    Regression: tip cap must be based on (check.total + service_charge).

    check.total   = 90 000
    service_charge = 10 000
    combined       = 100 000
    tip            =  45 000  (45% of combined) → ACCEPTED
    """
    _pay_check_mocks(monkeypatch, check_total=90_000)

    resp = client.post(
        "/api/table-orders/BASE-001/checks/BASE-001-CHK-1/pay",
        json={
            "payments":       [{"method": "efectivo", "amount": 145_000}],
            "service_charge": 10_000,
            "tip_amount":     45_000,
        },
        headers={"Authorization": "Bearer fake"},
    )
    # Must NOT be rejected by the tip cap
    assert resp.status_code == 200, (
        f"Expected 200 but got {resp.status_code}: {resp.json()}"
    )
    assert resp.json()["success"] is True


def test_tip_rejected_over_50pct_of_combined(client, monkeypatch):
    """
    Tip that exceeds 50% of (check.total + service_charge) must be rejected.

    check.total    = 90 000
    service_charge = 10 000
    combined       = 100 000
    tip            =  51 000  (51% of combined) → REJECTED 400
    """
    _pay_check_mocks(monkeypatch, check_total=90_000)

    resp = client.post(
        "/api/table-orders/BASE-001/checks/BASE-001-CHK-1/pay",
        json={
            "payments":       [{"method": "efectivo", "amount": 151_000}],
            "service_charge": 10_000,
            "tip_amount":     51_000,
        },
        headers={"Authorization": "Bearer fake"},
    )
    assert resp.status_code == 400
    assert "propina" in resp.json()["detail"].lower()


def test_tip_rejected_over_50pct_without_service_charge(client, monkeypatch):
    """
    Without a service charge the old 50%-of-total cap still applies.

    check.total    = 90 000
    service_charge =      0
    tip            = 46 000  (>50% of 90 000) → REJECTED 400
    """
    _pay_check_mocks(monkeypatch, check_total=90_000)

    resp = client.post(
        "/api/table-orders/BASE-001/checks/BASE-001-CHK-1/pay",
        json={
            "payments":  [{"method": "efectivo", "amount": 136_000}],
            "tip_amount": 46_000,
        },
        headers={"Authorization": "Bearer fake"},
    )
    assert resp.status_code == 400
    assert "propina" in resp.json()["detail"].lower()
