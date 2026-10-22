"""An invoice's payment window and accepted shortfall: their bounds and the server's defaults, shared by the invoice,
quantity link and invoice schedule requests and by an account's per-chain defaults (PLAN.md sections 4 and 7). Here,
the lowest domain that both accounts and invoices import, so neither keeps its own copy."""

from decimal import Decimal
from typing import Annotated

from pydantic import Field

# 30 minutes by default; 5 minutes to 12 hours, as Cryptomus.
DEFAULT_INVOICE_LIFETIME_SECONDS = 30 * 60
MINIMUM_INVOICE_LIFETIME_SECONDS = 5 * 60
MAXIMUM_INVOICE_LIFETIME_SECONDS = 12 * 60 * 60
# Paid once the shortfall is at most this share of the amount (Cryptomus `accuracy_payment_percent`); 0 to 5.
DEFAULT_ACCURACY_PERCENT = Decimal("1.0")

InvoiceLifetimeSeconds = Annotated[int, Field(ge=MINIMUM_INVOICE_LIFETIME_SECONDS, le=MAXIMUM_INVOICE_LIFETIME_SECONDS)]
# A JSON number or a decimal string, at most two decimals.
AccuracyPercent = Annotated[Decimal, Field(ge=0, le=5, max_digits=4, decimal_places=2)]


def format_accuracy_percent(accuracy_percent: Decimal) -> str:
    """As the API sends it: `1`, `0.5`, `2.25`."""
    return format(accuracy_percent.normalize(), "f")
