"""
tests/test_agent_action_announcement.py

Regression for CATEGORY A safety net (_is_false_action_announcement /
_ACTION_ANNOUNCEMENT_RE / _FINALIZED_RECAP_RE / _CONFIRMATION_SEEKING_RE in
app/services/agent.py).

Real-LLM run 2026-09-13 (tests/ai_sim + tests/e2e/test_pickup_customer_arrived.py)
showed Claude Haiku presenting a fully-formed order recap ("Listo, vamos con tu
Bandeja Paisa para recoger por Nequi.\n\nResumen:\n• 1x Bandeja Paisa...")
WITHOUT calling create_pickup_order in that same turn. The original
_ACTION_ANNOUNCEMENT_RE only caught "voy a procesar/crear...", "procesando...",
"creando...", etc. — none of which match this phrasing, so the false
confirmation reached the customer untouched and no pickup order was ever
committed (tests/e2e/test_pickup_customer_arrived.py failed:
"No pickup order found for phone=... after 3 turns").

ROUND 2 CORRECTION (post-review): a first pass added "listo,? vamos con" and
"resumen:" directly into _ACTION_ANNOUNCEMENT_RE, unconditionally. That was
too broad: STEP 5 of agent_external.py's system prompt explicitly instructs
the model to "Summarize order, address, payment. Ask explicit confirmation."
— i.e. a LEGITIMATE pre-confirmation recap looks exactly like "Resumen: ...".
Flagging it unconditionally meant CATEGORY A would swap out the recap the
customer needs to read before confirming, replacing it with "Un momento,
déjame verificar los detalles. ¿Confirmas que quieres proceder?" — the
customer never sees what they're supposedly confirming.

The fix separates these two ambiguous patterns into `_FINALIZED_RECAP_RE` and
only treats a match as a false announcement when the reply does NOT also seek
confirmation from the customer (`_CONFIRMATION_SEEKING_RE`: a "?", or an
explicit confirma/correcto/todo bien style phrase). A genuine pre-confirmation
recap always asks something; a false "already done" recap does not.
"""
from app.services.agent import (
    _ACTION_ANNOUNCEMENT_RE,
    _FINALIZED_RECAP_RE,
    _CONFIRMATION_SEEKING_RE,
    _is_false_action_announcement,
)


# ── Round 2: recap + confirmation-question → NOT a false announcement ───────

def test_resumen_with_confirmation_question_not_flagged():
    """Coordinator case (a): a legitimate STEP-5-style recap that ends by
    asking the customer to confirm must NOT be replaced — the customer needs
    to see this text to know what they're confirming."""
    text = "Resumen: 1x Ajiaco, Nequi. ¿Confirmas tu pedido?"
    assert not _is_false_action_announcement(text)


def test_listo_vamos_con_without_question_is_flagged():
    """Coordinator case (b): a finalized-sounding recap with NO question and
    NO tool call is the actual bug — must still be replaced."""
    text = "Listo, vamos con tu pedido de 1x Ajiaco."
    assert _is_false_action_announcement(text)


def test_resumen_colon_with_question_elsewhere_not_flagged():
    """Real transcript shape (pickup_03): the recap and the question can be
    in the same message, separated by other lines — the "?" anywhere in the
    reply is enough to treat it as a genuine pending-confirmation recap."""
    text = (
        "Perfecto. Entonces confirmamos:\n\n"
        "Resumen:\n1 Ajiaco Santafereño - $22,000\nMétodo de pago: Daviplata\n\n"
        "¿Dónde te lo dejamos?"
    )
    assert not _is_false_action_announcement(text)


def test_listo_vamos_con_matches_the_original_bug_transcript():
    """The exact false-confirmation text observed in the real-LLM run — no
    question anywhere, no tool call — must still be caught."""
    text = "Listo, vamos con tu Bandeja Paisa para recoger por Nequi.\n\nResumen:\n- 1x Bandeja Paisa"
    assert _is_false_action_announcement(text)


def test_resumen_without_colon_space_does_not_over_match_unrelated_word():
    """'resumenmente' or similar unrelated tokens must not accidentally match."""
    text = "Te puedo dar un resumenmente rápido de las opciones."
    assert not _FINALIZED_RECAP_RE.search(text)
    assert not _is_false_action_announcement(text)


# ── Building-block regexes (unit-level, in addition to the combined checks) ──

def test_confirmation_seeking_re_matches_question_mark():
    assert _CONFIRMATION_SEEKING_RE.search("¿Todo listo?")


def test_confirmation_seeking_re_matches_confirmas():
    assert _CONFIRMATION_SEEKING_RE.search("Por favor confirma los datos")


def test_confirmation_seeking_re_does_not_match_plain_recap():
    assert not _CONFIRMATION_SEEKING_RE.search("Listo, vamos con tu pedido de 1x Ajiaco.")


# ── Original phrasings (unconditional, no STEP-5 ambiguity) must still match ─

def test_voy_a_procesar_still_matches():
    assert _ACTION_ANNOUNCEMENT_RE.search("Listo, voy a procesar tu pedido ahora mismo.")
    assert _is_false_action_announcement("Listo, voy a procesar tu pedido ahora mismo.")


def test_procesando_still_matches():
    assert _is_false_action_announcement("Procesando tu pedido, un momento.")


def test_creando_still_matches():
    assert _is_false_action_announcement("Creando tu pedido de inmediato.")


def test_voy_a_procesar_with_question_is_still_flagged():
    """UNLIKE the recap patterns, the original "voy a X" family is an
    unconditional signal (the model is claiming an action is IN PROGRESS,
    which is never appropriate phrasing before a tool call regardless of a
    trailing question) — round 2 only narrowed the two NEW patterns."""
    assert _is_false_action_announcement("Voy a procesar tu pedido, ¿confirmas?")


# ── Real server-built receipts must NOT match (no false positive) ───────────

def test_real_salon_receipt_does_not_match():
    """Shape built in agent_salon.py's success path — must never be caught here,
    since CATEGORY A only fires when tool_name is NOT in _ANNOUNCED_ACTION_TOOLS
    (i.e. this text only ever reaches the check when a real tool call happened,
    where it must be a no-op)."""
    receipt = (
        "\U0001f9fe Pedido #ABC123\n"
        "1x Bandeja Paisa\n"
        "Total: $28.000\n"
        "⏱️ Estará listo en ~20 min\n"
        "───────────────\n"
        "¡El equipo ya está en ello! \U0001f468‍\U0001f373"
    )
    assert not _is_false_action_announcement(receipt)


def test_plain_chat_reply_does_not_match():
    assert not _is_false_action_announcement("Claro, tenemos bandeja paisa y ajiaco. ¿Cuál prefieres?")


def test_empty_reply_does_not_match():
    assert not _is_false_action_announcement("")
    assert not _is_false_action_announcement(None)
