"""
tests/test_agent_confirm_words.py

Locks down the behavior introduced in commit feae298:
  - _CONFIRM_WORDS must keep all expanded entries (vale, bueno, claro,
    correcto, bien, excelente, genial, sii/siii/siiii) plus the original
    set (sí, ok, dale, perfecto, listo, …).
  - _last_messages_have_confirmation must collapse repeated vowels
    BEFORE matching, so 'vaaaaale', 'siiii', 'buenoooo' all hit.
  - Existing tokens (sí, ok, dale, perfecto) must still match.
  - Negative cases (no confirmation in window) must return False.

Each entry exists because a real WhatsApp user typed it and the bot
ignored their confirmation. CLAUDE.md Regla #15 mandates these stay.
"""
from app.services.agent import _CONFIRM_WORDS, _last_messages_have_confirmation


# ── _CONFIRM_WORDS membership ────────────────────────────────────────────────

def test_confirm_words_keeps_classic_set():
    """Original set members from before commit feae298 must remain."""
    classic = {"sí", "si", "sip", "sipo", "dale", "ok", "okay", "va",
               "perfecto", "confirmo", "confirmar", "listo", "procede",
               "manda", "adelante", "yes", "yep", "sure"}
    missing = classic - _CONFIRM_WORDS
    assert not missing, f"Classic confirm tokens disappeared: {missing}"


def test_confirm_words_includes_new_spanish_variants():
    """Words added in commit feae298 must be present."""
    new = {"vale", "bueno", "claro", "correcto", "bien", "excelente", "genial"}
    missing = new - _CONFIRM_WORDS
    assert not missing, f"New Spanish confirm tokens missing: {missing}"


def test_confirm_words_includes_elongated_si_variants():
    """Direct hits for 'sii'/'siii'/'siiii' (in case normalization fails)."""
    elongated = {"sii", "siii", "siiii"}
    missing = elongated - _CONFIRM_WORDS
    assert not missing, f"Elongated 'si' variants missing: {missing}"


# ── _last_messages_have_confirmation positive cases ──────────────────────────

def _user_turn(text: str) -> dict:
    return {"role": "user", "content": text}


def test_classic_si_matches():
    assert _last_messages_have_confirmation([_user_turn("sí")]) is True


def test_new_word_vale_matches():
    assert _last_messages_have_confirmation([_user_turn("vale")]) is True


def test_new_word_bueno_matches():
    assert _last_messages_have_confirmation([_user_turn("bueno, dale")]) is True


def test_new_word_claro_matches():
    assert _last_messages_have_confirmation([_user_turn("claro!")]) is True


def test_new_word_genial_matches():
    assert _last_messages_have_confirmation([_user_turn("genial")]) is True


# ── Vowel elongation collapsing ──────────────────────────────────────────────

def test_elongated_vale_matches_via_collapse():
    """'vaaaale' must normalize to 'vale' and match."""
    assert _last_messages_have_confirmation([_user_turn("vaaaale")]) is True


def test_extreme_elongated_vale_matches():
    """'vaaaaaaaaaale' (many vowels) still matches."""
    assert _last_messages_have_confirmation([_user_turn("vaaaaaaaaaale")]) is True


def test_elongated_si_variants_match():
    """Elongated 'siiiiii' collapses to 'si' (which is in _CONFIRM_WORDS)."""
    assert _last_messages_have_confirmation([_user_turn("siiiiii claro")]) is True


def test_elongated_bueno_matches():
    """'buenoooo' collapses to 'bueno'."""
    assert _last_messages_have_confirmation([_user_turn("buenoooo, listo")]) is True


def test_uppercase_with_elongation_matches():
    """Case-insensitive + elongation collapse must combine."""
    assert _last_messages_have_confirmation([_user_turn("VAAAALE!")]) is True


# ── Negative cases ───────────────────────────────────────────────────────────

def test_no_confirmation_returns_false():
    assert _last_messages_have_confirmation([_user_turn("hola, qué tienen?")]) is False


def test_empty_history_returns_false():
    assert _last_messages_have_confirmation([]) is False


def test_only_assistant_turns_returns_false():
    """_last_messages_have_confirmation only inspects user turns."""
    history = [
        {"role": "assistant", "content": "vale, perfecto, dale"},
        {"role": "assistant", "content": "sí, claro"},
    ]
    assert _last_messages_have_confirmation(history) is False


def test_partial_word_does_not_match():
    """'valido' contains 'val' but should NOT match 'vale'."""
    assert _last_messages_have_confirmation([_user_turn("eso no es valido")]) is False


# ── Window semantics (only last 2 user turns) ────────────────────────────────

def test_only_last_two_user_turns_inspected():
    """Confirmation 3 turns ago must NOT count."""
    history = [
        _user_turn("vale"),                  # turn -3 (ignored)
        _user_turn("hola"),                  # turn -2
        _user_turn("qué hay?"),              # turn -1
    ]
    assert _last_messages_have_confirmation(history) is False


def test_confirmation_in_second_to_last_turn_counts():
    history = [
        _user_turn("perfecto"),              # turn -2 (counts)
        _user_turn("una pregunta más"),      # turn -1
    ]
    assert _last_messages_have_confirmation(history) is True


# ── Content-block format (Anthropic SDK) ─────────────────────────────────────

def test_content_blocks_format_supported():
    """When content is a list of blocks (tool_use API style), text is concatenated."""
    history = [{
        "role": "user",
        "content": [{"type": "text", "text": "dale, confirmo"}],
    }]
    assert _last_messages_have_confirmation(history) is True
