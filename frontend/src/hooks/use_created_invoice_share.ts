// What the creating tab carries from `/` to the invoice it just made: "Create link" navigates to
// `/i/{id}` with this in the history entry's state (react-router keeps it in `history.state`), so the address bar is
// the link, a reload of that entry keeps the creator's share overlay and icon, and anyone else opening the link gets
// the plain payer page. Nothing goes to localStorage or sessionStorage.
import { useCallback } from "react"
import { useLocation, useNavigate } from "react-router"

import type { PublicInvoice } from "@/lib/api_types"

export interface CreatedInvoiceShare {
  linkUrl: string
  amountText: string
  noteText: string
  // Where the money goes and the coins the payer may use, shown on the overlay so a wrong paste is caught before
  // sharing; "Make another" carries the receiver back to the form (2026-10-11).
  receiverAddress: string
  acceptedSymbols: string[]
  // The form it came from (`/`), where "Make another" goes.
  madeOnPath: string
  // Set once the creator closes the overlay, so a reload shows the payer view with the share icon, not the overlay.
  shareOverlayWasClosed: boolean
}

interface CreatedInvoiceHistoryState {
  createdInvoiceShare: CreatedInvoiceShare
}

export function buildInvoicePagePath(invoiceId: string): string {
  return `/i/${encodeURIComponent(invoiceId)}`
}

export function readCreatedInvoiceShare(historyState: unknown): CreatedInvoiceShare | null {
  if (historyState === null || typeof historyState !== "object" || !("createdInvoiceShare" in historyState)) return null
  return (historyState as CreatedInvoiceHistoryState).createdInvoiceShare
}

export function buildCreatedInvoiceHistoryState(createdInvoiceShare: CreatedInvoiceShare): CreatedInvoiceHistoryState {
  return { createdInvoiceShare }
}

// "Make another" goes back to the form with the receiver in that entry's state, so the next link pays the same
// address; a plain visit or Back gets a fresh form.
interface MakeAnotherInvoiceHistoryState {
  receiverAddressForNextInvoice: string
}

export function buildMakeAnotherInvoiceHistoryState(receiverAddress: string): MakeAnotherInvoiceHistoryState {
  return { receiverAddressForNextInvoice: receiverAddress }
}

export function readReceiverAddressForNextInvoice(historyState: unknown): string | null {
  if (historyState === null || typeof historyState !== "object" || !("receiverAddressForNextInvoice" in historyState)) return null
  const { receiverAddressForNextInvoice } = historyState as MakeAnotherInvoiceHistoryState
  return typeof receiverAddressForNextInvoice === "string" && receiverAddressForNextInvoice ? receiverAddressForNextInvoice : null
}

// Pushes (not replaces) the invoice's entry, so Back returns to the form, fresh.
export function useNavigateToCreatedInvoice() {
  const navigate = useNavigate()
  const { pathname: madeOnPath } = useLocation()
  return useCallback(
    (
      createdInvoice: PublicInvoice,
      { amountText, noteText, receiverAddress }: { amountText: string; noteText: string; receiverAddress: string },
    ) => {
      const invoicePagePath = buildInvoicePagePath(createdInvoice.id)
      const createdInvoiceShare: CreatedInvoiceShare = {
        linkUrl: createdInvoice.url || `${location.origin}${invoicePagePath}`,
        amountText,
        noteText,
        receiverAddress,
        acceptedSymbols: createdInvoice.assets,
        madeOnPath,
        shareOverlayWasClosed: false,
      }
      return navigate(invoicePagePath, { state: buildCreatedInvoiceHistoryState(createdInvoiceShare) })
    },
    [navigate, madeOnPath],
  )
}
