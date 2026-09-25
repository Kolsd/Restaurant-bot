"""Bot rule #15: a stretched confirmation ("siii", "vaaaale") still confirms.

This was covered only by an e2e test driven through the WhatsApp delivery
flow, deleted with that channel on 2026-09-25.
"""
from app.services.agent import _last_messages_have_confirmation


def _history(*user_texts: str) -> list:
    out = []
    for text in user_texts:
        out.append({"role": "user", "content": text})
        out.append({"role": "assistant", "content": "¿Confirmas el pedido?"})
    return out


def test_elongated_vowels_confirm():
    for text in ("vaaaaale", "siii", "Siiii perfecto", "dalee"):
        assert _last_messages_have_confirmation(_history("quiero un ajiaco", text)) is True, text


def test_non_confirmation_does_not_confirm():
    for text in ("no", "espera", "¿cuánto cuesta?", "mejor otra cosa"):
        assert _last_messages_have_confirmation(_history("quiero un ajiaco", text)) is False, text


def test_confirmation_in_content_blocks_counts():
    history = [{"role": "user", "content": [{"type": "text", "text": "vaaale"}]}]
    assert _last_messages_have_confirmation(history) is True


def test_only_the_last_two_user_turns_count():
    history = _history("sí", "quiero un ajiaco", "y una limonada")
    assert _last_messages_have_confirmation(history) is False
