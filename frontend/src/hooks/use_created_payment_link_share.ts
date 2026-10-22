// A quantity link's history entries (2026-10-11). "Create link" on `/` with more than one payer pushes `/l/{id}`
// carrying the same creator share state an invoice gets (use_created_invoice_share.ts), so the maker lands under the
// share overlay. A payer whose tap on a coin made an invoice gets that invoice's id written into the `/l/` entry before
// going on to `/i/{id}`: Back then offers that invoice again instead of the picker, which would take a second slot.
import { useCallback } from "react"
import { useLocation, useNavigate } from "react-router"

import { buildCreatedInvoiceHistoryState } from "@/hooks/use_created_invoice_share"
import type { AccountPaymentLink } from "@/lib/api_types"

interface PaymentLinkTakenInvoiceHistoryState {
  paymentLinkTakenInvoiceId: string
}

function buildPaymentLinkPagePath(paymentLinkId: string): string {
  return `/l/${encodeURIComponent(paymentLinkId)}`
}

export function buildPaymentLinkTakenInvoiceHistoryState(invoiceId: string): PaymentLinkTakenInvoiceHistoryState {
  return { paymentLinkTakenInvoiceId: invoiceId }
}

export function readPaymentLinkTakenInvoiceId(historyState: unknown): string | null {
  if (historyState === null || typeof historyState !== "object" || !("paymentLinkTakenInvoiceId" in historyState)) return null
  return (historyState as PaymentLinkTakenInvoiceHistoryState).paymentLinkTakenInvoiceId
}

// Pushes (not replaces) the link's entry, so Back returns to the form, fresh.
export function useNavigateToCreatedPaymentLink() {
  const navigate = useNavigate()
  const { pathname: madeOnPath } = useLocation()
  return useCallback(
    (createdPaymentLink: AccountPaymentLink, { amountText, noteText }: { amountText: string; noteText: string }) => {
      const paymentLinkPagePath = buildPaymentLinkPagePath(createdPaymentLink.id)
      return navigate(paymentLinkPagePath, {
        state: buildCreatedInvoiceHistoryState({
          linkUrl: createdPaymentLink.url || `${location.origin}${paymentLinkPagePath}`,
          amountText,
          noteText,
          receiverAddress: createdPaymentLink.destination_address,
          acceptedSymbols: createdPaymentLink.assets,
          madeOnPath,
          shareOverlayWasClosed: false,
        }),
      })
    },
    [navigate, madeOnPath],
  )
}
