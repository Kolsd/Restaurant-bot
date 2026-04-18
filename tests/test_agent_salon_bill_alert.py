"""
tests/test_agent_salon_bill_alert.py

Locks down the behavior introduced in commit d476e7c:
  - When the customer triggers action='bill' on a table with an active
    base_order, agent_salon.execute_salon_action MUST fire
    db_create_waiter_alert(alert_type='bill', ...) so floor staff sees
    the request immediately on the POS.
  - Failure to create the alert MUST NOT block the checkout flow
    (best-effort try/except — verified by raising from the mock and
    confirming the function still returns a normal reply).
  - The alert message MUST identify the table by name and include the
    subtotal so staff has context.

CLAUDE.md Regla #17 mandates this stays.
"""
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def base_table_context():
    return {"id": "table-uuid-1", "name": "Mesa 5"}


@pytest.fixture
def parsed_bill():
    return {"action": "bill", "reply": "Vale, te procesa la cuenta"}


def _patches_for_bill_path(create_alert_mock, alert_side_effect=None):
    """Patch every dep that the bill path touches before reaching the alert."""
    from app.services import agent_salon
    if alert_side_effect is not None:
        create_alert_mock.side_effect = alert_side_effect

    # _tenant_conn is used to fetch table_orders rows; build a fake connection
    fake_rows = [{"total": "30000", "items": json.dumps([{"name": "Pizza", "qty": 1}])}]
    fake_conn = MagicMock()
    fake_conn.fetch = AsyncMock(return_value=fake_rows)
    fake_ctx = MagicMock()
    fake_ctx.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_ctx.__aexit__ = AsyncMock(return_value=False)

    return [
        patch.object(agent_salon.db, "db_get_base_order_id", AsyncMock(return_value="base-1")),
        patch.object(agent_salon, "_tenant_conn", MagicMock(return_value=fake_ctx)),
        patch.object(agent_salon.state_store, "checkout_set", AsyncMock(return_value=None)),
        patch.object(agent_salon.db, "db_create_waiter_alert", create_alert_mock),
    ]


async def _run_bill_action(parsed, table_context, alert_mock, alert_side_effect=None):
    from app.services.agent_salon import execute_salon_action
    patches = _patches_for_bill_path(alert_mock, alert_side_effect=alert_side_effect)
    for p in patches:
        p.start()
    try:
        return await execute_salon_action(
            parsed=parsed,
            phone="+573000000001",
            bot_number="573001112222",
            table_context=table_context,
            session_state={},
            full_history=[],
            restaurant_obj={"id": 1, "name": "Test"},
            message="quiero la cuenta",
        )
    finally:
        for p in patches:
            p.stop()


# ── Happy path: alert fires ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bill_action_fires_waiter_alert(base_table_context, parsed_bill):
    """action='bill' MUST call db_create_waiter_alert exactly once with type='bill'."""
    alert_mock = AsyncMock(return_value={"id": 99})
    reply = await _run_bill_action(parsed_bill, base_table_context, alert_mock)

    alert_mock.assert_awaited_once()
    kwargs = alert_mock.await_args.kwargs
    assert kwargs["alert_type"] == "bill"
    assert kwargs["table_id"] == "table-uuid-1"
    assert kwargs["table_name"] == "Mesa 5"
    assert kwargs["bot_number"] == "573001112222"
    assert "Mesa 5" in kwargs["message"], "Alert message must mention the table by name"
    assert reply  # checkout flow returned something to send to the customer


@pytest.mark.asyncio
async def test_bill_alert_message_includes_subtotal(base_table_context, parsed_bill):
    """Alert message must surface the subtotal so staff has spending context."""
    alert_mock = AsyncMock(return_value={"id": 1})
    await _run_bill_action(parsed_bill, base_table_context, alert_mock)

    msg = alert_mock.await_args.kwargs["message"]
    # Subtotal in our fake row is 30000 — _fmt_cop produces formatted COP
    assert "subtotal" in msg.lower()
    # 30000 might render as "$30.000" or "$30,000" depending on locale; check digits
    assert "30" in msg


# ── Resilience: alert failure is non-blocking ────────────────────────────────

@pytest.mark.asyncio
async def test_bill_alert_failure_does_not_block_checkout(base_table_context, parsed_bill):
    """If db_create_waiter_alert raises, the checkout reply still goes out."""
    alert_mock = AsyncMock(side_effect=RuntimeError("waiter_alerts table down"))
    reply = await _run_bill_action(parsed_bill, base_table_context, alert_mock)

    alert_mock.assert_awaited_once()  # was attempted
    assert reply is not None and reply != "", "Reply must still be produced even when alert fails"


# ── Negative case: non-bill action does NOT fire bill alert ──────────────────

@pytest.mark.asyncio
async def test_non_bill_action_does_not_fire_bill_alert(base_table_context):
    """action='waiter' is a salon action but NOT bill — must NOT fire bill alert."""
    alert_mock = AsyncMock(return_value={"id": 1})
    # 'waiter' is a real salon action that handles call_waiter — different code path
    parsed = {"action": "waiter", "reply": "Aviso al mesero", "type": "assistance", "request": "agua"}
    await _run_bill_action(parsed, base_table_context, alert_mock)

    # Either the action took a totally different path (no waiter alert call),
    # OR if it DID call db_create_waiter_alert it must NOT have used type='bill'.
    for call in alert_mock.await_args_list:
        assert call.kwargs.get("alert_type") != "bill", \
            "Non-bill actions must never produce a bill-typed waiter alert"
