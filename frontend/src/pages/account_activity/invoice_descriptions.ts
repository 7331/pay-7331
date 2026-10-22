import {
  convertBaseUnitsToDecimal,
  convertDecimalToBaseUnits,
  formatDecimalForDisplay,
  formatFiatAmount,
  isPlainDecimalString,
  shortenHexString,
} from "@/lib/amount_formatting"
import type { AccountEvent, OperatorAlertAccountEvent, PublicInvoice, StaticDepositAddressAccountEvent } from "@/lib/api_types"
import { formatShortTime } from "@/lib/time_formatting"
import { listRefundsUnderway } from "@/pages/invoice_payment/invoice_refund_rules"

export const accountEventTitles: Record<string, string> = {
  "invoice.payment_detected": "Payment seen",
  // XMR only (PLAN.md section 14): a pool transaction, and a payment the second node operator contradicted.
  "invoice.seen": "Payment seen, waiting for a block",
  "invoice.payment_held": "Payment held up: the nodes disagree. The operator checks it.",
  "invoice.underpaid": "Underpaid",
  "invoice.paid": "Paid",
  "invoice.overpaid": "Overpaid",
  "invoice.expired": "Expired",
  "invoice.cancelled": "Cancelled",
  "invoice.swept": "Sent to your wallet",
  "invoice.sweep_failed": "Sending to your wallet failed",
  "invoice.late_payment": "Late payment",
  "invoice.stray_payment": "Wrong coin received",
  "invoice.refunded": "Refunded",
  // An invoice schedule made its next invoice (PLAN.md section 18): send its link to the payer.
  "schedule.invoice_created": "Repeating invoice made",
  "refund.waiting_for_float": "Refund waiting for float",
  "refund.needs_owner": "Refund needs the operator",
  "account.payout_address_changed": "Payout address changed",
  "account.monero_wallet_set": "Monero wallet set",
  "account.passkey_enrolled_by_link": "Passkey added with a recovery link",
  "account.passkey_added": "Passkey added",
  "account.passkey_deleted": "Passkey deleted",
  "operator.alert": "Needs the operator",
  // A customer's static deposit address (PLAN.md section 17).
  "static_address.payment_received": "Customer paid",
  "static_address.stray_payment": "Customer sent a coin the address doesn't take",
  "static_address.swept": "Customer payment sent to your wallet",
  "static_address.sweep_failed": "Sending a customer payment failed",
}

export type AccountEventTone = "success" | "info" | "warning" | "danger"

export const accountEventTones: Record<string, AccountEventTone> = {
  "invoice.paid": "success",
  "invoice.overpaid": "success",
  "invoice.swept": "success",
  "invoice.refunded": "success",
  "invoice.underpaid": "warning",
  "invoice.late_payment": "warning",
  "invoice.stray_payment": "warning",
  "refund.waiting_for_float": "warning",
  "invoice.sweep_failed": "danger",
  "invoice.payment_held": "danger",
  "refund.needs_owner": "danger",
  // Worth a look every time: a stolen session can't do it, but the owner should see it happened.
  "account.payout_address_changed": "warning",
  // Where XMR links pay, like the payout address.
  "account.monero_wallet_set": "warning",
  // Someone got in without a passkey of this account: the owner should know it happened.
  "account.passkey_enrolled_by_link": "warning",
  // Who can sign in changed; one of the account's passkeys approved it, but the owner should see it happened.
  "account.passkey_added": "warning",
  "account.passkey_deleted": "warning",
  "operator.alert": "danger",
  "static_address.payment_received": "success",
  "static_address.swept": "success",
  "static_address.stray_payment": "warning",
  "static_address.sweep_failed": "danger",
}

export function describeInvoiceAmount(invoice: PublicInvoice): string {
  if (invoice.fiat.currency && invoice.fiat.amount) return formatFiatAmount(invoice.fiat.amount, invoice.fiat.currency)
  if (invoice.amount_due && invoice.asset) return `${formatDecimalForDisplay(invoice.amount_due)} ${invoice.asset.symbol}`
  return "Invoice"
}

const paidInvoiceStatuses = new Set(["paid", "overpaid"])

// What the chain has shown: the final amount plus every payment not final yet (while `confirming` the API's
// `amount_received` is still "0" and the money seen is only in `payments`); null with nothing seen.
export function sumSeenAmount(invoice: PublicInvoice): string | null {
  const decimals = invoice.asset?.decimals
  if (decimals === undefined) return null
  let seenBaseUnits = isPlainDecimalString(invoice.amount_received ?? "") ? convertDecimalToBaseUnits(invoice.amount_received as string, decimals) : 0n
  for (const payment of invoice.payments) {
    if (!payment.final && isPlainDecimalString(payment.amount)) seenBaseUnits += convertDecimalToBaseUnits(payment.amount, decimals)
  }
  return seenBaseUnits > 0n ? convertBaseUnitsToDecimal(seenBaseUnits, decimals) : null
}

// "97.0822 of 97.0822 POL" while money is arriving or short; nothing once paid (the badge says it) or before any
// money is seen. Then the note.
export function describeInvoiceLine(invoice: PublicInvoice): string {
  const lineParts: string[] = []
  const seenAmount = sumSeenAmount(invoice)
  if (seenAmount && invoice.amount_due && invoice.asset && !paidInvoiceStatuses.has(invoice.status)) {
    lineParts.push(`${formatDecimalForDisplay(seenAmount)} of ${formatDecimalForDisplay(invoice.amount_due)} ${invoice.asset.symbol}`)
  }
  if (invoice.note) lineParts.push(invoice.note)
  return lineParts.join(" · ")
}

// What becomes of an invoice's stray money, for the account (2026-10-11: back by itself where the rules allow).
// One that needs the owner (a contract or unknown sender, or a refund that failed) says so first. Empty without any.
export function describeStrayHandlingForAccount(invoice: PublicInvoice): string {
  if (invoice.stray_payments.length === 0) return ""
  const handlings = new Set(invoice.stray_payments.map((strayPayment) => strayPayment.refund_handling))
  const strayAssetSymbols = new Set(invoice.stray_payments.map((strayPayment) => strayPayment.asset_symbol))
  const strayRefunds = invoice.refunds.filter((refund) => refund.asset_symbol !== null && strayAssetSymbols.has(refund.asset_symbol))
  if (handlings.has("owner_settles")) return "It came from a contract, so it can't go back by itself. Settle it with the payer."
  if (strayRefunds.some((refund) => refund.status === "failed" || refund.status === "needs_owner")) {
    return "Sending it back is held up. Settle it with the payer."
  }
  if (handlings.has("automatic")) {
    return strayRefunds.length > 0 && strayRefunds.every((refund) => refund.status === "final")
      ? "Sent back to the wallet it came from."
      : "Going back to the wallet it came from, by itself."
  }
  if (handlings.has("receiver_settles")) return "It went to the receiver with the rest. Settle it with the payer."
  return "Too small to send back; it stays with the receiver."
}

// What an invoice needs from the account now, as one sentence for `/activity`'s "Needs you"; empty when nothing. Read
// from the Invoice resource alone (PLAN.md section 7.1) and only for states that move on, so each item clears itself:
// a sweep that failed until the operator moves it (`sweep_blocked`), a refund that ended unproven (`needs_owner`) until
// the operator settles it, a failed refund while its sender is still offered, and an underpaid invoice past its payment
// window (accept or refund) with no refund of its own coin under way. Money from a contract or an unknown sender, and
// wrong-coin money that can't go back by itself, are settled with the payer outside this app; nothing here would ever
// clear them, so they stay in the invoice's overlay (describeSettleWithPayerLine, describeStrayHandlingForAccount),
// not here.
export function describeInvoiceNeedingAccount(invoice: PublicInvoice, nowMilliseconds: number): string {
  if (invoice.sweep_blocked) return "Sending it to your wallet failed. The operator moves it by hand."
  const ownRefunds = invoice.refunds.filter((refund) => refund.asset_symbol === (invoice.asset?.symbol ?? null))
  if (ownRefunds.some((refund) => refund.status === "needs_owner")) return "A refund didn't prove out. The operator settles it."
  if (ownRefunds.some((refund) => refund.status === "failed") && invoice.refund_offer.available) return "A refund didn't go through. Send it again."
  if (
    invoice.status === "underpaid" &&
    listRefundsUnderway(invoice).length === 0 &&
    invoice.expires_at !== null &&
    new Date(invoice.expires_at).getTime() <= nowMilliseconds
  ) {
    return invoice.refund_offer.available ? "Underpaid. Accept what arrived or refund it." : "Underpaid. Accept what arrived as paid?"
  }
  return ""
}

// Money in the invoice's own coin the owner settles with the payer (contract and unknown senders are settled
// by the owner; PLAN.md section 14: Monero has no refund path at all), for the invoice's overlay; empty
// otherwise.
export function describeSettleWithPayerLine(invoice: PublicInvoice): string {
  const refundableMoneyText = invoice.status === "overpaid" ? "The extra" : "What arrived"
  if (invoice.chain.kind === "monero" && invoice.status === "overpaid") return "The extra is in your Monero wallet; Monero can't send it back by itself. Settle it with the payer."
  if (invoice.refund_offer.reason === "contract_sender") return `${refundableMoneyText} came from a contract, so it can't go back by itself. Settle it with the payer.`
  if (invoice.refund_offer.reason === "unknown_sender") return `${refundableMoneyText} came from a sender we can't tell. Settle it with the payer.`
  return ""
}

// An operator alert's sentence; a reminder says since when the problem is open.
function describeOperatorAlertLine(accountEvent: OperatorAlertAccountEvent): string {
  const { message, is_reminder: isReminder, first_seen_at: firstSeenAt } = accountEvent.data
  if (!isReminder) return message
  return firstSeenAt ? `Still open since ${formatShortTime(firstSeenAt)}. ${message}` : `Still open. ${message}`
}

function isStaticDepositAddressAccountEvent(accountEvent: AccountEvent): accountEvent is StaticDepositAddressAccountEvent {
  return accountEvent.type.startsWith("static_address.")
}

// A static deposit address event's line: the customer, then what arrived (its USD value when credited) or the sweep.
function describeStaticDepositAddressEventLine(accountEvent: StaticDepositAddressAccountEvent): string {
  const { static_deposit_address: staticDepositAddress, payment, sweep } = accountEvent.data
  const lineParts = [staticDepositAddress.customer_reference]
  if (payment) {
    lineParts.push(`${formatDecimalForDisplay(payment.amount)} ${payment.asset_symbol}`)
    if (payment.credited && payment.amount_usd) lineParts.push(formatFiatAmount(payment.amount_usd, "USD"))
    if (payment.uncredited_reason === "wrong_coin") lineParts.push("not counted, sent to your wallet anyway")
  }
  if (sweep?.tx_hash) lineParts.push(shortenHexString(sweep.tx_hash))
  if (accountEvent.type === "static_address.sweep_failed") lineParts.push("The operator moves it by hand.")
  return lineParts.join(" · ")
}

// One line under an event's title: the invoice's amount and line, for the account's own change what changed, for an
// operator alert its sentence. A wrong-coin event leads with what becomes of the money.
export function describeAccountEventLine(accountEvent: AccountEvent): string {
  if (accountEvent.invoice_id === null) {
    if (accountEvent.type === "operator.alert") return describeOperatorAlertLine(accountEvent)
    if (isStaticDepositAddressAccountEvent(accountEvent)) return describeStaticDepositAddressEventLine(accountEvent)
    if (accountEvent.type === "account.monero_wallet_set") return "XMR links pay the new wallet from now on, links made before too. Invoices already made keep the old one."
    if (accountEvent.type === "account.passkey_enrolled_by_link") return "The operator's one-time link added a passkey. Not you? Delete it in Settings."
    if (accountEvent.type === "account.passkey_added") return "One of your passkeys approved it. Not you? Delete it in Settings."
    if (accountEvent.type === "account.passkey_deleted") return "It can't sign in anymore, and its sign-ins ended."
    return `${shortenHexString(accountEvent.data.previous_payout_address)} → ${shortenHexString(accountEvent.data.payout_address)}`
  }
  const strayHandlingText = accountEvent.type === "invoice.stray_payment" ? describeStrayHandlingForAccount(accountEvent.data) : ""
  return [strayHandlingText, describeInvoiceAmount(accountEvent.data), describeInvoiceLine(accountEvent.data)].filter(Boolean).join(" · ")
}

// The invoice an event is about: its own, or the one an operator alert names (often another account's or a signed-out
// invoice, so it opens as the public page).
export function findAccountEventInvoiceId(accountEvent: AccountEvent): string | null {
  if (accountEvent.invoice_id !== null) return accountEvent.invoice_id
  return accountEvent.type === "operator.alert" ? accountEvent.data.invoice_id : null
}

// Where an event leads: its invoice's page, /settings for the account's own change, and /activity itself for an
// operator alert about no invoice and for a customer address's event (its Customers tab).
export function findAccountEventHref(accountEvent: AccountEvent): string {
  const invoiceId = findAccountEventInvoiceId(accountEvent)
  if (invoiceId !== null) return `/i/${encodeURIComponent(invoiceId)}`
  return accountEvent.type === "operator.alert" || isStaticDepositAddressAccountEvent(accountEvent) ? "/activity" : "/settings"
}
