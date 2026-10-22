// The calm screens after sending: confirming, paid, refund on its way, closed, and a lapsed price for the rest. A
// settled screen is one hero, one heading, one muted line (the amount and the note) and at most one sentence on what
// became of the money; the refund attempts behind it stay on the account's /activity.
import { ArrowUpRight, Clock, Eye, RefreshCw, Store, Undo2, X } from "lucide-react"
import { useState } from "react"

import { ConfirmingProgressRing } from "@/components/confirming_progress_ring"
import { PageColumn } from "@/components/page_column"
import { RollingNumberText } from "@/components/rolling_number_text"
import { SuccessCheckMark } from "@/components/success_check_mark"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"
import { formatDecimalForDisplay } from "@/lib/amount_formatting"
import { fetchApiJson } from "@/lib/api_client"
import type { PublicInvoice } from "@/lib/api_types"
import { describePayerError } from "@/lib/payer_errors"
import { readPayerStrings } from "@/lib/payer_language"
import { readUsualFinalityMinutes } from "@/lib/payment_options"
import { publicInvoiceApiPath } from "@/hooks/use_invoice_polling"
import type { InvoicePageActions } from "@/pages/invoice_payment/invoice_page_actions"
import { invoiceWasRefunded } from "@/pages/invoice_payment/invoice_refund_rules"
import {
  InvoiceContextLine,
  InvoiceFlagLines,
  InvoiceNote,
  InvoiceRefundRows,
  InvoiceStatusHeading,
  InvoiceTopBar,
  OutcomeSentenceLine,
  SellerSupportLine,
} from "@/pages/invoice_payment/invoice_screen_parts"
import {
  buildExplorerTransactionUrl,
  describeClosedInvoiceLine,
  describeExtraOutcome,
  describeInvoiceAskedAmount,
  describeInvoiceStatusTitle,
  describeMoneroConfirmationProgress,
  describeMoneroConfirmingLine,
  describeRefundUnderwayLine,
  invoiceIsOnMonero,
  describeUnderpaidRefundLine,
  sumReceivedAndPendingAmount,
} from "@/pages/invoice_payment/invoice_view_rules"
import { primaryActionButtonClassName } from "@/lib/button_size_class_names"
import { cn } from "@/lib/utils"

function RefundMessageLine({ pageActions }: { pageActions: InvoicePageActions }) {
  if (!pageActions.refundMessage) return null
  return (
    <p data-testid="invoice_refund_message" role="status" className={pageActions.refundMessage.tone === "error" ? "text-center text-sm break-words text-destructive" : "text-center text-sm break-words text-muted-foreground"}>
      {pageActions.refundMessage.text}
    </p>
  )
}

export function InvoiceConfirmingScreen({ invoice }: { invoice: PublicInvoice }) {
  const assetSymbol = invoice.asset?.symbol ?? ""
  const amountSeen = sumReceivedAndPendingAmount(invoice)
  if (invoiceIsOnMonero(invoice)) return <MoneroConfirmingScreen invoice={invoice} amountSeen={amountSeen} />
  // From the chain's own `expected_finality_seconds`: a few seconds on Polygon, about 15 minutes on Arbitrum.
  const usualFinalityMinutes = readUsualFinalityMinutes(invoice.chain)
  const payerStrings = readPayerStrings()
  return (
    <PageColumn topBar={<InvoiceTopBar />}>
      <section className="flex flex-1 flex-col items-center justify-center gap-6 pb-16 text-center">
        <ConfirmingProgressRing>
          <span className="font-mono text-lg font-medium tabular-nums" aria-hidden="true">
            <RollingNumberText decimalText={amountSeen} formatDisplayText={formatDecimalForDisplay} />
            <span className="block text-xs text-muted-foreground">{assetSymbol}</span>
          </span>
        </ConfirmingProgressRing>
        <div>
          <InvoiceStatusHeading title={payerStrings.statusConfirming} />
          <p data-testid="invoice_confirming_amount" className="mt-2 text-[15px] text-muted-foreground">
            {payerStrings.confirmingReceived(`${formatDecimalForDisplay(amountSeen)} ${assetSymbol}`)}{" "}
            {usualFinalityMinutes === null ? payerStrings.finalInAFewSeconds : payerStrings.usuallyFinalWithinMinutes(usualFinalityMinutes)}
          </p>
          <InvoiceNote invoice={invoice} className="mt-1" />
        </div>
      </section>
      <InvoiceFlagLines invoice={invoice} />
      {invoice.stray_payments.length > 0 ? <SellerSupportLine invoice={invoice} /> : null}
    </PageColumn>
  )
}

// An XMR payment on its way (PLAN.md section 14): "Seen" while it sits in the pool (its amount is not on the wire, so
// the ring holds an eye), then "Confirming 3 of 10" with the arc that far round and the amount credited so far. About
// 20 minutes in all, so the line says roughly how long is left and the seller's contact stays in reach.
function MoneroConfirmingScreen({ invoice, amountSeen }: { invoice: PublicInvoice; amountSeen: string }) {
  const assetSymbol = invoice.asset?.symbol ?? ""
  const confirmationProgress = describeMoneroConfirmationProgress(invoice)
  const isOnlySeen = confirmationProgress.confirmations === null
  return (
    <PageColumn topBar={<InvoiceTopBar />}>
      <section className="flex flex-1 flex-col items-center justify-center gap-6 pb-16 text-center">
        <ConfirmingProgressRing arcFraction={confirmationProgress.arcFraction}>
          {isOnlySeen ? (
            <Eye className="size-9 text-muted-foreground" aria-hidden="true" />
          ) : (
            <span className="font-mono text-lg font-medium tabular-nums" aria-hidden="true">
              <RollingNumberText decimalText={amountSeen} formatDisplayText={formatDecimalForDisplay} />
              <span className="block text-xs text-muted-foreground">{assetSymbol}</span>
            </span>
          )}
        </ConfirmingProgressRing>
        <div>
          <InvoiceStatusHeading title={confirmationProgress.title} />
          <p data-testid="invoice_confirming_amount" className="mt-2 text-[15px] text-muted-foreground">
            {describeMoneroConfirmingLine(invoice, `${formatDecimalForDisplay(amountSeen)} ${assetSymbol}`)}
          </p>
          <InvoiceNote invoice={invoice} className="mt-1" />
        </div>
      </section>
      <SellerSupportLine invoice={invoice} />
    </PageColumn>
  )
}

// Paid (an overpay too): what arrived and the note, one sentence on the extra or on a partial the receiver accepted,
// and one quiet link to the payment on the explorer. "Back to shop" goes to the seller's `success_url`, else its
// `return_url` (2026-10-21).
export function InvoicePaidScreen({ invoice, pageActions }: { invoice: PublicInvoice; pageActions: InvoicePageActions }) {
  const latestPayment = invoice.payments.at(-1)
  const extraOutcome = describeExtraOutcome(invoice)
  const shopUrlAfterPayment = invoice.success_url || invoice.return_url
  const payerStrings = readPayerStrings()
  return (
    <PageColumn
      topBar={<InvoiceTopBar />}
      footer={
        shopUrlAfterPayment || invoice.refund_offer.available ? (
          <>
            <RefundMessageLine pageActions={pageActions} />
            {invoice.refund_offer.available ? pageActions.renderRefundRequestButton(shopUrlAfterPayment ? "secondary" : "default") : null}
            {shopUrlAfterPayment ? (
              <Button id="return_to_shop_link" asChild className={cn(primaryActionButtonClassName, "w-full")}>
                <a href={shopUrlAfterPayment} rel="noopener">
                  <Store /> {payerStrings.backToShop}
                </a>
              </Button>
            ) : null}
          </>
        ) : undefined
      }
    >
      <section className="flex flex-1 flex-col items-center justify-center gap-5 pb-8 text-center">
        <SuccessCheckMark sizeClassName="size-24" />
        <div>
          <InvoiceStatusHeading title={describeInvoiceStatusTitle(invoice)} />
          <InvoiceContextLine
            invoice={invoice}
            amountText={invoice.amount_received ? `${formatDecimalForDisplay(invoice.amount_received)} ${invoice.asset?.symbol ?? ""}` : ""}
            amountTestId="invoice_received_amount"
            testId="invoice_paid_amount_line"
          />
          {invoice.partial ? <p className="mt-3 text-[15px]">{payerStrings.sellerAcceptedWhatArrived}</p> : null}
          {extraOutcome ? <OutcomeSentenceLine sentence={extraOutcome} testId="invoice_refund_note" /> : null}
        </div>
        {latestPayment?.tx_hash ? (
          <a
            data-testid="invoice_transaction_link"
            href={buildExplorerTransactionUrl(invoice.chain.explorer_url, latestPayment.tx_hash)}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex min-h-11 items-center gap-1 px-2 text-[15px] text-muted-foreground underline underline-offset-4 hover:text-foreground"
          >
            {payerStrings.viewPayment} <ArrowUpRight className="size-4" />
          </a>
        ) : null}
        <InvoiceFlagLines invoice={invoice} />
      </section>
      <SellerSupportLine invoice={invoice} />
    </PageColumn>
  )
}

// An underpaid invoice whose money is going back: no "Refresh price", no "send the rest", only where the money goes,
// until the watcher closes it as Refunded.
export function InvoiceRefundUnderwayScreen({ invoice }: { invoice: PublicInvoice }) {
  return (
    <PageColumn topBar={<InvoiceTopBar />}>
      <section className="flex flex-1 flex-col items-center justify-center gap-5 pb-12 text-center">
        <div className="grid size-20 place-items-center rounded-full bg-muted text-muted-foreground" aria-hidden="true">
          <Undo2 className="size-9" />
        </div>
        <div>
          <InvoiceStatusHeading title={describeInvoiceStatusTitle(invoice)} />
          <InvoiceContextLine invoice={invoice} amountText={describeInvoiceAskedAmount(invoice)} />
          <OutcomeSentenceLine sentence={describeRefundUnderwayLine(invoice)} testId="invoice_refund_underway_line" />
        </div>
        <InvoiceFlagLines invoice={invoice} />
      </section>
      <SellerSupportLine invoice={invoice} />
    </PageColumn>
  )
}

export function InvoiceClosedScreen({ invoice, pageActions }: { invoice: PublicInvoice; pageActions: InvoicePageActions }) {
  const statusTitle = describeInvoiceStatusTitle(invoice)
  const isRefunded = invoiceWasRefunded(invoice)
  return (
    <PageColumn
      topBar={<InvoiceTopBar />}
      footer={
        pageActions.refundMessage || invoice.refund_offer.available ? (
          <>
            <RefundMessageLine pageActions={pageActions} />
            {invoice.refund_offer.available ? pageActions.renderRefundRequestButton("default") : null}
          </>
        ) : undefined
      }
    >
      <section className="flex flex-1 flex-col items-center justify-center gap-5 pb-12 text-center">
        {isRefunded ? (
          <SuccessCheckMark sizeClassName="size-20" tone="muted" />
        ) : (
          <div className="grid size-20 place-items-center rounded-full bg-muted text-muted-foreground" aria-hidden="true">
            {invoice.status === "cancelled" ? <X className="size-9" /> : <Clock className="size-9" />}
          </div>
        )}
        <div>
          <InvoiceStatusHeading title={statusTitle} />
          <InvoiceContextLine invoice={invoice} amountText={describeInvoiceAskedAmount(invoice)} />
          <OutcomeSentenceLine sentence={describeClosedInvoiceLine(invoice)} testId="invoice_closed_line" />
        </div>
        <InvoiceFlagLines invoice={invoice} />
      </section>
      <SellerSupportLine invoice={invoice} />
    </PageColumn>
  )
}

// What happens to an underpaid amount whose price ran out, from the API's refund offer (PLAN.md sections 4 and 6).
function describePriceLapsedNextStep(invoice: PublicInvoice): string {
  const payerStrings = readPayerStrings()
  if (invoice.refund_offer.available) return payerStrings.priceLapsedNextStepRefreshOrGetBack
  if (invoice.refund_offer.reason === "receiver_settles") return payerStrings.priceLapsedNextStepRefreshOrAskSeller
  return payerStrings.priceLapsedNextStepRefresh
}

export function InvoicePriceLapsedScreen({
  invoice,
  pageActions,
  onInvoiceChange,
}: {
  invoice: PublicInvoice
  pageActions: InvoicePageActions
  onInvoiceChange(nextInvoice: PublicInvoice): void
}) {
  const assetSymbol = invoice.asset?.symbol ?? ""
  const askedAmount = describeInvoiceAskedAmount(invoice)
  const refundLine = describeUnderpaidRefundLine(invoice)
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [errorMessage, setErrorMessage] = useState("")
  const payerStrings = readPayerStrings()
  async function refreshQuote() {
    setIsRefreshing(true)
    setErrorMessage("")
    try {
      onInvoiceChange(await fetchApiJson<PublicInvoice>(publicInvoiceApiPath(invoice.id, "/quote"), { method: "POST" }))
    } catch (refreshError) {
      setErrorMessage(describePayerError(refreshError))
    } finally {
      setIsRefreshing(false)
    }
  }
  return (
    <PageColumn
      topBar={<InvoiceTopBar />}
      footer={
        <>
          {errorMessage ? (
            <Alert variant="destructive">
              <AlertDescription>{errorMessage}</AlertDescription>
            </Alert>
          ) : null}
          <RefundMessageLine pageActions={pageActions} />
          <Button id="refresh_price_button" className={cn(primaryActionButtonClassName, "w-full")} disabled={isRefreshing} onClick={() => void refreshQuote()}>
            {isRefreshing ? <Spinner /> : <RefreshCw />} {payerStrings.refreshPrice}
          </Button>
          {invoice.refund_offer.available ? pageActions.renderRefundRequestButton("secondary") : null}
        </>
      }
    >
      <section className="flex flex-1 flex-col items-center justify-center gap-3 pb-12 text-center">
        <div className="grid size-20 place-items-center rounded-full bg-muted text-muted-foreground" aria-hidden="true">
          <Clock className="size-9" />
        </div>
        <div>
          <InvoiceStatusHeading title={payerStrings.statusPriceExpired} />
          {askedAmount ? <div className="mt-1 text-[17px] text-muted-foreground tabular-nums">{askedAmount}</div> : null}
          <InvoiceNote invoice={invoice} className="mt-1" />
        </div>
        <p data-testid="invoice_price_lapsed_line" className="text-[15px]">
          {payerStrings.priceLapsedLine(formatDecimalForDisplay(invoice.amount_received), assetSymbol)} {describePriceLapsedNextStep(invoice)}
        </p>
        {refundLine ? <p data-testid="invoice_refund_note" className="text-[13px] text-muted-foreground">{refundLine}</p> : null}
        <InvoiceRefundRows invoice={invoice} />
        <InvoiceFlagLines invoice={invoice} />
      </section>
      <SellerSupportLine invoice={invoice} />
    </PageColumn>
  )
}
