"""
tests/test_money.py

Pure-unit tests for app/services/money.py.
No database, no network — just Decimal arithmetic.
"""

import pytest
from decimal import Decimal

from app.services.money import (
    ZERO,
    currency_exponent,
    money_mul,
    money_sum,
    quantize_money,
    to_decimal,
)


# ── currency_exponent ─────────────────────────────────────────────────────────

class TestCurrencyExponent:
    def test_none_returns_2(self):
        assert currency_exponent(None) == 2

    def test_empty_string_returns_2(self):
        assert currency_exponent("") == 2

    def test_usd_returns_2(self):
        assert currency_exponent("USD") == 2

    def test_eur_returns_2(self):
        assert currency_exponent("EUR") == 2

    def test_cop_upper_returns_0(self):
        assert currency_exponent("COP") == 0

    def test_cop_lower_returns_0(self):
        """Case-insensitive: 'cop' should map to 0 decimals."""
        assert currency_exponent("cop") == 0

    def test_clp_returns_0(self):
        assert currency_exponent("CLP") == 0

    def test_jpy_returns_0(self):
        assert currency_exponent("JPY") == 0

    def test_krw_returns_0(self):
        assert currency_exponent("KRW") == 0

    def test_vnd_returns_0(self):
        assert currency_exponent("VND") == 0

    def test_pyg_returns_0(self):
        assert currency_exponent("PYG") == 0

    def test_isk_returns_0(self):
        assert currency_exponent("ISK") == 0


# ── to_decimal ────────────────────────────────────────────────────────────────

class TestToDecimal:
    def test_none_returns_zero(self):
        assert to_decimal(None) == ZERO

    def test_none_custom_default(self):
        assert to_decimal(None, default=Decimal("99")) == Decimal("99")

    def test_decimal_passthrough(self):
        d = Decimal("12.34")
        result = to_decimal(d)
        assert result == d
        assert isinstance(result, Decimal)

    def test_int_zero(self):
        assert to_decimal(0) == ZERO

    def test_int_positive(self):
        assert to_decimal(50000) == Decimal("50000")

    def test_int_negative(self):
        assert to_decimal(-1) == Decimal("-1")

    def test_float_simple(self):
        """0.1 + 0.2 != 0.3 in binary float, but to_decimal goes via str so no noise."""
        result = to_decimal(0.1)
        assert result == Decimal("0.1")

    def test_float_avoids_binary_noise(self):
        """Demonstrate that str-conversion path gives exact representation."""
        # 0.3 in binary float is 0.2999999999999999888... but str(0.3) == '0.3'
        result = to_decimal(0.3)
        assert result == Decimal("0.3")

    def test_str_integer(self):
        assert to_decimal("35000") == Decimal("35000")

    def test_str_decimal(self):
        assert to_decimal("12.50") == Decimal("12.50")

    def test_str_whitespace_stripped(self):
        assert to_decimal("  7500  ") == Decimal("7500")

    def test_str_empty_returns_zero(self):
        assert to_decimal("") == ZERO

    def test_str_empty_custom_default(self):
        assert to_decimal("", default=Decimal("1")) == Decimal("1")

    def test_str_invalid_returns_default(self):
        assert to_decimal("abc") == ZERO

    def test_str_invalid_custom_default(self):
        assert to_decimal("not-a-number", default=Decimal("-1")) == Decimal("-1")

    def test_large_cop_amount(self):
        assert to_decimal("150000000") == Decimal("150000000")


# ── quantize_money ────────────────────────────────────────────────────────────

class TestQuantizeMoney:
    def test_usd_two_decimals(self):
        result = quantize_money(Decimal("12.456"), currency="USD")
        assert result == Decimal("12.46")  # rounds up at .456

    def test_cop_zero_decimals_truncates_half_even(self):
        """Banker's rounding: .5 rounds to nearest even integer."""
        # 2500.5 → nearest even is 2500 (even)
        result = quantize_money(Decimal("2500.5"), currency="COP")
        assert result == Decimal("2500")

    def test_cop_zero_decimals_rounds_up(self):
        # 2501.5 → nearest even is 2502 (even)
        result = quantize_money(Decimal("2501.5"), currency="COP")
        assert result == Decimal("2502")

    def test_cop_plain_integer_unchanged(self):
        result = quantize_money(Decimal("50000"), currency="COP")
        assert result == Decimal("50000")

    def test_none_currency_defaults_to_2_decimals(self):
        result = quantize_money(Decimal("9.999"))
        assert result == Decimal("10.00")

    def test_none_value_returns_zero(self):
        result = quantize_money(None)
        assert result == Decimal("0.00")

    def test_str_input_cop(self):
        result = quantize_money("75000.9", currency="COP")
        assert result == Decimal("75001")

    def test_result_is_decimal(self):
        result = quantize_money(100, currency="USD")
        assert isinstance(result, Decimal)

    def test_clp_zero_decimals(self):
        result = quantize_money("1234.7", currency="CLP")
        assert result == Decimal("1235")

    def test_negative_value_cop(self):
        result = quantize_money("-500.6", currency="COP")
        assert result == Decimal("-501")


# ── money_sum ─────────────────────────────────────────────────────────────────

class TestMoneySum:
    def test_empty_iterable_returns_zero(self):
        assert money_sum([]) == ZERO

    def test_single_value(self):
        assert money_sum([Decimal("100")]) == Decimal("100")

    def test_multiple_decimals(self):
        assert money_sum([Decimal("10"), Decimal("20"), Decimal("30")]) == Decimal("60")

    def test_mixed_types(self):
        """money_sum should handle ints, strings, and Decimals together."""
        result = money_sum([Decimal("10.5"), 20, "30.25"])
        assert result == Decimal("60.75")

    def test_large_cop_values(self):
        amounts = [Decimal("50000"), Decimal("75000"), Decimal("125000")]
        assert money_sum(amounts) == Decimal("250000")

    def test_with_none_values_skipped(self):
        """None converts to ZERO, so it doesn't change the sum."""
        result = money_sum([Decimal("100"), None, Decimal("50")])
        assert result == Decimal("150")

    def test_generator_input(self):
        result = money_sum(x * 1000 for x in range(1, 4))
        assert result == Decimal("6000")

    def test_result_is_decimal(self):
        assert isinstance(money_sum([1, 2, 3]), Decimal)


# ── money_mul ─────────────────────────────────────────────────────────────────

class TestMoneyMul:
    def test_decimal_times_decimal(self):
        assert money_mul(Decimal("10.5"), Decimal("2")) == Decimal("21.0")

    def test_int_times_int(self):
        assert money_mul(3, 4) == Decimal("12")

    def test_none_a_returns_zero(self):
        """None coerces to ZERO, so result is 0 * b = 0."""
        assert money_mul(None, Decimal("100")) == ZERO

    def test_none_b_returns_zero(self):
        assert money_mul(Decimal("100"), None) == ZERO

    def test_percentage_calculation(self):
        """Typical payroll use-case: rate × hours."""
        hourly = Decimal("15000")
        hours = Decimal("8")
        assert money_mul(hourly, hours) == Decimal("120000")

    def test_tip_half_check_boundary(self):
        """Boundary used in tables.py: tip_amount <= money_mul(check_total, 0.5)."""
        check_total = Decimal("60000")
        result = money_mul(check_total, Decimal("0.5"))
        assert result == Decimal("30000.0")

    def test_result_is_decimal(self):
        assert isinstance(money_mul(5, 3), Decimal)

    def test_str_inputs(self):
        assert money_mul("12.5", "4") == Decimal("50.0")
