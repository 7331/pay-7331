// What the invoice page knows about its viewer having made the invoice: the creating tab (its history entry says so)
// or a signed-in account reading its own invoice. Then the top bar says "This is what your payer sees" and the one-way
// coin commit stays disabled; the creating tab also gets a share icon back to its overlay. Payers get the default.
import { createContext, useContext } from "react"

export interface InvoiceMakerView {
  viewerMadeThisInvoice: boolean
  reopenCreatorShare: (() => void) | null
}

export const InvoiceMakerViewContext = createContext<InvoiceMakerView>({ viewerMadeThisInvoice: false, reopenCreatorShare: null })

export function useInvoiceMakerView(): InvoiceMakerView {
  return useContext(InvoiceMakerViewContext)
}
