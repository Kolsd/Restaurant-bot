"""
tests/test_agent_external_pickup_prompt.py

Regression for the pickup funnel prompt fixes (real-LLM run 2026-09-13,
tests/e2e/test_pickup_customer_arrived.py + ai_sim pickup_03/pickup_04):

  - pickup_03: bot asked for a delivery ADDRESS on a PICKUP order ("necesito la
    dirección completa") and never called create_pickup_order. Root cause:
    STEP 5/6 of _SYSTEM_EXTERNAL mentioned "address" unconditionally instead of
    scoping it to delivery only (the CRITICAL RULES section elsewhere already
    scoped it correctly with "(if delivery)", but STEP 5/6 did not, creating
    contradictory guidance for the model).
  - pickup_04: bot repeatedly asked for the customer's NAME before calling
    create_pickup_order, even after "sí confirmo el pedido" — a field that
    does not exist anywhere in the create_pickup_order tool schema
    (app/services/agent_tools.py) or in the prompt; the model invented the
    requirement.
  - test_pickup_customer_arrived_fires_waiter_alert (e2e): bot got stuck
    re-asking for quantity ("una bandeja paisa" not parsed as qty=1) across
    3 fixed turns, so create_pickup_order was never called.

This locks in the added guardrails in app/services/agent_external.py's
_SYSTEM_EXTERNAL prompt: pickup needs ONLY items + payment_method, address is
delivery-only, and quantity words/articles must be parsed instead of re-asked.
"""
from app.services.agent_external import _SYSTEM_EXTERNAL, build_external_prompt


def _prompt_text() -> str:
    """Full external system prompt text (single source block)."""
    blocks = build_external_prompt()
    return " ".join(b["text"] for b in blocks if isinstance(b, dict) and "text" in b)


def test_pickup_never_requires_address():
    text = _prompt_text()
    assert "pickup has no address field" in text or "NEVER mention or ask for address on pickup" in text


def test_pickup_never_requires_customer_name():
    text = _prompt_text()
    assert "NEVER ask the customer for their name" in text or "NEVER ask for the customer's name" in text
    assert "PICKUP MINIMUM DATA" in text


def test_pickup_minimum_data_names_only_items_and_payment():
    text = _prompt_text()
    idx = text.find("PICKUP MINIMUM DATA")
    assert idx != -1
    snippet = text[idx: idx + 300]
    assert "items" in snippet.lower()
    assert "payment_method" in snippet.lower()


def test_quantity_parsing_rule_present():
    text = _prompt_text()
    assert "QUANTITY PARSING" in text
    idx = text.find("QUANTITY PARSING")
    snippet = text[idx: idx + 400]
    # Spanish number words that must be recognized as explicit quantities,
    # plus the "una bandeja paisa" example that reproduces the e2e bug.
    assert "una/uno→1" in snippet or "un/una/uno→1" in snippet
    assert "dos→2" in snippet
    assert "una bandeja paisa" in snippet


def test_step5_scopes_address_to_delivery_only():
    """STEP 5 (CONFIRM) must not tell the model to always summarize 'address' —
    it must be explicitly delivery-only, matching the CRITICAL RULES bullet
    that already said 'address (if delivery)'."""
    text = _prompt_text()
    step5_idx = text.find("STEP 5")
    step6_idx = text.find("STEP 6", step5_idx)  # STEP 1's "carry forward to STEP 6" precedes STEP 5
    assert step5_idx != -1 and step6_idx != -1 and step6_idx > step5_idx
    step5_text = text[step5_idx:step6_idx]
    assert "delivery only" in step5_text.lower()


def test_raw_system_prompt_constant_unchanged_shape():
    """Sanity: the prompt constant is still a non-empty string (guards against
    an edit accidentally truncating/emptying _SYSTEM_EXTERNAL)."""
    assert isinstance(_SYSTEM_EXTERNAL, str)
    assert len(_SYSTEM_EXTERNAL) > 1000
