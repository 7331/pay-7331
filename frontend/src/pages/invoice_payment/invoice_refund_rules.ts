// Which of an invoice's refunds count, shared by the payer page and the account's activity. No words here, so the
// account pages that import it never carry the payer's language strings (main.tsx).
import type { PublicInvoice } from "@/lib/api_types"

// The invoice's own refunds: those in its asset. A stray coin sent back (INC-001) is not the invoice's money going
// back, so it never makes the invoice "Refunded" or "Refund on its way".
export function listInvoiceAssetRefunds(invoice: PublicInvoice): PublicInvoice["refunds"] {
  return invoice.refunds.filter((refund) => !invoice.asset || !refund.asset_symbol || refund.asset_symbol === invoice.asset.symbol)
}

// An underpaid invoice whose money is going back (the payer's signed request, or the account's refund) stays
// `underpaid` until the watcher closes it, but nothing more should go into it: no price refresh, no "send the rest"
// (2026-10-11). A failed refund does not count; its offer comes back and the payer may ask again.
export function listRefundsUnderway(invoice: PublicInvoice): PublicInvoice["refunds"] {
  if (invoice.status !== "underpaid") return []
  return listInvoiceAssetRefunds(invoice).filter((refund) => refund.status !== "failed")
}

// A cancelled invoice whose own money went back reads "Refunded" rather than "Cancelled".
export function invoiceWasRefunded(invoice: PublicInvoice): boolean {
  return invoice.status === "cancelled" && listInvoiceAssetRefunds(invoice).length > 0
}
