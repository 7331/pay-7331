// The API refusals a payer can meet on `/i/` and `/l/`, as plain sentences in the payer's language. English is
// `describeError` itself (lib/api_client.ts keeps the one English list) except for the few codes whose sentence there
// speaks to the maker; another language maps the codes a payer can get, and anything else (an account-only code, a
// server message with numbers in it) keeps describeError's English rather than a vaguer sentence.
import { ApiError, describeError, measureWaitTime } from "@/lib/api_client"
import { readPayerStrings } from "@/lib/payer_language"
import type { PayerErrorSentences, PayerStrings } from "@/lib/payer_strings/payer_strings_type"

function describeWaitTime(errorSentences: PayerErrorSentences, retryAfterSeconds: number | null): string {
  const waitTime = measureWaitTime(retryAfterSeconds)
  if (waitTime.unit === "seconds") return waitTime.count === 1 ? errorSentences.waitOneSecond : errorSentences.waitSeconds(waitTime.count)
  return waitTime.count === 1 ? errorSentences.waitOneMinute : errorSentences.waitMinutes(waitTime.count)
}

// Codes whose describeError sentence is for the maker ("Set a Monero wallet in Settings first."), in every language.
function describeMakerWordedApiErrorForPayer(payerStrings: PayerStrings, apiError: ApiError): string | null {
  switch (apiError.code) {
    case "too_many_open_invoices":
      return payerStrings.tooManyOpenInvoices
    case "public_invoice_capacity_reached":
      return payerStrings.tooManyPeoplePayingNow
    case "monero_wallet_missing":
    case "monero_lookahead_exhausted":
      return payerStrings.linkCantTakePaymentsNow
    default:
      return null
  }
}

function describePayerApiError(errorSentences: PayerErrorSentences, apiError: ApiError): string | null {
  const serverMessageIncludes = (fragment: string) => apiError.message.includes(fragment)
  switch (apiError.code) {
    case "network_error":
      return errorSentences.cantReachServer
    case "rate_limited":
      return errorSentences.tooManyTries(describeWaitTime(errorSentences, apiError.retryAfterSeconds))
    case "validation_error":
      return errorSentences.requestNotValid
    case "invoice_not_found":
      return errorSentences.invoiceNotFound
    case "invoice_state_conflict":
      if (serverMessageIncludes("already picked")) return errorSentences.coinAlreadyPicked
      if (serverMessageIncludes("expired before a coin")) return errorSentences.expiredBeforeCoinPicked
      if (serverMessageIncludes("was cancelled")) return errorSentences.invoiceWasCancelled
      if (serverMessageIncludes("refund for this invoice is under way")) return errorSentences.refundUnderwayNoMorePayments
      if (serverMessageIncludes("fresh quote")) return errorSentences.cantGetNewPrice
      if (serverMessageIncludes("no payment address")) return errorSentences.pickCoinFirst
      return errorSentences.invoiceChangedReload
    case "payment_link_not_found":
      return errorSentences.paymentLinkNotFound
    case "sold_out":
      return errorSentences.soldOut
    case "payment_link_closed":
      return errorSentences.paymentLinkClosed
    case "refund_not_allowed":
      if (serverMessageIncludes("pays its receiver directly")) return errorSentences.refundReceiverPaidDirectly
      if (serverMessageIncludes("payment window")) return errorSentences.refundOnceTimeToPayIsUp
      if (serverMessageIncludes("already under way")) return errorSentences.refundAlreadyUnderway
      if (serverMessageIncludes("at most")) return null
      return errorSentences.nothingToRefund
    case "refund_offer_not_found":
      return errorSentences.refundOfferGone
    case "refund_offer_used":
      return errorSentences.refundOfferUsed
    case "refund_signature_invalid":
      return errorSentences.refundSignatureNotFromPayer
    case "price_unavailable":
      return errorSentences.pricesUnavailable
    case "service_unavailable":
      return errorSentences.serverCantFinish
    case "factory_not_deployed":
      return errorSentences.paymentsNotSetUp
    case "chain_not_enabled":
      return errorSentences.networkNotAvailable
    case "asset_not_supported":
      return errorSentences.coinNotAvailable
    case "monero_sidecar_unavailable":
      return errorSentences.moneroUnavailable
    case "chain_kind_not_supported":
      return errorSentences.networkCantDoThis
    case "not_found":
      return errorSentences.notHereAnymore
    case "forbidden":
      return errorSentences.notAllowed
    case "conflict":
      return errorSentences.changedInTheMeantime
    case "internal_error":
      return errorSentences.somethingBrokeOnOurSide
    default:
      return null
  }
}

export function describePayerError(error: unknown): string {
  const payerStrings = readPayerStrings()
  if (error instanceof ApiError) {
    const payerSentence = describeMakerWordedApiErrorForPayer(payerStrings, error)
    if (payerSentence) return payerSentence
  }
  const errorSentences = payerStrings.errors
  if (!errorSentences) return describeError(error)
  if (error instanceof ApiError) return describePayerApiError(errorSentences, error) ?? describeError(error)
  if (error instanceof Error && error.message) return error.message
  return errorSentences.somethingWentWrong
}
