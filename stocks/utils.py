import math
from datetime import datetime


def clean_date(value):
    """Convert an Angel API expiry string (e.g. '27-Feb-2025') to a date, or None."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d-%b-%Y").date()
    except (TypeError, ValueError):
        return None


def clean_decimal(value):
    """Convert empty or invalid numeric strings to None."""
    try:
        return float(value) if value not in ("", None) else None
    except (TypeError, ValueError):
        return None


def safe_round(value, ndigits):
    """round() that maps NaN/inf to None so the result is valid JSON."""
    value = float(value)
    if not math.isfinite(value):
        return None
    return round(value, ndigits)
