// Which screen an invoice shows, and the words for its states (PLAN.md sections 4, 6 and 10).
import {
  convertBaseUnitsToDecimal,
  convertDecimalToBaseUnits,
  formatAmountToSendForDisplay,
  formatDecimalForDisplay,
  formatFiatAmount,
  isPlainDecimalString,
  shortenHexString,
} from "@/lib/amount_formatting"
import type { PublicInvoice } from "@/lib/api_types"
import { formatPayerDateTime, formatPayerList } from "@/lib/payer_formatting"
import { readPayerStrings } from "@/lib/payer_language"
import { invoiceWasRefunded, listInvoiceAssetRefunds, listRefundsUnderway } from "@/pages/invoice_payment/invoice_refund_rules"
import { millisecondsUntil } from "@/lib/time_formatting"

export type InvoiceViewKind = "choose_coin" | "send_payment" | "price_lapsed" | "refund_underway" | "confirming" | "paid" | "closed"

function quoteHasLapsed(invoice: PublicInvoice): boolean {
  return Boolean(invoice.fiat.currency && invoice.quote && millisecondsUntil(invoice.quote.expires_at) <= 0)
}

function invoiceIsPayable(invoice: PublicInvoice): boolean {
  if (!invoice.deposit_address || !invoice.asset) return false
  if (invoice.status === "awaiting_payment") return Boolean(invoice.expires_at) && millisecondsUntil(invoice.expires_at as string) > 0
  if (invoice.status === "underpaid") return !quoteHasLapsed(invoice)
  return false
}

// The watcher marks it `expired` on its next tick; until then the API still says `awaiting_asset`, and picking a coin
// would only be refused.
function invoiceExpiredBeforeCoinChosen(invoice: PublicInvoice): boolean {
  return invoice.status === "awaiting_asset" && Boolean(invoice.expires_at) && millisecondsUntil(invoice.expires_at as string) <= 0
}

// An XMR invoice (PLAN.md section 14): no injected wallet, no refund path, no stray money; the payer page says "Seen"
// and counts confirmations instead.
export function invoiceIsOnMonero(invoice: PublicInvoice): boolean {
  return invoice.chain.kind === "monero"
}

// Monero's block target is 120 s (`DIFFICULTY_TARGET_V2` in
// https://github.com/monero-project/monero/blob/release-v0.18/src/cryptonote_config.h).
const moneroBlockTargetMinutes = 2

// A payment on its way to an XMR invoice that is still open for money: one in the pool (`seen`), or a credited top-up
// short of final on an underpaid one. The page shows the confirming screen for it rather than the send screen, so the
// payer does not send twice while it confirms. The watcher keeps the invoice from expiring meanwhile.
function moneroPaymentIsOnItsWay(invoice: PublicInvoice): boolean {
  if (!invoiceIsOnMonero(invoice)) return false
  if (invoice.status !== "awaiting_payment" && invoice.status !== "underpaid") return false
  if (invoice.seen) return true
  return invoice.confirmations !== null && invoice.confirmations_final !== null && invoice.confirmations < invoice.confirmations_final
}

export interface MoneroConfirmationProgress {
  // "Seen" (pool only) or "Confirming 3 of 10".
  title: string
  // The ring's arc: a sliver while only seen, then confirmations over the final count, never full (the paid screen's
  // check is the finish).
  arcFraction: number
  // Null while only seen: the pool row's amount is not on the wire.
  confirmations: number | null
  confirmationsFinal: number | null
}

export function describeMoneroConfirmationProgress(invoice: PublicInvoice): MoneroConfirmationProgress {
  const { confirmations, confirmations_final: confirmationsFinal } = invoice
  const isCredited = confirmations !== null && confirmationsFinal !== null && (invoice.status === "confirming" || confirmations < confirmationsFinal)
  const payerStrings = readPayerStrings()
  if (!isCredited) return { title: payerStrings.statusSeenOnMonero, arcFraction: 0.06, confirmations: null, confirmationsFinal: null }
  const shownConfirmations = Math.min(confirmations, confirmationsFinal)
  return {
    title: payerStrings.statusConfirmingCount(shownConfirmations, confirmationsFinal),
    arcFraction: Math.min(Math.max(shownConfirmations / confirmationsFinal, 0.06), 0.94),
    confirmations: shownConfirmations,
    confirmationsFinal,
  }
}

// The confirming screen's one line on Monero: what is on its way and roughly how long until it counts.
export function describeMoneroConfirmingLine(invoice: PublicInvoice, amountSeenText: string): string {
  const payerStrings = readPayerStrings()
  const progress = describeMoneroConfirmationProgress(invoice)
  if (progress.confirmations === null || progress.confirmationsFinal === null) return payerStrings.moneroInPool(moneroBlockTargetMinutes)
  const blocksLeft = progress.confirmationsFinal - progress.confirmations
  if (blocksLeft <= 0) return payerStrings.moneroFinalAnyMoment(amountSeenText)
  return payerStrings.moneroFinalInMinutes(amountSeenText, blocksLeft * moneroBlockTargetMinutes)
}

export function classifyInvoiceView(invoice: PublicInvoice): InvoiceViewKind {
  if (invoice.status === "awaiting_asset") return invoiceExpiredBeforeCoinChosen(invoice) ? "closed" : "choose_coin"
  if (moneroPaymentIsOnItsWay(invoice)) return "confirming"
  if (listRefundsUnderway(invoice).length > 0) return "refund_underway"
  if (invoiceIsPayable(invoice)) return "send_payment"
  if (invoice.status === "underpaid" && quoteHasLapsed(invoice)) return "price_lapsed"
  if (invoice.status === "confirming") return "confirming"
  if (invoice.status === "paid" || invoice.status === "overpaid") return "paid"
  return "closed"
}

export function describeInvoiceStatusTitle(invoice: PublicInvoice): string {
  if (moneroPaymentIsOnItsWay(invoice)) return describeMoneroConfirmationProgress(invoice).title
  const payerStrings = readPayerStrings()
  switch (invoice.status) {
    case "awaiting_asset":
      return invoiceExpiredBeforeCoinChosen(invoice) ? payerStrings.statusExpired : payerStrings.statusChooseHowToPay
    case "awaiting_payment":
      return invoice.expires_at && millisecondsUntil(invoice.expires_at) <= 0 ? payerStrings.statusTimesUp : payerStrings.statusAwaitingPayment
    case "confirming":
      return invoiceIsOnMonero(invoice) ? describeMoneroConfirmationProgress(invoice).title : payerStrings.statusConfirming
    case "underpaid":
      // A refund that ended unproven (`needs_owner`) or failed moves nothing, so the heading says it is held up.
      if (listRefundsUnderway(invoice).length > 0) return refundIsHeldUp(invoice) ? payerStrings.statusRefundHeldUp : payerStrings.statusRefundOnItsWay
      return quoteHasLapsed(invoice) ? payerStrings.statusPriceExpired : payerStrings.statusUnderpaid
    case "paid":
      return payerStrings.statusPaid
    case "overpaid":
      // The extra gets its own sentence on the paid screen; the heading stays one word.
      return payerStrings.statusPaid
    case "expired":
      return payerStrings.statusExpired
    case "cancelled":
      return invoiceWasRefunded(invoice) ? payerStrings.statusRefunded : payerStrings.statusCancelled
    default:
      return invoice.status
  }
}

// One sentence a payer reads, and the refund on the explorer when the sentence ends on where it went.
export interface OutcomeSentence {
  text: string
  refundTransactionUrl: string | null
}

// What became of the invoice's own refunds, as one outcome: all sent, some still on the way, or one that didn't go
// through (the API leaves out a failed attempt that a later one replaced, so a failure here is one nobody retried).
interface RefundOutcome {
  state: "sent" | "underway" | "stuck"
  amountText: string
  recipientText: string
  refundTransactionUrl: string | null
}

function summarizeRefundOutcome(invoice: PublicInvoice, refunds: PublicInvoice["refunds"]): RefundOutcome | null {
  const decimals = invoice.asset?.decimals
  if (refunds.length === 0 || decimals === undefined) return null
  const refundIsStuck = (refund: PublicInvoice["refunds"][number]) => refund.status === "failed" || refund.status === "needs_owner"
  const sentRefunds = refunds.filter((refund) => refund.status === "final")
  const state = sentRefunds.length === refunds.length ? "sent" : refunds.some((refund) => refund.status !== "final" && !refundIsStuck(refund)) ? "underway" : "stuck"
  let movingBaseUnits = 0n
  for (const refund of refunds) {
    if (!refundIsStuck(refund) && isPlainDecimalString(refund.amount)) movingBaseUnits += convertDecimalToBaseUnits(refund.amount, decimals)
  }
  const recipients = new Set(refunds.flatMap((refund) => (refund.recipient ? [refund.recipient] : [])))
  const onlySentRefund = sentRefunds.length === 1 ? sentRefunds[0] : null
  return {
    state,
    amountText: `${formatDecimalForDisplay(convertBaseUnitsToDecimal(movingBaseUnits, decimals))} ${invoice.asset?.symbol ?? ""}`,
    recipientText: recipients.size === 1 ? shortenHexString([...recipients][0]) : readPayerStrings().walletsThatSentIt,
    refundTransactionUrl:
      state === "sent" && recipients.size === 1 && onlySentRefund?.tx_hash
        ? buildExplorerTransactionUrl(invoice.chain.explorer_url, onlySentRefund.tx_hash)
        : null,
  }
}

// An underpaid invoice whose refunds all stopped short of final ("held up": `needs_owner` may still have moved money,
// so the words never say it didn't go).
function refundIsHeldUp(invoice: PublicInvoice): boolean {
  return summarizeRefundOutcome(invoice, listRefundsUnderway(invoice))?.state === "stuck"
}

// "<what> went back to 0x2c78…Debd" with the refund linked after it, else the sentence with its full stop.
function describeSentRefund(sentence: string, outcome: RefundOutcome): OutcomeSentence {
  return outcome.refundTransactionUrl
    ? { text: sentence, refundTransactionUrl: outcome.refundTransactionUrl }
    : { text: `${sentence}.`, refundTransactionUrl: null }
}

function plainSentence(text: string): OutcomeSentence {
  return { text, refundTransactionUrl: null }
}

export function describeClosedInvoiceLine(invoice: PublicInvoice): OutcomeSentence {
  const payerStrings = readPayerStrings()
  if (invoice.status === "awaiting_payment") return plainSentence(payerStrings.checkingLastSecondPayment)
  if (invoice.status === "awaiting_asset" || (invoice.status === "expired" && invoice.payments.length === 0)) {
    return plainSentence(payerStrings.linkExpiredAskForNew)
  }
  const refundOutcome = invoice.status === "cancelled" ? summarizeRefundOutcome(invoice, listInvoiceAssetRefunds(invoice)) : null
  if (refundOutcome?.state === "sent") {
    return describeSentRefund(payerStrings.yourAmountWentBackTo(refundOutcome.amountText, refundOutcome.recipientText), refundOutcome)
  }
  if (refundOutcome?.state === "underway") {
    return plainSentence(payerStrings.yourAmountGoingBackTo(refundOutcome.amountText, refundOutcome.recipientText))
  }
  if (refundOutcome?.state === "stuck") return plainSentence(payerStrings.refundHeldUpSellerFollowsUp)
  if (invoice.status === "cancelled") return plainSentence(payerStrings.receiverCancelledInvoice)
  return plainSentence(payerStrings.invoiceClosed)
}

// Where the money of a refund underway goes: "Your 20.00 POL is on its way back to 0x2c78…Debd." ("went back" once
// every refund is final, until the watcher closes the invoice as Refunded).
export function describeRefundUnderwayLine(invoice: PublicInvoice): OutcomeSentence {
  const payerStrings = readPayerStrings()
  const refundOutcome = summarizeRefundOutcome(invoice, listRefundsUnderway(invoice))
  if (!refundOutcome) return plainSentence(payerStrings.moneyOnItsWayBack)
  if (refundOutcome.state === "sent") {
    return describeSentRefund(payerStrings.yourAmountWentBackTo(refundOutcome.amountText, refundOutcome.recipientText), refundOutcome)
  }
  // A refund that ended `needs_owner` keeps the invoice open with nothing moving, so no amount "on its way".
  if (refundOutcome.state === "stuck") return plainSentence(payerStrings.refundHeldUpDontSendMore)
  return plainSentence(payerStrings.yourAmountGoingBackDontSendMore(refundOutcome.amountText, refundOutcome.recipientText))
}

// What became of an overpay's extra, in one sentence: "10.50 POL extra went back to 0x6d60…dfb2" (linked), going back
// by itself, or who settles it. Empty unless the invoice is overpaid.
export function describeExtraOutcome(invoice: PublicInvoice): OutcomeSentence | null {
  if (invoice.status !== "overpaid") return null
  const payerStrings = readPayerStrings()
  const refundOutcome = summarizeRefundOutcome(invoice, listInvoiceAssetRefunds(invoice))
  if (refundOutcome?.state === "sent") {
    return describeSentRefund(payerStrings.extraWentBackTo(refundOutcome.amountText, refundOutcome.recipientText), refundOutcome)
  }
  if (refundOutcome?.state === "underway") {
    return plainSentence(payerStrings.extraGoingBackByItself(refundOutcome.amountText, refundOutcome.recipientText))
  }
  if (refundOutcome?.state === "stuck") return plainSentence(payerStrings.extraHeldUp)
  switch (invoice.refund_offer.reason) {
    case "receiver_settles":
    case "unknown_sender":
    case "contract_sender":
      return plainSentence(payerStrings.askSellerForExtra)
    case "refund_in_progress":
      return plainSentence(payerStrings.extraGoesBackByItself)
    case "already_refunded":
      return plainSentence(payerStrings.extraWentBack)
    case "below_minimum":
      return plainSentence(payerStrings.extraTooSmall)
    case "price_unavailable":
      // The watcher sends an overpay's extra back by itself once prices can be read (2026-10-12).
      return plainSentence(payerStrings.extraBackOncePricesReadable)
    default:
      return null
  }
}

// Refunds of stray money: those in a coin the invoice did not ask for (INC-001).
function listStrayRefunds(invoice: PublicInvoice): PublicInvoice["refunds"] {
  const strayAssetSymbols = new Set(invoice.stray_payments.map((strayPayment) => strayPayment.asset_symbol))
  return invoice.refunds.filter((refund) => refund.asset_symbol !== null && strayAssetSymbols.has(refund.asset_symbol))
}

// The words for money in a coin the invoice did not ask for (INC-001, 2026-10-11: it goes back by itself where
// the rules allow): what came, and that it is going or went back to where it came from, or that the seller has it.
// A stray refund that failed or ended `needs_owner` is stuck, so the line then says the seller has it. Empty without
// any.
export function describeStrayPaymentLine(invoice: PublicInvoice): string {
  if (invoice.stray_payments.length === 0) return ""
  const amountTexts = summarizeStrayAmounts(invoice).map(({ assetSymbol, amountText }) => `${formatDecimalForDisplay(amountText)} ${assetSymbol}`)
  const receivedText = formatPayerList(amountTexts)
  const invoiceAssetSymbol = invoice.asset?.symbol ?? null
  const goesBackByItself = invoice.stray_payments.some((strayPayment) => strayPayment.refund_handling === "automatic")
  const strayRefunds = listStrayRefunds(invoice)
  const allStrayRefundsAreStuck =
    strayRefunds.length > 0 && strayRefunds.every((refund) => refund.status === "failed" || refund.status === "needs_owner")
  if (goesBackByItself && !allStrayRefundsAreStuck) {
    const wentBack = strayRefunds.length > 0 && strayRefunds.every((refund) => refund.status === "final")
    return readPayerStrings().strayPaymentLine(receivedText, invoiceAssetSymbol, wentBack ? "was_sent_back" : "being_sent_back")
  }
  return readPayerStrings().strayPaymentLine(receivedText, invoiceAssetSymbol, "goes_to_seller")
}

// Stray money summed per coin, in the order the coins first arrived; amounts stay exact decimal strings.
function summarizeStrayAmounts(invoice: PublicInvoice): { assetSymbol: string; amountText: string }[] {
  const decimalsBySymbol = new Map<string, number>()
  for (const strayPayment of invoice.stray_payments) {
    const placeCount = (strayPayment.amount.split(".")[1] ?? "").length
    decimalsBySymbol.set(strayPayment.asset_symbol, Math.max(decimalsBySymbol.get(strayPayment.asset_symbol) ?? 0, placeCount))
  }
  const baseUnitsBySymbol = new Map<string, bigint>()
  for (const strayPayment of invoice.stray_payments) {
    if (!isPlainDecimalString(strayPayment.amount)) continue
    const decimals = decimalsBySymbol.get(strayPayment.asset_symbol) ?? 0
    baseUnitsBySymbol.set(
      strayPayment.asset_symbol,
      (baseUnitsBySymbol.get(strayPayment.asset_symbol) ?? 0n) + convertDecimalToBaseUnits(strayPayment.amount, decimals),
    )
  }
  return [...baseUnitsBySymbol].map(([assetSymbol, baseUnits]) => ({
    assetSymbol,
    amountText: convertBaseUnitsToDecimal(baseUnits, decimalsBySymbol.get(assetSymbol) ?? 0),
  }))
}


// One line about getting an underpayment back, or nothing.
export function describeUnderpaidRefundLine(invoice: PublicInvoice): string {
  const refundOffer = invoice.refund_offer
  if (refundOffer.available || invoice.status !== "underpaid") return ""
  if (refundOffer.reason === "payment_window_open" && invoice.expires_at) {
    return readPayerStrings().underpaidRefundFrom(formatPayerDateTime(invoice.expires_at))
  }
  // No price, so whether it is big enough to send back is unknown, not "too small" (2026-10-13).
  if (refundOffer.reason === "price_unavailable") return readPayerStrings().underpaidRefundCantBeChecked
  return ""
}

export function describeRefundStatus(refundStatus: string): string {
  const payerStrings = readPayerStrings()
  const refundStatusLabels: Record<string, string> = {
    pending: payerStrings.refundStatusQueued,
    broadcast: payerStrings.refundStatusSending,
    mined: payerStrings.refundStatusConfirming,
    final: payerStrings.refundStatusSent,
    failed: payerStrings.refundStatusFailed,
    needs_owner: payerStrings.refundStatusChecking,
    waiting_for_float: payerStrings.refundStatusQueued,
  }
  return refundStatusLabels[refundStatus] ?? refundStatus
}

export function buildExplorerTransactionUrl(explorerUrl: string, transactionHash: string): string {
  return `${explorerUrl.replace(/\/+$/, "")}/tx/${transactionHash}`
}

export function describeFiatEquivalents(invoice: PublicInvoice): string {
  const fiatParts: string[] = []
  if (invoice.fiat.usd) fiatParts.push(formatFiatAmount(invoice.fiat.usd, "USD"))
  if (invoice.fiat.eur) fiatParts.push(formatFiatAmount(invoice.fiat.eur, "EUR"))
  return fiatParts.join(" · ")
}

// What the chain has shown so far: the final amount plus every payment not final yet. While `confirming` the API's
// `amount_received` is still "0" and the money seen is only in `payments`; with nothing to add, the amount asked.
export function sumReceivedAndPendingAmount(invoice: PublicInvoice): string {
  const decimals = invoice.asset?.decimals
  if (decimals === undefined) return invoice.amount_received ?? "0"
  let totalBaseUnits = isPlainDecimalString(invoice.amount_received ?? "") ? convertDecimalToBaseUnits(invoice.amount_received as string, decimals) : 0n
  for (const payment of invoice.payments) {
    if (!payment.final && isPlainDecimalString(payment.amount)) totalBaseUnits += convertDecimalToBaseUnits(payment.amount, decimals)
  }
  if (totalBaseUnits === 0n && invoice.amount_due) return invoice.amount_due
  return convertBaseUnitsToDecimal(totalBaseUnits, decimals)
}

function convertDecimalToScaledInteger(decimalText: string): { scaledInteger: bigint; scale: number } {
  const fractionLength = (decimalText.trim().split(".")[1] ?? "").length
  return { scaledInteger: convertDecimalToBaseUnits(decimalText, fractionLength), scale: fractionLength }
}

// An asset amount's USD worth at the quote's price, to the nearest cent, in BigInt; null without a usable price.
function estimateUsdValueOfAssetAmount(assetAmount: string | null | undefined, assetPriceUsd: string | null | undefined): string | null {
  if (!assetAmount || !assetPriceUsd || !isPlainDecimalString(assetAmount) || !isPlainDecimalString(assetPriceUsd)) return null
  const amount = convertDecimalToScaledInteger(assetAmount)
  const price = convertDecimalToScaledInteger(assetPriceUsd)
  const product = amount.scaledInteger * price.scaledInteger
  const productScale = amount.scale + price.scale
  const cents = productScale <= 2 ? product * 10n ** BigInt(2 - productScale) : (product + 10n ** BigInt(productScale - 2) / 2n) / 10n ** BigInt(productScale - 2)
  return convertBaseUnitsToDecimal(cents, 2)
}

export function formatEstimatedUsdValue(assetAmount: string | null | undefined, invoice: PublicInvoice): string {
  const usdValue = estimateUsdValueOfAssetAmount(assetAmount, invoice.quote?.asset_price_usd)
  return usdValue ? `≈ ${formatFiatAmount(usdValue, "USD")}` : ""
}

// Pads exact decimal strings to the same number of places ("23.9831" beside "23.98315" reads "23.98310"), so amounts
// shown side by side line up; never drops a digit.
export function padDecimalsToSamePlaces(decimalTexts: string[]): string[] {
  const placeCount = Math.max(2, ...decimalTexts.map((decimalText) => (decimalText.split(".")[1] ?? "").length))
  return decimalTexts.map((decimalText) => {
    if (!isPlainDecimalString(decimalText)) return decimalText
    const [wholePart, fractionPart = ""] = decimalText.split(".")
    return `${wholePart}.${fractionPart.padEnd(placeCount, "0")}`
  })
}

// The invoice's own amount: its fiat amount, else its USD worth, else the crypto amount asked.
export function describeInvoiceAskedAmount(invoice: PublicInvoice): string {
  if (invoice.fiat.currency && invoice.fiat.amount) return formatFiatAmount(invoice.fiat.amount, invoice.fiat.currency)
  if (invoice.fiat.usd) return formatFiatAmount(invoice.fiat.usd, "USD")
  if (invoice.amount_due && invoice.asset) return `${formatAmountToSendForDisplay(invoice.amount_due)} ${invoice.asset.symbol}`
  return ""
}

// The seller's `return_url` (Cryptomus `url_return`, 2026-10-21) while nothing is paid or on its way: the screen's one
// back button. Null once money moved or was sent, so leaving never looks like the way to finish paying.
export function readBackToShopUrlBeforePayment(invoice: PublicInvoice, { paymentWasSent = false } = {}): string | null {
  if (invoice.status !== "awaiting_asset" && invoice.status !== "awaiting_payment") return null
  if (paymentWasSent || invoice.payments.length > 0 || invoice.stray_payments.length > 0) return null
  return invoice.return_url
}
