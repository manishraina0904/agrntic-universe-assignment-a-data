"""
validators.py — Pure validation and normalisation functions.

Each function takes a raw string value and returns a clean value,
or raises ValueError with a plain reason string on failure.
No database or IO side-effects.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


# ── Phone ──────────────────────────────────────────────────────────────────

def validate_phone(raw: str) -> str:
    """
    Normalise and validate an Indian mobile phone number.

    Rules (from spec):
      1. Strip every non-digit character.
      2. If 12 digits and starts with '91', drop the leading '91'.
      3. If 11 digits and starts with '0', drop the leading '0'.
      4. Must be exactly 10 digits.
      5. First digit must be 6, 7, 8 or 9.

    Returns the canonical form: '+91XXXXXXXXXX'.
    Raises ValueError if the number is invalid.
    """
    digits = re.sub(r"\D", "", raw)

    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    if len(digits) != 10:
        raise ValueError("invalid_phone")
    if digits[0] not in "6789":
        raise ValueError("invalid_phone")

    return "+91" + digits


# ── Date ───────────────────────────────────────────────────────────────────

# Month abbreviations for DD-Mon-YYYY format
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def validate_date(raw: str) -> str:
    """
    Parse a date string in one of four formats and return YYYY-MM-DD.

    Accepted formats:
      YYYY-MM-DD
      DD/MM/YYYY
      DD-MM-YYYY
      DD-Mon-YYYY  (e.g. 01-Dec-2026; month name is case-insensitive)

    Raises ValueError for unrecognised formats or impossible dates (e.g. 31 Feb).
    """
    import datetime

    s = raw.strip()

    def _make(y: int, m: int, d: int) -> str:
        # datetime constructor validates the date; raises ValueError on impossible dates
        dt = datetime.date(y, m, d)
        return dt.isoformat()

    # YYYY-MM-DD
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return _make(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # DD/MM/YYYY
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return _make(int(m.group(3)), int(m.group(2)), int(m.group(1)))

    # DD-MM-YYYY  (must check that the middle part is digits, not a month name)
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})-(\d{4})", s)
    if m:
        return _make(int(m.group(3)), int(m.group(2)), int(m.group(1)))

    # DD-Mon-YYYY
    m = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", s)
    if m:
        mon = _MONTHS.get(m.group(2).lower())
        if mon is None:
            raise ValueError(f"unknown month abbreviation: {m.group(2)}")
        return _make(int(m.group(3)), mon, int(m.group(1)))

    raise ValueError(f"unrecognised date format: {s!r}")


# ── Decimal / currency ─────────────────────────────────────────────────────

# Matches optional currency prefix (₹, Rs, Rs., INR) with optional space
_CURRENCY_PREFIX = re.compile(
    r"^(₹|rs\.?|inr)\s*",
    re.IGNORECASE,
)


def validate_decimal(raw: str) -> Decimal:
    """
    Parse a decimal / currency string and return a Decimal rounded to 2 places.

    Accepted prefixes (case-insensitive, optional space after): ₹  Rs  Rs.  INR
    Commas are treated as thousands separators and removed.
    Value must be ≥ 0.

    Raises ValueError on invalid input.
    """
    s = raw.strip()
    s = _CURRENCY_PREFIX.sub("", s)   # strip currency prefix
    s = s.replace(",", "")            # remove thousands separators
    s = s.strip()

    try:
        value = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"invalid decimal: {raw!r}")

    if value < 0:
        raise ValueError(f"decimal must be >= 0, got {value}")

    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ── Enum ───────────────────────────────────────────────────────────────────

def validate_enum(raw: str, allowed_values: list[str]) -> str:
    """
    Match raw (case-insensitively) against allowed_values.
    Returns the matching value in lowercase.
    Raises ValueError if no match.
    """
    normalised = raw.strip().lower()
    for v in allowed_values:
        if v.lower() == normalised:
            return v.lower()
    raise ValueError(f"invalid enum value: {raw!r}")

