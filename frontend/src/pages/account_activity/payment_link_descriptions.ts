import { formatFiatAmount } from "@/lib/amount_formatting"
import type { AccountPaymentLink } from "@/lib/api_types"

export function describePaymentLinkAmount(paymentLink: AccountPaymentLink): string {
  return formatFiatAmount(paymentLink.fiat.amount, paymentLink.fiat.currency ?? "USD")
}

// "Sold 3 of 10", plus how many hold a slot while they pay ("· 1 paying now").
export function describePaymentLinkSales(paymentLink: AccountPaymentLink): string {
  const payingCount = paymentLink.slots_held - paymentLink.paid_count
  return `Sold ${paymentLink.paid_count} of ${paymentLink.quantity}${payingCount > 0 ? ` · ${payingCount} paying now` : ""}`
}
