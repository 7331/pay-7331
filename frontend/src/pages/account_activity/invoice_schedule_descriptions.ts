import { formatDecimalForDisplay, formatFiatAmount } from "@/lib/amount_formatting"
import type { AccountInvoiceSchedule, InvoiceScheduleInterval } from "@/lib/api_types"
import { formatShortTime } from "@/lib/time_formatting"

// The fiat currencies an invoice can be priced in (backend offered_chains.py FIAT_CURRENCIES); any other is a coin.
const fiatCurrencyCodes = new Set(["USD", "EUR"])

const invoiceScheduleIntervalLabels: Record<InvoiceScheduleInterval, string> = {
  daily: "Daily",
  weekly: "Weekly",
  monthly: "Monthly",
}

export function describeInvoiceScheduleAmount(invoiceSchedule: AccountInvoiceSchedule): string {
  if (fiatCurrencyCodes.has(invoiceSchedule.currency)) return formatFiatAmount(invoiceSchedule.amount, invoiceSchedule.currency)
  return `${formatDecimalForDisplay(invoiceSchedule.amount)} ${invoiceSchedule.currency}`
}

// "Monthly · next Nov 20", "Weekly · 3 of 12 made", "Daily · paused"; the end reached reads "12 made".
export function describeInvoiceScheduleTiming(invoiceSchedule: AccountInvoiceSchedule): string {
  const intervalText = invoiceScheduleIntervalLabels[invoiceSchedule.interval] ?? invoiceSchedule.interval
  const madeText =
    invoiceSchedule.ends_after_invoice_count === null
      ? `${invoiceSchedule.created_invoice_count} made`
      : `${invoiceSchedule.created_invoice_count} of ${invoiceSchedule.ends_after_invoice_count} made`
  const nextText = invoiceSchedule.status === "active" && invoiceSchedule.next_period_start ? `next ${formatShortTime(invoiceSchedule.next_period_start)}` : ""
  return [intervalText, nextText, madeText].filter(Boolean).join(" · ")
}

// Why the last try made no invoice, in words; the watcher tries again on its own.
export function describeInvoiceScheduleFailure(invoiceSchedule: AccountInvoiceSchedule): string {
  if (!invoiceSchedule.last_failure_code) return ""
  if (invoiceSchedule.last_failure_code === "price_unavailable") return "The last invoice waits for prices to come back. It's retried on its own."
  if (invoiceSchedule.last_failure_code === "order_id_taken") return "Another invoice already has this period's order id, so none was made for it."
  if (invoiceSchedule.last_failure_code === "period_already_billed") return "This period's invoice was already made, so the try was skipped."
  return `The last invoice couldn't be made (${invoiceSchedule.last_failure_code.replaceAll("_", " ")}). It's retried on its own.`
}
