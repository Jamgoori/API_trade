"""Small numeric and time helpers shared across the bot.

Prices and sizes are handled as Decimal everywhere an exchange sees them, because
Arcus signs integer ticks/quantums derived from them and a float rounding error
would produce an invalid signature.
"""

from __future__ import annotations

import time
import uuid
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, InvalidOperation

ZERO = Decimal(0)
ONE = Decimal(1)
BPS = Decimal("0.0001")
HOURS_PER_YEAR = Decimal(8760)


def D(value) -> Decimal:
    """Convert to Decimal without going through a binary float representation."""
    if isinstance(value, Decimal):
        return value
    if value is None:
        raise ValueError("cannot convert None to Decimal")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {value!r}") from exc


def quantize_to_step(value: Decimal, step: Decimal, mode: str = "down") -> Decimal:
    """Round value to a multiple of step. mode: down | up | nearest."""
    if step <= 0:
        raise ValueError("step must be positive")
    rounding = {"down": ROUND_FLOOR, "up": ROUND_CEILING, "nearest": ROUND_HALF_EVEN}[mode]
    units = (value / step).to_integral_value(rounding=rounding)
    return (units * step).quantize(step) if units else ZERO


def exact_units(value: Decimal, step: Decimal) -> int:
    """value / step as an int, raising if value is not an exact multiple of step."""
    units = value / step
    if units != units.to_integral_value():
        raise ValueError(f"{value} is not a multiple of {step}")
    return int(units)


def fmt_decimal(value: Decimal) -> str:
    """Plain decimal string without exponent and without trailing zeros (exchanges reject '1E+2')."""
    value = D(value)
    if value == value.to_integral_value():
        return str(value.quantize(ONE))
    return format(value.normalize(), "f")


def bps(a: Decimal, b: Decimal) -> Decimal:
    """(a - b) / b in basis points."""
    if b == 0:
        return ZERO
    return (a - b) / b / BPS


def clamp(value, lo, hi):
    return max(lo, min(hi, value))


def now_s() -> float:
    return time.time()


def now_ms() -> int:
    return int(time.time() * 1000)


def now_us() -> int:
    return time.time_ns() // 1000


def now_ns() -> int:
    return time.time_ns()


def new_client_id(prefix: str = "hb") -> str:
    """Client order id that satisfies Arcus' [A-Za-z0-9_-]{1,36} rule."""
    return f"{prefix}{uuid.uuid4().hex[:24]}"
