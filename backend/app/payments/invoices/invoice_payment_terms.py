"""An invoice's payment window and accepted shortfall per offered chain (PLAN.md section 4), resolved once when it is
made and never again, so a later settings change moves no invoice (as the payout address).

Per chain: the maker's value when the request named one (it applies to every chain), else the account's saved default
for that chain, else the server's (``NewInvoice.default_lifetime_seconds``, ``DEFAULT_ACCURACY_PERCENT``). A
signed-out invoice has no account and takes the server's. One chain: its terms are the invoice's. Several chains: the
invoice waits for the payer's pick with the longest window among them (so no chain's window is cut short) and the
first chain's shortfall, which nothing reads before a pick (there is no deposit yet); each chain's terms are kept in
``payment_terms_by_chain``, and the pick takes the chosen chain's (``invoice_asset_choice.py``).
"""

from decimal import Decimal

from pydantic import JsonValue

from app.payments.chains.invoice_payment_terms import DEFAULT_ACCURACY_PERCENT
from app.payments.invoices.models import Invoice
from app.payments.invoices.types import InvoicePaymentTerms, NewInvoice


def resolve_payment_terms_by_chain(
    new_invoice: NewInvoice,
    *,
    chain_keys: list[str],
    saved_lifetime_seconds_by_chain: dict[str, int],
    saved_accuracy_percent_by_chain: dict[str, Decimal],
) -> dict[str, InvoicePaymentTerms]:
    """Each offered chain's terms, in the offer's order. The saved defaults are the account's (empty signed out)."""
    return {
        chain_key: InvoicePaymentTerms(
            lifetime_seconds=new_invoice.lifetime_seconds
            or saved_lifetime_seconds_by_chain.get(chain_key)
            or new_invoice.default_lifetime_seconds,
            accuracy_percent=(
                new_invoice.accuracy_percent
                if new_invoice.accuracy_percent is not None
                else saved_accuracy_percent_by_chain.get(chain_key, DEFAULT_ACCURACY_PERCENT)
            ),
        )
        for chain_key in chain_keys
    }


def find_payment_terms_while_choosing(payment_terms_by_chain: dict[str, InvoicePaymentTerms]) -> InvoicePaymentTerms:
    """The terms a new invoice starts with: its one chain's, or with several the longest window and the first chain's
    shortfall."""
    first_chain_terms = next(iter(payment_terms_by_chain.values()))
    return InvoicePaymentTerms(
        lifetime_seconds=max(terms.lifetime_seconds for terms in payment_terms_by_chain.values()),
        accuracy_percent=first_chain_terms.accuracy_percent,
    )


def present_payment_terms_by_chain(
    payment_terms_by_chain: dict[str, InvoicePaymentTerms],
) -> dict[str, dict[str, JsonValue]] | None:
    """The ``payment_terms_by_chain`` column: kept only when the payer still picks a chain; the shortfall as a decimal
    string, never a float."""
    if len(payment_terms_by_chain) < 2:
        return None
    return {
        chain_key: {"lifetime_seconds": terms.lifetime_seconds, "accuracy_percent": format(terms.accuracy_percent, "f")}
        for chain_key, terms in payment_terms_by_chain.items()
    }


def find_chosen_chain_payment_terms(invoice: Invoice, *, chain_key: str) -> InvoicePaymentTerms:
    """The terms the picked chain was offered with; the invoice's own on a row without per-chain terms (one chain, or
    made before migration 0037)."""
    saved_terms = (invoice.payment_terms_by_chain or {}).get(chain_key)
    if saved_terms is None:
        return InvoicePaymentTerms(lifetime_seconds=invoice.lifetime_seconds, accuracy_percent=invoice.accuracy_percent)
    return InvoicePaymentTerms(
        lifetime_seconds=int(str(saved_terms["lifetime_seconds"])),
        accuracy_percent=Decimal(str(saved_terms["accuracy_percent"])),
    )
