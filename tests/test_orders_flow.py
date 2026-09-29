"""
Suite — Orders (delivery/recoger) + WhatsApp bot flow
tests/test_orders_flow.py

Prefijos de rutas (según main.py include_router):
  chat_router    → prefix="/api"  → /api/webhook/meta, /api/chat
  orders_router  → prefix="/api"  → /api/orders, /api/cart, /api/payment/...
  tables_router  → sin prefijo    → /api/tables, /api/pos/...

Cubre:
  A.  Listar y consultar órdenes (/api/orders)
  C.  Carrito (ver / limpiar)
  D.  Webhook Wompi — validación firma y flujos
  E.  Bot WhatsApp — webhook Meta ingesta
  F.  Inbox worker dispatch
  G.  Deduplicación WAM
  H.  Commit de orden ACID (orders_repo)

Section B (GET/PATCH /api/delivery/orders* — org-wide, no sede scoping) and
the delivery-order rows in section A were deleted in chunk 9
(docs/claude/delivery-web.md): WhatsApp delivery/pickup ordering was retired
and those endpoints were removed (any staff still needing that data uses the
sede-scoped /api/staff/delivery/* routes, app/routes/staff_delivery.py).
"""
import hashlib
import hmac
import json
import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import make_pool, make_row, patch_auth
import app.services.database as db_mod
from app.services.tenant_context import bypass_tenant_scope as _bypass

_HEADERS = {"Authorization": "Bearer tok"}


def _auth(monkeypatch, features=None):
    if features is None:
        features = {"staff_tips": True}
    patch_auth(monkeypatch, features=features)
    monkeypatch.setattr(db_mod, "db_check_module", AsyncMock(return_value=True))


def _mock_pool(monkeypatch, rows=None, fetchrow_result=None):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows or [])
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__  = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    pool = make_pool(conn)
    monkeypatch.setattr(db_mod, "get_pool", AsyncMock(return_value=pool))
    return conn


# ─── fixtures ─────────────────────────────────────────────────────────────────

_ORDER = {
    "id": "ord-001", "restaurant_id": 1, "phone": "+573001111111",
    "order_type": "domicilio", "status": "pendiente", "paid": False,
    "items": [{"name": "Pizza", "quantity": 1, "price": 35000}],
    "total": 35000, "address": "Calle 123", "created_at": "2026-04-08T10:00:00",
    "bot_number": "+573009999999",
}


# ══════════════════════════════════════════════════════════════════════════════
# A. LISTAR Y CONSULTAR ÓRDENES
# ══════════════════════════════════════════════════════════════════════════════

def test_list_orders_dashboard(client, monkeypatch):
    """GET /api/orders → 200 con summary y lista."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_all_orders", AsyncMock(return_value=[_ORDER]))
    r = client.get("/api/orders", headers=_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert "summary" in body
    assert "orders" in body


def test_list_orders_empty(client, monkeypatch):
    """Sin órdenes → summary con ceros."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_all_orders", AsyncMock(return_value=[]))
    r = client.get("/api/orders", headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["summary"]["total_orders"] == 0


def test_list_orders_counts_paid(client, monkeypatch):
    """Summary cuenta correctamente órdenes pagadas."""
    _auth(monkeypatch)
    paid_order = {**_ORDER, "paid": True}
    monkeypatch.setattr(db_mod, "db_get_all_orders",
                        AsyncMock(return_value=[_ORDER, paid_order]))
    r = client.get("/api/orders", headers=_HEADERS)
    summary = r.json()["summary"]
    assert summary["paid"] == 1
    assert summary["pending_payment"] == 1


def test_get_single_order_success(client, monkeypatch):
    """GET /api/orders/{id} → 200, devuelve la orden."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_order", AsyncMock(return_value=_ORDER))
    r = client.get("/api/orders/ord-001", headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["id"] == "ord-001"


def test_get_single_order_not_found(client, monkeypatch):
    """Orden inexistente → 404."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_order", AsyncMock(return_value=None))
    r = client.get("/api/orders/NOPE", headers=_HEADERS)
    assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# C. CARRITO
# ══════════════════════════════════════════════════════════════════════════════

def test_view_cart_with_items(client, monkeypatch):
    """GET /api/cart/{phone}/{bot} → 200, summary con items."""
    _auth(monkeypatch)
    summary = {"items": [{"name": "Pizza", "quantity": 1, "price": 35000}],
               "total": 35000, "subtotal": 35000}
    monkeypatch.setattr("app.routes.orders_routes.cart_summary", AsyncMock(return_value=summary))
    r = client.get("/api/cart/+573001111111/+573009999999", headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["summary"]["total"] == 35000


def test_view_cart_empty(client, monkeypatch):
    """Carrito vacío → items=[]."""
    _auth(monkeypatch)
    monkeypatch.setattr("app.routes.orders_routes.cart_summary",
                        AsyncMock(return_value={"items": [], "total": 0, "subtotal": 0}))
    r = client.get("/api/cart/+573001111111/+573009999999", headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["summary"]["items"] == []


# ══════════════════════════════════════════════════════════════════════════════
# D. WEBHOOK WOMPI
# ══════════════════════════════════════════════════════════════════════════════

def _wompi_sig(body_bytes: bytes, secret: str) -> str:
    return hashlib.sha256((body_bytes.decode() + secret).encode()).hexdigest()


def test_wompi_no_secret_configured(client, monkeypatch):
    """Sin WOMPI_EVENTS_SECRET → 500."""
    monkeypatch.setattr("app.routes.orders_routes.WOMPI_EVENTS_SECRET", None)
    r = client.post("/api/payment/wompi-webhook",
                    json={"event": "transaction.updated", "data": {}})
    assert r.status_code == 500


def test_wompi_valid_approved_transaction(client, monkeypatch):
    """Transacción APPROVED con firma válida → 200, orden confirmada."""
    secret = "test_wompi_secret"
    monkeypatch.setattr("app.routes.orders_routes.WOMPI_EVENTS_SECRET", secret)
    # record_wompi_event uses the GLOBAL processed_wompi_events table; mock so
    # the test doesn't need a real DB. True = first time (proceed with processing).
    monkeypatch.setattr(
        "app.routes.orders_routes.record_wompi_event",
        AsyncMock(return_value=True),
    )
    payload = {
        "event": "transaction.updated",
        "data": {"transaction": {"id": "txn-001", "status": "APPROVED", "reference": "ord-001"}},
    }
    body_bytes = json.dumps(payload).encode()
    sig = _wompi_sig(body_bytes, secret)
    confirmed_order = {**_ORDER, "status": "pagado", "restaurant_id": 1}
    monkeypatch.setattr(db_mod, "db_confirm_payment", AsyncMock(return_value=confirmed_order))
    r = client.post("/api/payment/wompi-webhook", content=body_bytes,
                    headers={"x-event-checksum": sig, "Content-Type": "application/json"})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_wompi_invalid_signature(client, monkeypatch):
    """Firma inválida → 200 con status=invalid_signature (H2: prevent Wompi retry flood)."""
    secret = "real_secret"
    monkeypatch.setattr("app.routes.orders_routes.WOMPI_EVENTS_SECRET", secret)
    monkeypatch.setattr("app.services.state_store.rate_limit_check", AsyncMock(return_value=True))
    payload = {"event": "transaction.updated", "data": {}}
    body_bytes = json.dumps(payload).encode()
    r = client.post("/api/payment/wompi-webhook", content=body_bytes,
                    headers={"x-event-checksum": "invalidsig",
                             "Content-Type": "application/json"})
    assert r.status_code == 200
    assert r.json().get("status") == "invalid_signature"


def test_wompi_declined_transaction(client, monkeypatch):
    """DECLINED transaction → 200 but does not call db_confirm_payment."""
    secret = "test_secret"
    monkeypatch.setattr("app.routes.orders_routes.WOMPI_EVENTS_SECRET", secret)
    monkeypatch.setattr(
        "app.routes.orders_routes.record_wompi_event",
        AsyncMock(return_value=True),
    )
    payload = {
        "event": "transaction.updated",
        "data": {"transaction": {"id": "txn-002", "status": "DECLINED", "reference": "ord-001"}},
    }
    body_bytes = json.dumps(payload).encode()
    sig = _wompi_sig(body_bytes, secret)
    confirm_mock = AsyncMock()
    monkeypatch.setattr(db_mod, "db_confirm_payment", confirm_mock)
    r = client.post("/api/payment/wompi-webhook", content=body_bytes,
                    headers={"x-event-checksum": sig, "Content-Type": "application/json"})
    assert r.status_code == 200
    confirm_mock.assert_not_awaited()


def test_wompi_unknown_event(client, monkeypatch):
    """Evento desconocido → 200 (ignorado silenciosamente)."""
    secret = "test_secret"
    monkeypatch.setattr("app.routes.orders_routes.WOMPI_EVENTS_SECRET", secret)
    payload = {"event": "refund.created", "data": {}}
    body_bytes = json.dumps(payload).encode()
    sig = _wompi_sig(body_bytes, secret)
    r = client.post("/api/payment/wompi-webhook", content=body_bytes,
                    headers={"x-event-checksum": sig, "Content-Type": "application/json"})
    assert r.status_code == 200


def test_wompi_no_reference(client, monkeypatch):
    """APPROVED transaction without reference → 200, doesn't crash."""
    secret = "test_secret"
    monkeypatch.setattr("app.routes.orders_routes.WOMPI_EVENTS_SECRET", secret)
    monkeypatch.setattr(
        "app.routes.orders_routes.record_wompi_event",
        AsyncMock(return_value=True),
    )
    payload = {
        "event": "transaction.updated",
        "data": {"transaction": {"id": "txn-003", "status": "APPROVED"}},  # sin reference
    }
    body_bytes = json.dumps(payload).encode()
    sig = _wompi_sig(body_bytes, secret)
    monkeypatch.setattr(db_mod, "db_confirm_payment", AsyncMock(return_value=None))
    r = client.post("/api/payment/wompi-webhook", content=body_bytes,
                    headers={"x-event-checksum": sig, "Content-Type": "application/json"})
    assert r.status_code == 200


def test_wompi_no_signature_header(client, monkeypatch):
    """Sin header x-event-checksum → 200 con status=invalid_signature (H2: prevent Wompi retry flood)."""
    secret = "test_secret"
    monkeypatch.setattr("app.routes.orders_routes.WOMPI_EVENTS_SECRET", secret)
    monkeypatch.setattr("app.services.state_store.rate_limit_check", AsyncMock(return_value=True))
    payload = {"event": "transaction.updated", "data": {}}
    body_bytes = json.dumps(payload).encode()
    r = client.post("/api/payment/wompi-webhook", content=body_bytes,
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 200  # H2: 200 to prevent Wompi retry storm
    assert r.json().get("status") == "invalid_signature"


# ══════════════════════════════════════════════════════════════════════════════
# E. WEBHOOK META — INGESTA
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# F. INBOX WORKER DISPATCH
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# G. DEDUPLICACIÓN WAM
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# H. COMMIT DE ORDEN ACID (orders_repo)
# ══════════════════════════════════════════════════════════════════════════════

def _make_order_payload(order_id="ord-test", sku="pizza-m", qty=1, price=35000):
    return {
        "id": order_id, "restaurant_id": 1, "phone": "+57300",
        "order_type": "domicilio", "status": "pendiente",
        "items": [{"sku": sku, "quantity": qty, "price": price,
                   "name": "Pizza", "subtotal": price * qty}],
        "subtotal": price * qty,
        "delivery_fee": 0,
        "total": price * qty,
        "paid": False,
        "address": "Calle 1",
        "bot_number": "+57999",
    }


def _make_commit_mocks(monkeypatch, stock_row=None, execute_side_effect=None):
    """Set up pool mock for orders_repo commit tests."""
    conn = MagicMock()
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    # fetch([]) → no recipe rows → falls through to legacy linked_dishes path
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=stock_row)
    if execute_side_effect:
        conn.execute = AsyncMock(side_effect=execute_side_effect)
    else:
        conn.execute = AsyncMock()
    pool = make_pool(conn)
    monkeypatch.setattr(db_mod, "get_pool", AsyncMock(return_value=pool))
    return conn, pool


@pytest.mark.asyncio
async def test_commit_insufficient_stock_raises(monkeypatch):
    """Stock insuficiente en path legado → InsufficientStockError."""
    from app.repositories.orders_repo import commit_order_transaction, InsufficientStockError
    from app.services.tenant_context import tenant_scope

    conn = MagicMock()
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    # Primera llamada a fetch → sin receta (escandallo vacío)
    # Segunda llamada a fetch → hay un row de inventario (legacy path)
    inv_row = make_row({"id": 1, "current_stock": 2.0, "linked_dishes": '["Pizza"]',
                        "min_stock": 0})
    conn.fetch = AsyncMock(side_effect=[[], [inv_row]])
    # fetchval → set_config GUC (tenant_connection); fetchrow → UPDATE stock falla → sin stock
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    pool = make_pool(conn)

    cart = {"items": _make_order_payload()["items"], "bot_number": "+57999"}
    order = _make_order_payload()

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(1):
            with pytest.raises(InsufficientStockError):
                await commit_order_transaction(pool, restaurant_id=1,
                                               conversation_id="+57300",
                                               cart=cart, order_payload=order)


@pytest.mark.asyncio
async def test_commit_inserts_order(monkeypatch):
    """Con stock suficiente → INSERT en orders."""
    from app.repositories.orders_repo import commit_order_transaction
    from app.services.tenant_context import tenant_scope

    insert_calls = []
    conn = MagicMock()
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(return_value=make_row({"stock": 5}))
    async def capture_execute(q, *args):
        insert_calls.append(q)
    conn.execute = capture_execute
    pool = make_pool(conn)

    cart = {"items": _make_order_payload()["items"], "bot_number": "+57999"}
    order = _make_order_payload()

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(1):
            await commit_order_transaction(pool, restaurant_id=1, conversation_id="+57300",
                                           cart=cart, order_payload=order)
    assert any("INSERT" in q and "orders" in q.lower() for q in insert_calls)


@pytest.mark.asyncio
async def test_commit_deletes_cart(monkeypatch):
    """commit_order_transaction clears the customer's cart."""
    from app.repositories.orders_repo import commit_order_transaction
    from app.services.tenant_context import tenant_scope

    executed = []
    conn = MagicMock()
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(return_value=make_row({"stock": 5}))
    async def capture_execute(q, *args):
        executed.append(q)
    conn.execute = capture_execute
    pool = make_pool(conn)

    cart = {"items": _make_order_payload()["items"], "bot_number": "+57999"}
    order = _make_order_payload()

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(1):
            await commit_order_transaction(pool, restaurant_id=1, conversation_id="+57300",
                                           cart=cart, order_payload=order)
    assert any("DELETE" in q and "cart" in q.lower() for q in executed)


@pytest.mark.asyncio
async def test_commit_zero_stock_raises(monkeypatch):
    """Stock = 0 con quantity > 0 → InsufficientStockError (escandallo path)."""
    from app.repositories.orders_repo import commit_order_transaction, InsufficientStockError
    from app.services.tenant_context import tenant_scope

    conn = MagicMock()
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    # Escandallo path: primera fetch devuelve rows de receta
    recipe_row = make_row({"ingredient_id": 10, "recipe_qty": 1.0})
    # recipe_ingredient_id is what dish_recipes points at; id is the row of
    # the sede actually cooking (same here — one sede).
    locked_row = make_row({"recipe_ingredient_id": 10, "id": 10,
                           "current_stock": 0.0, "min_stock": 0,
                           "linked_dishes": "[]"})
    conn.fetch = AsyncMock(side_effect=[[recipe_row], [locked_row]])
    # fetchval → set_config GUC (tenant_connection)
    conn.fetchval = AsyncMock(return_value=None)
    # UPDATE RETURNING → None (no hay stock)
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    pool = make_pool(conn)

    cart = {"items": _make_order_payload()["items"], "bot_number": "+57999"}
    order = _make_order_payload(qty=3)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(1):
            with pytest.raises(InsufficientStockError):
                await commit_order_transaction(pool, restaurant_id=1, conversation_id="+57300",
                                               cart=cart, order_payload=order)


@pytest.mark.asyncio
async def test_commit_float_total_coerced(monkeypatch):
    """Total como float en el payload se coerce a Decimal (sin TypeError)."""
    from app.repositories.orders_repo import commit_order_transaction
    from app.services.tenant_context import tenant_scope

    conn = MagicMock()
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(return_value=make_row({"stock": 10}))
    conn.execute = AsyncMock()
    pool = make_pool(conn)

    order = _make_order_payload(price=12500)
    order["total"] = 12500.50  # deliberate float
    cart = {"items": order["items"], "bot_number": "+57999"}

    # Must not raise TypeError
    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(1):
            await commit_order_transaction(pool, restaurant_id=1, conversation_id="+57300",
                                           cart=cart, order_payload=order)
    assert conn.execute.called


@pytest.mark.asyncio
async def test_commit_db_error_raises_order_commit_error(monkeypatch):
    """DB error → OrderCommitError (not a generic Exception)."""
    from app.repositories.orders_repo import commit_order_transaction, OrderCommitError

    conn = MagicMock()
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=make_row({"stock": 10}))
    conn.execute = AsyncMock(side_effect=Exception("DB connection lost"))
    pool = make_pool(conn)

    order = _make_order_payload()
    cart = {"items": order["items"], "bot_number": "+57999"}

    with pytest.raises((OrderCommitError, Exception)):
        await commit_order_transaction(pool, restaurant_id=1, conversation_id="+57300",
                                       cart=cart, order_payload=order)


@pytest.mark.asyncio
async def test_commit_no_items_no_inventory_deduction(monkeypatch):
    """Order without items (edge case) → does not attempt to deduct inventory."""
    from app.repositories.orders_repo import commit_order_transaction
    from app.services.tenant_context import tenant_scope

    conn = MagicMock()
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(return_value=None)  # must not be called for inventory
    conn.execute = AsyncMock()
    pool = make_pool(conn)

    order = _make_order_payload()
    order["items"] = []  # no items
    cart = {"items": [], "bot_number": "+57999"}

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(1):
            await commit_order_transaction(pool, restaurant_id=1, conversation_id="+57300",
                                           cart=cart, order_payload=order)
    # fetchrow not called for inventory (no items)
    conn.fetchrow.assert_not_awaited()
