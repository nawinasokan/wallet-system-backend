"""Money is always an int of minor units (cents) in Python and BIGINT in the DB. Never a float.
JSON amounts out are decimal *strings* so no client can lose precision."""
import re

MAX_AMOUNT = 10**15  # comfortably inside BIGINT and every JS safe integer
_DIGITS = re.compile(r"^[0-9]{1,16}$")


def parse_amount(value) -> int:
    """Parse an API amount (positive integer, or a string of digits) into minor units."""
    if isinstance(value, bool):
        raise TypeError("amount must be an integer or digit string")
    if isinstance(value, int):
        s = str(value)
    elif isinstance(value, str):
        s = value
    else:
        # floats (1.5, 1e3) and everything else
        raise TypeError("amount must be an integer or digit string")
    if not _DIGITS.match(s):
        raise ValueError("amount must be an integer number of minor units")
    n = int(s)
    if n <= 0:
        raise ValueError("amount must be positive")
    if n > MAX_AMOUNT:
        raise ValueError("amount too large")
    return n


def m(value: int | None) -> str | None:
    """Serialise money for JSON."""
    return None if value is None else str(value)
