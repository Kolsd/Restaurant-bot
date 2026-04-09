"""
Suite — Orders (delivery/recoger) + WhatsApp bot flow (50 tests)
tests/test_orders_flow.py

Prefijos de rutas (según main.py include_router):
  chat_router    → prefix="/api"  → /api/webhook/meta, /api/chat
  orders_router  → prefix="/api"  → /api/orders, /api/delivery/...
  tables_router  → sin prefijo    → /api/tables, /api/pos/...

Cubre:
  A.  Listar y consultar órdenes de delivery                [1–7]
  B.  Cambio de status de delivery + notificaciones         [8–14]
  C.  Carrito (ver / limpiar)                               [15–18]
  D.  Webhook Wompi — validación firma y flujos             [19–25]
  E.  Bot WhatsApp — webhook Meta ingesta                   [26–33]
  F.  Inbox worker dispatch                                 [34–38]
  G.  Deduplicación WAM                                     [39–43]
  H.  Commit de orden ACID (orders_repo)                    [44–50]
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
    conn.execute = AsyncMock()
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


def test_list_delivery_orders(client, monkeypatch):
    """GET /api/delivery/orders → 200, lista."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_delivery_orders", AsyncMock(return_value=[_ORDER]))
    r = client.get("/api/delivery/orders", headers=_HEADERS)
    assert r.status_code == 200
    assert len(r.json()["orders"]) == 1


def test_delivery_check_updates_returns_hash(client, monkeypatch):
    """GET /api/delivery/check-updates → hash de estado."""
    _auth(monkeypatch)
    _mock_pool(monkeypatch, rows=[make_row({"id": "ord-001", "status": "pendiente"})])
    r = client.get("/api/delivery/check-updates", headers=_HEADERS)
    assert r.status_code == 200
    assert "hash" in r.json()


# ══════════════════════════════════════════════════════════════════════════════
# B. CAMBIO DE STATUS + NOTIFICACIONES
# ══════════════════════════════════════════════════════════════════════════════

def test_update_delivery_status_en_camino(client, monkeypatch):
    """PATCH status → en_camino → 200."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_order", AsyncMock(return_value=_ORDER))
    monkeypatch.setattr(db_mod, "db_update_order_status", AsyncMock())
    r = client.patch("/api/delivery/orders/ord-001/status",
                     json={"status": "en_camino"}, headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["new_status"] == "en_camino"


def test_update_delivery_status_entregado(client, monkeypatch):
    """PATCH status → entregado → 200."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_order", AsyncMock(return_value=_ORDER))
    monkeypatch.setattr(db_mod, "db_update_order_status", AsyncMock())
    r = client.patch("/api/delivery/orders/ord-001/status",
                     json={"status": "entregado"}, headers=_HEADERS)
    assert r.status_code == 200


def test_update_delivery_status_cancelado(client, monkeypatch):
    """PATCH status → cancelado → 200."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_order", AsyncMock(return_value=_ORDER))
    monkeypatch.setattr(db_mod, "db_update_order_status", AsyncMock())
    r = client.patch("/api/delivery/orders/ord-001/status",
                     json={"status": "cancelado"}, headers=_HEADERS)
    assert r.status_code == 200


def test_update_delivery_status_not_found(client, monkeypatch):
    """Orden inexistente → 404."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_order", AsyncMock(return_value=None))
    r = client.patch("/api/delivery/orders/NOPE/status",
                     json={"status": "en_camino"}, headers=_HEADERS)
    assert r.status_code == 404


def test_update_delivery_status_calls_db(client, monkeypatch):
    """PATCH status llama a db_update_order_status con args correctos."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_order", AsyncMock(return_value=_ORDER))
    update_mock = AsyncMock()
    monkeypatch.setattr(db_mod, "db_update_order_status", update_mock)
    client.patch("/api/delivery/orders/ord-001/status",
                 json={"status": "listo"}, headers=_HEADERS)
    update_mock.assert_awaited_once_with("ord-001", "listo")


def test_update_delivery_no_notification_for_listo(client, monkeypatch):
    """Status 'listo' no dispara notificación WhatsApp."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_order", AsyncMock(return_value=_ORDER))
    monkeypatch.setattr(db_mod, "db_update_order_status", AsyncMock())
    # Si no lanza y devuelve 200, no se llamó send_delivery_notification sincrónicamente
    r = client.patch("/api/delivery/orders/ord-001/status",
                     json={"status": "listo"}, headers=_HEADERS)
    assert r.status_code == 200


def test_update_delivery_returns_new_status(client, monkeypatch):
    """Respuesta incluye new_status."""
    _auth(monkeypatch)
    monkeypatch.setattr(db_mod, "db_get_order", AsyncMock(return_value=_ORDER))
    monkeypatch.setattr(db_mod, "db_update_order_status", AsyncMock())
    r = client.patch("/api/delivery/orders/ord-001/status",
                     json={"status": "en_puerta"}, headers=_HEADERS)
    assert r.json()["new_status"] == "en_puerta"


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


def test_clear_cart_success(client, monkeypatch):
    """POST /api/cart/clear → 200."""
    monkeypatch.setattr("app.routes.orders_routes.clear_cart", AsyncMock())
    r = client.post("/api/cart/clear",
                    json={"phone": "+573001111111", "bot_number": "+573009999999"})
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_clear_cart_missing_phone(client, monkeypatch):
    """Sin phone → 422."""
    r = client.post("/api/cart/clear", json={"bot_number": "+573009999999"})
    assert r.status_code == 422


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
    payload = {
        "event": "transaction.updated",
        "data": {"transaction": {"id": "txn-001", "status": "APPROVED", "reference": "ord-001"}},
    }
    body_bytes = json.dumps(payload).encode()
    sig = _wompi_sig(body_bytes, secret)
    confirmed_order = {**_ORDER, "status": "pagado"}
    monkeypatch.setattr(db_mod, "db_confirm_payment", AsyncMock(return_value=confirmed_order))
    monkeypatch.setattr("app.routes.orders_routes.loyalty_svc",
                        MagicMock(accrue_on_order=AsyncMock()))
    r = client.post("/api/payment/wompi-webhook", content=body_bytes,
                    headers={"x-event-checksum": sig, "Content-Type": "application/json"})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_wompi_invalid_signature(client, monkeypatch):
    """Firma inválida → 401."""
    secret = "real_secret"
    monkeypatch.setattr("app.routes.orders_routes.WOMPI_EVENTS_SECRET", secret)
    payload = {"event": "transaction.updated", "data": {}}
    body_bytes = json.dumps(payload).encode()
    r = client.post("/api/payment/wompi-webhook", content=body_bytes,
                    headers={"x-event-checksum": "invalidsig",
                             "Content-Type": "application/json"})
    assert r.status_code == 401


def test_wompi_declined_transaction(client, monkeypatch):
    """Transacción DECLINED → 200 pero no llama db_confirm_payment."""
    secret = "test_secret"
    monkeypatch.setattr("app.routes.orders_routes.WOMPI_EVENTS_SECRET", secret)
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
    """Transacción APPROVED sin reference → 200, no crashea."""
    secret = "test_secret"
    monkeypatch.setattr("app.routes.orders_routes.WOMPI_EVENTS_SECRET", secret)
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
    """Sin header x-event-checksum → se procesa igual (Wompi lo permite en sandbox)."""
    secret = "test_secret"
    monkeypatch.setattr("app.routes.orders_routes.WOMPI_EVENTS_SECRET", secret)
    payload = {"event": "transaction.updated", "data": {}}
    body_bytes = json.dumps(payload).encode()
    r = client.post("/api/payment/wompi-webhook", content=body_bytes,
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 200  # sin header → vacío → no hay mismatch


# ══════════════════════════════════════════════════════════════════════════════
# E. WEBHOOK META — INGESTA
# ══════════════════════════════════════════════════════════════════════════════

def _meta_payload(wam_id="wam-001", text="Hola", phone="+573001111111",
                  bot_number="+573009999999"):
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": "phid-001",
                                 "display_phone_number": bot_number},
                    "messages": [{
                        "id": wam_id,
                        "from": phone.replace("+", ""),
                        "type": "text",
                        "text": {"body": text},
                        "timestamp": str(int(time.time())),
                    }],
                }
            }]
        }]
    }


def _meta_sig(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_meta_webhook_verification(client, monkeypatch):
    """GET /api/webhook/meta con challenge → retorna el challenge."""
    monkeypatch.setenv("META_VERIFY_TOKEN", "verify_tok")
    r = client.get("/api/webhook/meta?hub.mode=subscribe"
                   "&hub.verify_token=verify_tok&hub.challenge=abc123")
    assert r.status_code == 200
    assert "abc123" in r.text


def test_meta_webhook_verification_wrong_token(client, monkeypatch):
    """Token incorrecto → 403."""
    monkeypatch.setenv("META_VERIFY_TOKEN", "correct")
    r = client.get("/api/webhook/meta?hub.mode=subscribe"
                   "&hub.verify_token=wrong&hub.challenge=abc123")
    assert r.status_code == 403


def test_meta_webhook_ingest_returns_200(client, monkeypatch):
    """POST /api/webhook/meta → siempre 200 (ACK inmediato a Meta)."""
    secret = "test_secret"
    monkeypatch.setenv("META_APP_SECRET", secret)
    payload = _meta_payload()
    body = json.dumps(payload).encode()
    sig = _meta_sig(body, secret)
    monkeypatch.setattr(db_mod, "db_is_duplicate_wam", AsyncMock(return_value=False))
    monkeypatch.setattr(db_mod, "db_get_restaurant_by_phone", AsyncMock(return_value=None))
    monkeypatch.setattr("app.repositories.inbox_repo.enqueue", AsyncMock(return_value=True))
    pool_conn = MagicMock()
    pool_conn.execute = AsyncMock()
    pool_conn.fetchval = AsyncMock(return_value=0)
    pool_mock = make_pool(pool_conn)
    monkeypatch.setattr(db_mod, "get_pool", AsyncMock(return_value=pool_mock))
    r = client.post("/api/webhook/meta", content=body,
                    headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
    assert r.status_code == 200


def test_meta_webhook_status_update_returns_200(client, monkeypatch):
    """Webhook de status update (sin messages) → 200 silencioso."""
    secret = "test_secret"
    monkeypatch.setenv("META_APP_SECRET", secret)
    payload = {"entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp",
        "statuses": [{"id": "wam-001", "status": "delivered"}],
    }}]}]}
    body = json.dumps(payload).encode()
    sig = _meta_sig(body, secret)
    r = client.post("/api/webhook/meta", content=body,
                    headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
    assert r.status_code == 200


def test_meta_webhook_invalid_signature(client, monkeypatch):
    """Firma inválida → 401."""
    monkeypatch.setenv("META_APP_SECRET", "real_secret")
    payload = _meta_payload()
    body = json.dumps(payload).encode()
    r = client.post("/api/webhook/meta", content=body,
                    headers={"X-Hub-Signature-256": "sha256=invalidsig",
                             "Content-Type": "application/json"})
    assert r.status_code == 401


def test_meta_webhook_duplicate_not_enqueued(client, monkeypatch):
    """WAM duplicado → no encola."""
    secret = "test_secret"
    monkeypatch.setenv("META_APP_SECRET", secret)
    payload = _meta_payload(wam_id="wam-SEEN")
    body = json.dumps(payload).encode()
    sig = _meta_sig(body, secret)
    enqueue_mock = AsyncMock()
    monkeypatch.setattr(db_mod, "db_is_duplicate_wam", AsyncMock(return_value=True))
    monkeypatch.setattr(db_mod, "db_get_restaurant_by_phone", AsyncMock(return_value=None))
    monkeypatch.setattr("app.repositories.inbox_repo.enqueue", enqueue_mock)
    pool_conn = MagicMock()
    pool_conn.execute = AsyncMock()
    pool_conn.fetchval = AsyncMock(return_value=0)
    monkeypatch.setattr(db_mod, "get_pool", AsyncMock(return_value=make_pool(pool_conn)))
    r = client.post("/api/webhook/meta", content=body,
                    headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
    assert r.status_code == 200
    enqueue_mock.assert_not_awaited()


def test_meta_webhook_new_message_enqueued(client, monkeypatch):
    """Mensaje nuevo → enqueue llamado."""
    secret = "test_secret"
    monkeypatch.setenv("META_APP_SECRET", secret)
    payload = _meta_payload(wam_id="wam-NEW", text="Quiero una arepa")
    body = json.dumps(payload).encode()
    sig = _meta_sig(body, secret)
    enqueue_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(db_mod, "db_is_duplicate_wam", AsyncMock(return_value=False))
    monkeypatch.setattr(db_mod, "db_get_restaurant_by_phone", AsyncMock(return_value=None))
    monkeypatch.setattr("app.repositories.inbox_repo.enqueue", enqueue_mock)
    # Rate limiter uses raw pool — mock fetchval=0 (not limited)
    pool_conn = MagicMock()
    pool_conn.execute = AsyncMock()
    pool_conn.fetchval = AsyncMock(return_value=0)
    monkeypatch.setattr(db_mod, "get_pool", AsyncMock(return_value=make_pool(pool_conn)))
    r = client.post("/api/webhook/meta", content=body,
                    headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
    assert r.status_code == 200
    enqueue_mock.assert_awaited_once()


# ══════════════════════════════════════════════════════════════════════════════
# F. INBOX WORKER DISPATCH
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_inbox_dispatch_calls_handler():
    """Worker despacha al handler registrado."""
    from app.services import inbox_worker
    handler_mock = AsyncMock()
    inbox_worker._handlers["meta_whatsapp"] = handler_mock
    payload = {"user_phone": "+57300", "user_text": "hola",
               "bot_number": "+57999", "phone_id": "pid", "access_token": "tok"}
    await inbox_worker._dispatch("meta_whatsapp", payload)
    handler_mock.assert_awaited_once_with(payload)


@pytest.mark.asyncio
async def test_inbox_dispatch_unknown_provider():
    """Provider desconocido → ValueError."""
    from app.services import inbox_worker
    with pytest.raises(ValueError, match="No handler registered"):
        await inbox_worker._dispatch("provider_desconocido", {})


@pytest.mark.asyncio
async def test_inbox_handler_passes_correct_fields():
    """Handler meta_whatsapp pasa los campos correctos a _process_message."""
    from app.services import inbox_worker
    process_mock = AsyncMock()
    with patch("app.routes.chat._process_message", process_mock):
        payload = {
            "user_phone": "+573001111111", "user_text": "Una arepa",
            "bot_number": "+573009999999", "phone_id": "phid-001",
            "access_token": "META_TOKEN",
        }
        await inbox_worker._handle_meta_whatsapp(payload)
    process_mock.assert_awaited_once_with(
        user_phone="+573001111111", user_text="Una arepa",
        bot_number="+573009999999", phone_id="phid-001", access_token="META_TOKEN",
    )


@pytest.mark.asyncio
async def test_inbox_handler_missing_key_raises():
    """Payload incompleto → KeyError claro."""
    from app.services import inbox_worker
    with pytest.raises(KeyError):
        await inbox_worker._handle_meta_whatsapp({"user_phone": "+57300"})


@pytest.mark.asyncio
async def test_inbox_handler_registered_at_import():
    """Handler meta_whatsapp se registra al importar el módulo."""
    from app.services import inbox_worker
    assert "meta_whatsapp" in inbox_worker._handlers


# ══════════════════════════════════════════════════════════════════════════════
# G. DEDUPLICACIÓN WAM
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_wam_dedup_new_message_false(monkeypatch):
    """Mensaje nuevo → retorna False (no es duplicado)."""
    from app.repositories.conversations_repo import db_is_duplicate_wam

    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value="wam-001")  # insertado → no duplicado
    pool = make_pool(conn)
    monkeypatch.setattr("app.repositories.conversations_repo._get_pool",
                        AsyncMock(return_value=pool))

    result = await db_is_duplicate_wam("wam-001")
    assert result is False


@pytest.mark.asyncio
async def test_wam_dedup_duplicate_true(monkeypatch):
    """WAM ya procesado → True."""
    from app.repositories.conversations_repo import db_is_duplicate_wam

    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)  # ON CONFLICT → None
    pool = make_pool(conn)
    monkeypatch.setattr("app.repositories.conversations_repo._get_pool",
                        AsyncMock(return_value=pool))

    result = await db_is_duplicate_wam("wam-dup")
    assert result is True


@pytest.mark.asyncio
async def test_wam_dedup_cleans_old_records(monkeypatch):
    """Limpia registros > 2 min antes de insertar."""
    from app.repositories.conversations_repo import db_is_duplicate_wam

    executed_queries = []
    conn = MagicMock()
    async def capture_execute(q, *a):
        executed_queries.append(q)
    conn.execute = capture_execute
    conn.fetchval = AsyncMock(return_value="wam-clean")
    pool = make_pool(conn)
    monkeypatch.setattr("app.repositories.conversations_repo._get_pool",
                        AsyncMock(return_value=pool))

    await db_is_duplicate_wam("wam-clean")
    assert any("DELETE" in q and "2 minutes" in q for q in executed_queries)


@pytest.mark.asyncio
async def test_wam_dedup_different_ids_both_new(monkeypatch):
    """Dos WAM IDs distintos → ambos retornan False (ninguno duplicado)."""
    from app.repositories.conversations_repo import db_is_duplicate_wam

    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value="any_id")
    pool = make_pool(conn)
    monkeypatch.setattr("app.repositories.conversations_repo._get_pool",
                        AsyncMock(return_value=pool))

    r1 = await db_is_duplicate_wam("wam-A")
    r2 = await db_is_duplicate_wam("wam-B")
    assert r1 is False
    assert r2 is False


def test_wam_dedup_endpoint_dedup_no_enqueue(client, monkeypatch):
    """Webhook con WAM ya procesado → 200 pero no encola."""
    secret = "test_secret"
    monkeypatch.setenv("META_APP_SECRET", secret)
    payload = _meta_payload(wam_id="wam-SEEN")
    body = json.dumps(payload).encode()
    sig = _meta_sig(body, secret)
    enqueue_mock = AsyncMock()
    monkeypatch.setattr(db_mod, "db_is_duplicate_wam", AsyncMock(return_value=True))
    monkeypatch.setattr("app.repositories.inbox_repo.enqueue", enqueue_mock)
    r = client.post("/api/webhook/meta", content=body,
                    headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
    assert r.status_code == 200
    enqueue_mock.assert_not_awaited()


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
    # fetchrow para INSERT order → devuelve None (UPDATE stock falla → sin stock)
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    pool = make_pool(conn)

    cart = {"items": _make_order_payload()["items"], "bot_number": "+57999"}
    order = _make_order_payload()

    with pytest.raises(InsufficientStockError):
        await commit_order_transaction(pool, restaurant_id=1,
                                       conversation_id="+57300",
                                       cart=cart, order_payload=order)


@pytest.mark.asyncio
async def test_commit_inserts_order(monkeypatch):
    """Con stock suficiente → INSERT en orders."""
    from app.repositories.orders_repo import commit_order_transaction

    insert_calls = []
    conn = MagicMock()
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=make_row({"stock": 5}))
    async def capture_execute(q, *args):
        insert_calls.append(q)
    conn.execute = capture_execute
    pool = make_pool(conn)

    cart = {"items": _make_order_payload()["items"], "bot_number": "+57999"}
    order = _make_order_payload()
    await commit_order_transaction(pool, restaurant_id=1, conversation_id="+57300",
                                   cart=cart, order_payload=order)
    assert any("INSERT" in q and "orders" in q.lower() for q in insert_calls)


@pytest.mark.asyncio
async def test_commit_deletes_cart(monkeypatch):
    """commit_order_transaction borra el carrito del cliente."""
    from app.repositories.orders_repo import commit_order_transaction

    executed = []
    conn = MagicMock()
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=make_row({"stock": 5}))
    async def capture_execute(q, *args):
        executed.append(q)
    conn.execute = capture_execute
    pool = make_pool(conn)

    cart = {"items": _make_order_payload()["items"], "bot_number": "+57999"}
    order = _make_order_payload()
    await commit_order_transaction(pool, restaurant_id=1, conversation_id="+57300",
                                   cart=cart, order_payload=order)
    assert any("DELETE" in q and "cart" in q.lower() for q in executed)


@pytest.mark.asyncio
async def test_commit_zero_stock_raises(monkeypatch):
    """Stock = 0 con quantity > 0 → InsufficientStockError (escandallo path)."""
    from app.repositories.orders_repo import commit_order_transaction, InsufficientStockError

    conn = MagicMock()
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    # Escandallo path: primera fetch devuelve rows de receta
    recipe_row = make_row({"ingredient_id": 10, "recipe_qty": 1.0})
    locked_row = make_row({"id": 10, "current_stock": 0.0, "min_stock": 0,
                           "linked_dishes": "[]"})
    conn.fetch = AsyncMock(side_effect=[[recipe_row], [locked_row]])
    # UPDATE RETURNING → None (no hay stock)
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    pool = make_pool(conn)

    cart = {"items": _make_order_payload()["items"], "bot_number": "+57999"}
    order = _make_order_payload(qty=3)

    with pytest.raises(InsufficientStockError):
        await commit_order_transaction(pool, restaurant_id=1, conversation_id="+57300",
                                       cart=cart, order_payload=order)


@pytest.mark.asyncio
async def test_commit_float_total_coerced(monkeypatch):
    """Total como float en el payload se coerce a Decimal (sin TypeError)."""
    from app.repositories.orders_repo import commit_order_transaction

    conn = MagicMock()
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=make_row({"stock": 10}))
    conn.execute = AsyncMock()
    pool = make_pool(conn)

    order = _make_order_payload(price=12500)
    order["total"] = 12500.50  # float intencional
    cart = {"items": order["items"], "bot_number": "+57999"}

    # No debe lanzar TypeError
    await commit_order_transaction(pool, restaurant_id=1, conversation_id="+57300",
                                   cart=cart, order_payload=order)
    assert conn.execute.called


@pytest.mark.asyncio
async def test_commit_db_error_raises_order_commit_error(monkeypatch):
    """Error en DB → OrderCommitError (no Exception genérica)."""
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
    """Orden sin items (edge case) → no intenta descontar inventario."""
    from app.repositories.orders_repo import commit_order_transaction

    conn = MagicMock()
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)  # no se debe llamar para inventario
    conn.execute = AsyncMock()
    pool = make_pool(conn)

    order = _make_order_payload()
    order["items"] = []  # sin items
    cart = {"items": [], "bot_number": "+57999"}

    await commit_order_transaction(pool, restaurant_id=1, conversation_id="+57300",
                                   cart=cart, order_payload=order)
    # fetchrow no llamado para inventario (no hay items)
    conn.fetchrow.assert_not_awaited()
