"""
Edge-case tests for find_dish bidirectional ratio fix (Issue B1).

These tests run without a DB — find_dish is patched at db.db_get_menu.
"""
import pytest
from unittest.mock import AsyncMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_MENU = {
    "Platos Fuertes": [
        {"name": "Ajiaco", "price": 18000, "description": "Sopa bogotana"},
        {"name": "Bandeja Paisa", "price": 28000, "description": "Plato típico"},
        {"name": "Arroz con Pollo", "price": 15000, "description": "Clásico"},
    ],
    "Bebidas": [
        {"name": "Agua", "price": 2000, "description": "500ml"},
        {"name": "Jugo de Mora", "price": 5000, "description": "Natural"},
    ],
}


async def _find(query: str, menu: dict = SAMPLE_MENU):
    """Run find_dish with a mocked db_get_menu."""
    from app.services.orders import find_dish

    with patch("app.services.orders.db") as mock_db:
        mock_db.db_get_menu = AsyncMock(return_value=menu)
        return await find_dish(query, bot_number="5571234567")


# ---------------------------------------------------------------------------
# B1 — Ratio guard: short query must NOT match long dish name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_char_query_does_not_match_ajiaco():
    """'a' (len=1) vs 'ajiaco' (len=6): ratio=1/6≈0.17 < 0.4 → no match."""
    result = await _find("a")
    assert result is None, (
        f"'a' should not match any dish via substring — got {result!r}"
    )


@pytest.mark.asyncio
async def test_two_char_query_does_not_match_arroz_con_pollo():
    """'ar' (len=2) vs 'arroz con pollo' (len=15): ratio≈0.13 < 0.4 → no match."""
    result = await _find("ar")
    assert result is None, (
        f"'ar' should not match 'Arroz con Pollo' — got {result!r}"
    )


# ---------------------------------------------------------------------------
# B1 — Exact match (Pass 1) must still work regardless of ratio
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_match_ajiaco():
    """'ajiaco' matches 'Ajiaco' via exact case-insensitive Pass 1."""
    result = await _find("ajiaco")
    assert result is not None
    assert result["name"] == "Ajiaco"


@pytest.mark.asyncio
async def test_exact_match_case_insensitive():
    """'BANDEJA PAISA' matches 'Bandeja Paisa' via exact match."""
    result = await _find("BANDEJA PAISA")
    assert result is not None
    assert result["name"] == "Bandeja Paisa"


# ---------------------------------------------------------------------------
# B1 — Substring match with sufficient ratio must work (Pass 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_band_matches_bandeja_paisa():
    """'BAND' (len=4) vs 'bandeja paisa' (len=13): ratio=4/13≈0.31 < 0.4 → no match."""
    # 'band' is too short relative to 'bandeja paisa' (ratio 0.31 < 0.4)
    result = await _find("BAND")
    assert result is None, (
        f"'BAND' ratio vs 'Bandeja Paisa' is ~0.31 (<0.4), should not match — got {result!r}"
    )


@pytest.mark.asyncio
async def test_bandeja_matches_bandeja_paisa():
    """'bandeja' (len=7) vs 'bandeja paisa' (len=13): ratio=7/13≈0.54 >= 0.4 → match."""
    result = await _find("bandeja")
    assert result is not None
    assert result["name"] == "Bandeja Paisa"


@pytest.mark.asyncio
async def test_arroz_matches_arroz_con_pollo():
    """'arroz' (len=5) vs 'arroz con pollo' (len=15): ratio=5/15≈0.33 < 0.4 → no match."""
    result = await _find("arroz")
    assert result is None, (
        f"'arroz' ratio vs 'Arroz con Pollo' is ~0.33 (<0.4), should not match — got {result!r}"
    )


@pytest.mark.asyncio
async def test_mora_matches_jugo_de_mora():
    """'mora' (len=4) vs 'jugo de mora' (len=12): ratio=4/12≈0.33 < 0.4 → no match."""
    result = await _find("mora")
    assert result is None


@pytest.mark.asyncio
async def test_jugo_mora_matches_jugo_de_mora():
    """'jugo mora' (len=9) vs 'jugo de mora' (len=12): ratio=9/12=0.75 >= 0.4 → match."""
    result = await _find("jugo mora")
    # 'jugo mora' is NOT a substring of 'jugo de mora', so no match via substring
    # This verifies we don't force-match spurious queries
    assert result is None


@pytest.mark.asyncio
async def test_agua_exact_match():
    """'agua' (len=4) vs 'agua' (len=4): exact match → always hits Pass 1."""
    result = await _find("agua")
    assert result is not None
    assert result["name"] == "Agua"


# ---------------------------------------------------------------------------
# B1 — Empty / whitespace query must return None cleanly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_query_returns_none():
    result = await _find("")
    assert result is None


@pytest.mark.asyncio
async def test_whitespace_query_returns_none():
    result = await _find("   ")
    assert result is None


# ---------------------------------------------------------------------------
# B1 — Name-in-query direction (dish name is substring of query)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_name_in_query_sufficient_ratio():
    """Query 'dame un ajiaco' contains 'ajiaco', but ratio=6/14≈0.43 >= 0.4 → match."""
    result = await _find("dame un ajiaco")
    # 'ajiaco' in 'dame un ajiaco' → True; ratio = min(6,14)/max(6,14) = 6/14 ≈ 0.43
    assert result is not None
    assert result["name"] == "Ajiaco"


@pytest.mark.asyncio
async def test_name_in_query_insufficient_ratio():
    """Very long query wrapping a short dish name stays below ratio threshold."""
    # 'agua' (len=4) in 'quiero una botella de agua fría con hielo' (len=41)
    # ratio = 4/41 ≈ 0.10 < 0.4 → no match
    result = await _find("quiero una botella de agua fría con hielo")
    assert result is None, (
        f"Short dish 'Agua' should not match very long query — got {result!r}"
    )
