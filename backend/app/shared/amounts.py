"""Integer base units to and from decimal strings, exactly (PLAN.md section 4: never a float)."""

from decimal import ROUND_CEILING, Decimal, InvalidOperation

from app.shared.errors import InvalidInputError

# Above every real amount and inside every amount column: fiat numeric(20,2), link amounts numeric(38,18). A wire amount
# past it was a 500 from the database (2026-10-09).
MAXIMUM_WIRE_AMOUNT = Decimal(10) ** 15


def format_base_units(base_units: int, *, decimals: int) -> str:
    """``201946000000000000000`` with 18 decimals is ``"201.946"``: no exponent, no trailing zeros."""
    text = format(Decimal(base_units).scaleb(-decimals), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def format_decimal_text(value: Decimal) -> str:
    """``Decimal("1.50")`` is ``"1.5"``: no exponent, no trailing zeros, for prices and rates on the wire."""
    return format(value.normalize(), "f")


def format_base_units_for_reading(base_units: int, *, decimals: int, maximum_decimals: int = 6) -> str:
    """For a sentence a person reads: at most ``maximum_decimals`` places, cut, never rounded up (an alert's
    ``0.006900112536391818`` ETH reads ``0.0069``)."""
    cut_base_units = base_units - base_units % 10 ** max(decimals - maximum_decimals, 0)
    return format_base_units(cut_base_units, decimals=decimals)


def parse_decimal_amount(text: str, *, field_name: str) -> Decimal:
    """A positive decimal from the wire; anything else (an exponent, NaN, a sign) is a 400."""
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise InvalidInputError(f"{field_name}: not a decimal number") from exc
    if not amount.is_finite() or amount <= 0 or "e" in text.lower():
        raise InvalidInputError(f"{field_name}: must be a positive decimal number")
    if amount >= MAXIMUM_WIRE_AMOUNT:
        raise InvalidInputError(f"{field_name}: must be below {MAXIMUM_WIRE_AMOUNT:f}")
    return amount


def to_base_units(amount: Decimal, *, decimals: int, field_name: str) -> int:
    """A decimal amount of an asset in its base units; more decimals than the asset has is a 400."""
    scaled = amount.scaleb(decimals)
    if scaled != scaled.to_integral_value():
        raise InvalidInputError(f"{field_name}: more than {decimals} decimals")
    return int(scaled)


def ceil_to_base_units(amount: Decimal, *, decimals: int, quote_decimals: int) -> int:
    """Rounded up to ``quote_decimals`` places, then in base units: a quote never asks for less than the fiat amount
    and stays short enough to type (20.01 USDC, 56.9030 POL, not 56.902925076750116716)."""
    return int(amount.scaleb(quote_decimals).to_integral_value(rounding=ROUND_CEILING)) * 10 ** (
        decimals - quote_decimals
    )


def to_scientific_text(base_units: int) -> str:
    """EIP-681's number form for a ``value``: ``101946000000000000000`` is ``1.01946e20``."""
    if base_units == 0:
        return "0"
    digits = str(base_units)
    significant = digits.rstrip("0")
    exponent = len(digits) - 1
    mantissa = significant[0] + ("." + significant[1:] if len(significant) > 1 else "")
    return f"{mantissa}e{exponent}"
