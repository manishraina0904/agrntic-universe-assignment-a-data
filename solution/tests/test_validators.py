"""
test_validators.py — Tests for phone, date, decimal and enum validators.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from solution.validators import (
    validate_date,
    validate_decimal,
    validate_enum,
    validate_phone,
)


# ══════════════════════════════════════════════════════════
# Phone
# ══════════════════════════════════════════════════════════

class TestPhone:
    """Test all six valid phone formats and all invalid cases."""

    # -- Valid formats --

    def test_bare_10_digits(self):
        assert validate_phone("9876543210") == "+919876543210"

    def test_plus91_space_format(self):
        assert validate_phone("+91 98765 43210") == "+919876543210"

    def test_leading_zero_11_digits(self):
        assert validate_phone("09876543210") == "+919876543210"

    def test_91_dash_format(self):
        assert validate_phone("91-98765-43210") == "+919876543210"

    def test_bracket_plus91(self):
        assert validate_phone("(+91) 9876543210") == "+919876543210"

    def test_plus91_no_space(self):
        assert validate_phone("+919876543210") == "+919876543210"

    def test_first_digit_6(self):
        assert validate_phone("6123456789") == "+916123456789"

    def test_first_digit_7(self):
        assert validate_phone("7123456789") == "+917123456789"

    def test_first_digit_8(self):
        assert validate_phone("8123456789") == "+918123456789"

    def test_first_digit_9(self):
        assert validate_phone("9123456789") == "+919123456789"

    # -- Invalid formats --

    def test_9_digits_rejected(self):
        with pytest.raises(ValueError):
            validate_phone("987654321")  # only 9 digits

    def test_bad_prefix_5(self):
        with pytest.raises(ValueError):
            validate_phone("5123456789")  # starts with 5

    def test_landline_rejected(self):
        with pytest.raises(ValueError):
            validate_phone("022-2345-6789")

    def test_na_rejected(self):
        with pytest.raises(ValueError):
            validate_phone("N/A")

    def test_us_number_rejected(self):
        with pytest.raises(ValueError):
            validate_phone("+1 415 555 0134")

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            validate_phone("")


# ══════════════════════════════════════════════════════════
# Date
# ══════════════════════════════════════════════════════════

class TestDate:
    """Test all four date formats and impossible dates."""

    def test_yyyy_mm_dd(self):
        assert validate_date("2026-10-15") == "2026-10-15"

    def test_dd_slash_mm_slash_yyyy(self):
        assert validate_date("15/10/2026") == "2026-10-15"

    def test_dd_dash_mm_dash_yyyy(self):
        assert validate_date("15-10-2026") == "2026-10-15"

    def test_dd_mon_yyyy_lower(self):
        assert validate_date("15-Oct-2026") == "2026-10-15"

    def test_dd_mon_yyyy_upper(self):
        assert validate_date("15-OCT-2026") == "2026-10-15"

    def test_dd_mon_yyyy_lower2(self):
        assert validate_date("01-dec-2026") == "2026-12-01"

    def test_impossible_31_feb(self):
        with pytest.raises(ValueError):
            validate_date("31/02/2026")

    def test_impossible_month_13(self):
        with pytest.raises(ValueError):
            validate_date("2026-13-01")

    def test_text_rejected(self):
        with pytest.raises(ValueError):
            validate_date("soon")

    def test_tbd_rejected(self):
        with pytest.raises(ValueError):
            validate_date("TBD")


# ══════════════════════════════════════════════════════════
# Decimal
# ══════════════════════════════════════════════════════════

class TestDecimal:
    """Test all currency prefix formats, commas, edge cases."""

    def test_plain_number(self):
        assert validate_decimal("12000.50") == Decimal("12000.50")

    def test_integer_string(self):
        assert validate_decimal("12000") == Decimal("12000.00")

    def test_rupee_symbol(self):
        assert validate_decimal("₹12,000") == Decimal("12000.00")

    def test_rs_prefix(self):
        assert validate_decimal("Rs 12000") == Decimal("12000.00")

    def test_rs_dot_prefix(self):
        assert validate_decimal("Rs. 12000") == Decimal("12000.00")

    def test_inr_prefix(self):
        assert validate_decimal("INR 12000.5") == Decimal("12000.50")

    def test_inr_uppercase(self):
        assert validate_decimal("INR 38000.1") == Decimal("38000.10")

    def test_comma_thousands(self):
        assert validate_decimal("1,00,000") == Decimal("100000.00")

    def test_rounds_to_2dp(self):
        assert validate_decimal("100.999") == Decimal("101.00")

    def test_zero_accepted(self):
        assert validate_decimal("0") == Decimal("0.00")

    def test_negative_rejected(self):
        with pytest.raises(ValueError):
            validate_decimal("-100")

    def test_text_rejected(self):
        with pytest.raises(ValueError):
            validate_decimal("N/A")

    def test_dash_rejected(self):
        with pytest.raises(ValueError):
            validate_decimal("-")

    def test_word_rejected(self):
        with pytest.raises(ValueError):
            validate_decimal("twelve thousand")


# ══════════════════════════════════════════════════════════
# Enum
# ══════════════════════════════════════════════════════════

class TestEnum:
    LANGS = ["en", "hi", "ta", "kn", "te", "mr"]

    def test_exact_lowercase(self):
        assert validate_enum("en", self.LANGS) == "en"

    def test_uppercase_accepted(self):
        assert validate_enum("EN", self.LANGS) == "en"

    def test_mixed_case(self):
        assert validate_enum("Hi", self.LANGS) == "hi"

    def test_whitespace_stripped(self):
        assert validate_enum(" en ", self.LANGS) == "en"

    def test_invalid_rejected(self):
        with pytest.raises(ValueError):
            validate_enum("Hindi", self.LANGS)

    def test_english_rejected(self):
        with pytest.raises(ValueError):
            validate_enum("english", self.LANGS)

    def test_tamil_rejected(self):
        with pytest.raises(ValueError):
            validate_enum("tamil", self.LANGS)

