// The payer's two wallet actions, kept on the page (not a screen) so their messages survive the screen changing under
// them: paying the remainder from an injected wallet, and asking for money back by signing. Once the wallet hands back a
// transaction hash, paying again stays locked until the invoice shows progress (its status, payments, received or
// remaining amount change), so a second tap cannot send a second payment; the send screen offers "Send again" only
// after `sentPaymentLockMilliseconds` with nothing seen (2026-10-11: a walkthrough paid twice). The send is kept in
// sessionStorage (`lib/sent_wallet_payment_storage.ts`), so a reload before the invoice moves restores the lock and
// asks the payment check what the chain shows of that hash: on its way keeps the lock, a failed or misdirected send
// clears it with one plain line (2026-10-12).
import { Check, Copy, Undo2 } from "lucide-react"
import { useEffect, useRef, useState, type ReactNode } from "react"

import { copyTextToClipboard } from "@/components/tap_to_copy_button"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"
import { primaryActionButtonClassName, touchTargetButtonClassName } from "@/lib/button_size_class_names"
import { cn } from "@/lib/utils"
import { ApiError, fetchApiJson } from "@/lib/api_client"
import type { PaymentCheckOutcome, PaymentCheckResponse, PublicInvoice } from "@/lib/api_types"
import { describeWalletError, hasBrowserWallet, sendInvoicePaymentFromWallet } from "@/lib/browser_wallet"
import { readPayerStrings } from "@/lib/payer_language"
import {
  forgetSentWalletPayment,
  readSentWalletPayment,
  rememberSentWalletPayment,
  type SentWalletPayment,
} from "@/lib/sent_wallet_payment_storage"
import { publicInvoiceApiPath } from "@/hooks/use_invoice_polling"
import { invoiceIsOnMonero } from "@/pages/invoice_payment/invoice_view_rules"
import { requestRefundWithBrowserWallet } from "@/pages/invoice_payment/refund_request_flow"

export const sentPaymentLockMilliseconds = 120_000

// A restored send's check that gets no answer (offline; the API's 10-a-minute budget for public writes, which a
// payer tapping "I've paid" a few times spends) is asked again after Retry-After, else this long, this many times.
const storedSendCheckAttempts = 4
const storedSendCheckRetrySeconds = 15

interface ActionMessage {
  tone: "info" | "success" | "error"
  text: string
}

// What the chain shows of a restored send that means the money is on its way to this deposit: the lock holds until
// the invoice moves, past the 2 minutes too (sending again would pay twice for sure).
const outcomesWithThePaymentOnItsWay = new Set<PaymentCheckOutcome>(["payment_counted", "payment_confirming"])

// The restored send's one line once the chain answered.
function describeRestoredSendOutcome(outcome: PaymentCheckOutcome, chainName: string): ActionMessage {
  const payerStrings = readPayerStrings()
  switch (outcome) {
    case "payment_counted":
    case "payment_confirming":
      return { tone: "info", text: payerStrings.restoredSendOnItsWay(chainName) }
    case "transaction_not_mined":
      return { tone: "info", text: payerStrings.restoredSendNotMined(chainName) }
    case "transaction_failed":
      return { tone: "error", text: payerStrings.restoredSendFailed(chainName) }
    case "other_address":
      return { tone: "error", text: payerStrings.restoredSendOtherAddress }
    case "transaction_unknown":
      return { tone: "error", text: payerStrings.restoredSendUnknown(chainName) }
    default:
      return { tone: "error", text: payerStrings.restoredSendDidNotPay }
  }
}

// Changes whenever the chain shows the invoice anything new.
function describeInvoiceProgressKey(invoice: PublicInvoice): string {
  return [invoice.status, invoice.amount_received ?? "", invoice.payments.length, invoice.amount_remaining ?? ""].join("|")
}

export interface InvoicePageActions {
  walletIsBusy: boolean
  walletMessage: ActionMessage | null
  sentTransactionHash: string | null
  // When the wallet's last payment was handed over, while the invoice has shown nothing new since; else null.
  sentPaymentAwaitingChainSinceMilliseconds: number | null
  // The payment check found that send on its way to this deposit: no "Send again" while the invoice catches up.
  sentPaymentIsSeenOnChain: boolean
  payWithBrowserWallet(): Promise<void>
  refundMessage: ActionMessage | null
  renderRefundRequestButton(variant: "default" | "secondary"): ReactNode
}

export function useInvoicePageActions(invoice: PublicInvoice | null, applyInvoice: (nextInvoice: PublicInvoice) => void): InvoicePageActions {
  const [walletIsBusy, setWalletIsBusy] = useState(false)
  const walletRequestIsOpen = useRef(false)
  const [walletMessage, setWalletMessage] = useState<ActionMessage | null>(null)
  // undefined until this page load sends or hears back about the stored send; meanwhile the stored one stands, so the
  // first frame after a reload is already locked.
  const [decidedSentWalletPayment, setSentWalletPayment] = useState<SentWalletPayment | null | undefined>(undefined)
  // An XMR invoice has no injected wallet and no refund path (PLAN.md section 14): nothing here touches a wallet for it.
  const invoiceTakesBrowserWallet = invoice !== null && !invoiceIsOnMonero(invoice)
  const storedSentWalletPayment =
    decidedSentWalletPayment === undefined && invoice && invoiceTakesBrowserWallet ? readSentWalletPayment(invoice.id) : null
  const sentWalletPayment = decidedSentWalletPayment === undefined ? storedSentWalletPayment : decidedSentWalletPayment
  const [sentPaymentIsSeenOnChain, setSentPaymentIsSeenOnChain] = useState(false)
  // The invoice whose stored send was already checked on the chain: once per page load, not on every poll.
  const sentPaymentCheckedForInvoiceId = useRef<string | null>(null)
  const storedSendCheckRetryTimer = useRef<number | undefined>(undefined)
  const [refundIsBusy, setRefundIsBusy] = useState(false)
  const [refundMessage, setRefundMessage] = useState<ActionMessage | null>(null)
  const [invoiceLinkWasCopied, setInvoiceLinkWasCopied] = useState(false)
  const invoiceLinkCopiedResetTimer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(invoiceLinkCopiedResetTimer.current), [])
  const payerStrings = readPayerStrings()

  async function payWithBrowserWallet() {
    // The ref, not the state, so two taps inside one frame cannot both reach the wallet.
    if (!invoice || !invoiceTakesBrowserWallet || walletRequestIsOpen.current) return
    walletRequestIsOpen.current = true
    const invoiceProgressKeyWhenSent = describeInvoiceProgressKey(invoice)
    setWalletIsBusy(true)
    setSentWalletPayment(null)
    setSentPaymentIsSeenOnChain(false)
    setWalletMessage({ tone: "info", text: payerStrings.confirmPaymentInWallet })
    try {
      const transactionHash = await sendInvoicePaymentFromWallet(invoice)
      const nextSentWalletPayment = { transactionHash, sentAtMilliseconds: Date.now(), invoiceProgressKeyWhenSent }
      rememberSentWalletPayment(invoice.id, nextSentWalletPayment)
      setSentWalletPayment(nextSentWalletPayment)
      setWalletMessage({ tone: "success", text: payerStrings.sentPageUpdatesItself })
    } catch (walletError) {
      setWalletMessage({ tone: "error", text: describeWalletError(walletError) })
    } finally {
      walletRequestIsOpen.current = false
      setWalletIsBusy(false)
    }
  }

  async function requestRefund() {
    if (!invoice || !invoiceTakesBrowserWallet) return
    setRefundIsBusy(true)
    try {
      const refundResult = await requestRefundWithBrowserWallet(invoice.id, (progressText) => setRefundMessage({ tone: "info", text: progressText }))
      if (refundResult.updatedInvoice) {
        // The refund screen that follows says where the money goes; a message on top would repeat it.
        setRefundMessage(null)
        applyInvoice(refundResult.updatedInvoice)
      } else {
        setRefundMessage({ tone: "error", text: refundResult.refusalText })
      }
    } catch (refundError) {
      setRefundMessage({ tone: "error", text: describeWalletError(refundError) })
    } finally {
      setRefundIsBusy(false)
    }
  }

  async function copyInvoiceLinkForWallet() {
    if (!invoice) return
    try {
      await copyTextToClipboard(invoice.url)
    } catch {
      setRefundMessage({ tone: "error", text: payerStrings.couldntCopyUseAddressBar })
      return
    }
    setInvoiceLinkWasCopied(true)
    window.clearTimeout(invoiceLinkCopiedResetTimer.current)
    invoiceLinkCopiedResetTimer.current = window.setTimeout(() => setInvoiceLinkWasCopied(false), 1600)
  }

  function renderRefundRequestButton(variant: "default" | "secondary"): ReactNode {
    // No wallet in this browser: say how to get to one (the wallet app's own browser) and hand over the link.
    if (!hasBrowserWallet()) {
      return (
        <>
          <p data-testid="invoice_refund_how_to" className="text-center text-sm text-muted-foreground">
            {payerStrings.refundHowToWithoutWallet}
          </p>
          <Button
            id="copy_refund_link_button"
            variant={variant === "default" ? "default" : "secondary"}
            className={cn(variant === "default" ? primaryActionButtonClassName : touchTargetButtonClassName, "w-full")}
            onClick={() => void copyInvoiceLinkForWallet()}
          >
            {invoiceLinkWasCopied ? <Check /> : <Copy />} {invoiceLinkWasCopied ? payerStrings.linkCopied : payerStrings.copyLink}
          </Button>
        </>
      )
    }
    return (
      <Button
        id="refund_request_button"
        variant={variant === "default" ? "default" : "secondary"}
        className={cn(variant === "default" ? primaryActionButtonClassName : touchTargetButtonClassName, "w-full")}
        disabled={refundIsBusy}
        onClick={() => void requestRefund()}
      >
        {refundIsBusy ? <Spinner /> : <Undo2 />}
        {refundIsBusy ? payerStrings.waitingForWallet : payerStrings.getMyMoneyBack}
      </Button>
    )
  }

  const sentPaymentAwaitingChainSinceMilliseconds =
    sentWalletPayment && invoice && describeInvoiceProgressKey(invoice) === sentWalletPayment.invoiceProgressKeyWhenSent
      ? sentWalletPayment.sentAtMilliseconds
      : null
  // A reload before the invoice moved: the stored send already locks the page; the chain says what became of it.
  const storedSendAwaitsCheck = decidedSentWalletPayment === undefined && sentPaymentAwaitingChainSinceMilliseconds !== null
  useEffect(() => {
    if (!invoice || !storedSendAwaitsCheck || sentPaymentCheckedForInvoiceId.current === invoice.id) return
    const storedSendOrNothing = readSentWalletPayment(invoice.id)
    if (!storedSendOrNothing) return
    const storedSend = storedSendOrNothing
    sentPaymentCheckedForInvoiceId.current = invoice.id
    const checkedInvoiceId = invoice.id
    const chainName = invoice.chain.name
    function checkStoredSend(attemptNumber: number) {
      fetchApiJson<PaymentCheckResponse>(publicInvoiceApiPath(checkedInvoiceId, "/payment-checks"), {
        method: "POST",
        body: { transaction_hash: storedSend.transactionHash },
      })
        .then((paymentCheck) => {
          // A send the RPCs don't know yet, within the lock's 2 minutes, is most likely still on its way to them: a
          // reload seconds after sending must not unlock a second payment (2026-10-12). It reads as not mined.
          const sendIsTooRecentToCallUnknown =
            paymentCheck.outcome === "transaction_unknown" &&
            Date.now() - storedSend.sentAtMilliseconds < sentPaymentLockMilliseconds
          const outcomeToShow = sendIsTooRecentToCallUnknown ? "transaction_not_mined" : paymentCheck.outcome
          if (outcomesWithThePaymentOnItsWay.has(outcomeToShow)) {
            setSentPaymentIsSeenOnChain(true)
            setSentWalletPayment(storedSend)
          } else if (outcomeToShow === "transaction_not_mined") {
            setSentWalletPayment(storedSend)
          } else {
            // That send can't pay this invoice: unlock, so the payer can pay again after reading why.
            forgetSentWalletPayment(checkedInvoiceId)
            setSentWalletPayment(null)
          }
          setWalletMessage(describeRestoredSendOutcome(outcomeToShow, chainName))
          applyInvoice(paymentCheck.invoice)
        })
        .catch((checkError) => {
          // No answer (offline, rate limited): the lock and "Checking…" stand while it asks again after the answer's
          // Retry-After; after the last try, the lock and its 2-minute way out stand, as right after a send.
          if (attemptNumber < storedSendCheckAttempts && readSentWalletPayment(checkedInvoiceId)) {
            const retryAfterSeconds = checkError instanceof ApiError ? checkError.retryAfterSeconds : null
            const waitSeconds = Math.min(retryAfterSeconds ?? storedSendCheckRetrySeconds, 60)
            storedSendCheckRetryTimer.current = window.setTimeout(() => checkStoredSend(attemptNumber + 1), waitSeconds * 1000)
            return
          }
          setSentWalletPayment(storedSend)
          setWalletMessage({ tone: "success", text: readPayerStrings().sentPageUpdatesItself })
        })
    }
    checkStoredSend(1)
  }, [invoice, storedSendAwaitsCheck, applyInvoice])
  useEffect(() => () => window.clearTimeout(storedSendCheckRetryTimer.current), [])

  // The invoice moved since the send: the stored send has done its job.
  const sentPaymentShowedProgress = sentWalletPayment !== null && invoice !== null && sentPaymentAwaitingChainSinceMilliseconds === null
  useEffect(() => {
    if (sentPaymentShowedProgress && invoice) forgetSentWalletPayment(invoice.id)
  }, [sentPaymentShowedProgress, invoice])

  return {
    walletIsBusy,
    walletMessage:
      walletMessage ?? (storedSendAwaitsCheck && invoice ? { tone: "info", text: payerStrings.checkingLastPayment(invoice.chain.name) } : null),
    sentTransactionHash: sentWalletPayment?.transactionHash ?? null,
    sentPaymentAwaitingChainSinceMilliseconds,
    sentPaymentIsSeenOnChain: sentPaymentAwaitingChainSinceMilliseconds !== null && sentPaymentIsSeenOnChain,
    payWithBrowserWallet,
    refundMessage,
    renderRefundRequestButton,
  }
}
