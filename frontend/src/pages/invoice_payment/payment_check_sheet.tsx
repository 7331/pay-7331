// "I've paid" on the send screen: a Drawer on a phone (Dialog from 768 px) that first rereads this
// invoice's deposit on the chain. When the payment shows, it says so and closes, and the page takes the fresh invoice
// through the polling hand-off. When nothing shows, it asks for the transaction hash and says in one sentence what the
// chain shows of it. The check records only what the chain proves; it never counts a payment by itself.
import { ClipboardPaste, X } from "lucide-react"
import { useEffect, useRef, useState, type FormEvent } from "react"

import { ResponsiveSheet } from "@/components/responsive_sheet"
import { RiseInEntrance } from "@/components/rise_in_entrance"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupTextarea } from "@/components/ui/input-group"
import { Spinner } from "@/components/ui/spinner"
import { formatDecimalForDisplay } from "@/lib/amount_formatting"
import { fetchApiJson } from "@/lib/api_client"
import type { PaymentCheckOutcome, PaymentCheckResponse, PublicInvoice } from "@/lib/api_types"
import { describePayerError } from "@/lib/payer_errors"
import { readPayerStrings } from "@/lib/payer_language"
import { readUsualFinalityMinutes } from "@/lib/payment_options"
import { cn } from "@/lib/utils"
import { publicInvoiceApiPath } from "@/hooks/use_invoice_polling"
import { describeStrayPaymentLine, invoiceIsOnMonero } from "@/pages/invoice_payment/invoice_view_rules"
import { primaryActionButtonClassName } from "@/lib/button_size_class_names"

const clipboardCanBeRead = typeof navigator !== "undefined" && typeof navigator.clipboard?.readText === "function"

// Long enough to read one sentence before the sheet closes over the updated page.
const foundPaymentCloseDelayMilliseconds = 2400

// What the chain shows that the payer's money is accounted for: the sheet's job is done.
const outcomesThatFoundThePayment = new Set<PaymentCheckOutcome>([
  "payment_counted",
  "payment_confirming",
  "wrong_coin_recorded",
  "wrong_coin_confirming",
])

type SheetPhase = "rereading_deposit" | "asking_for_hash" | "checking_hash"

interface CheckSentence {
  tone: "found" | "not_found" | "error"
  text: string
}

// "1.00 USDC", or "Your payment" when the check names no amount.
function describeCheckedAmount(paymentCheck: PaymentCheckResponse): string {
  return paymentCheck.amount
    ? `${formatDecimalForDisplay(paymentCheck.amount)} ${paymentCheck.asset_symbol ?? ""}`
    : readPayerStrings().checkYourPaymentFallback
}

// An XMR invoice's outcomes (PLAN.md section 14): the reread looks at the scanner's rows, a txid goes to the sidecar,
// which decodes it with the view key. No wrong coin there (one coin), and a payment takes about 20 minutes to count.
function describeMoneroPaymentCheckOutcome(paymentCheck: PaymentCheckResponse): string {
  const payerStrings = readPayerStrings()
  const amountText = describeCheckedAmount(paymentCheck)
  switch (paymentCheck.outcome) {
    case "payment_counted":
      return payerStrings.checkFoundCounted(amountText)
    case "payment_confirming":
      return payerStrings.checkFoundConfirmingMonero(amountText, paymentCheck.invoice.confirmations_final)
    case "payment_after_close":
      return payerStrings.checkAfterClose(amountText)
    case "other_address":
      return payerStrings.checkOtherAddressMonero
    case "transaction_failed":
      return payerStrings.checkTransactionFailedMonero
    case "transaction_unknown":
      return payerStrings.checkTransactionUnknownMonero
    case "hash_malformed":
      return payerStrings.checkHashMalformedMonero
    case "nothing_found":
      return payerStrings.checkNothingFoundMonero
    default:
      return describeEvmPaymentCheckOutcome(paymentCheck)
  }
}

function describePaymentCheckOutcome(paymentCheck: PaymentCheckResponse): string {
  return invoiceIsOnMonero(paymentCheck.invoice) ? describeMoneroPaymentCheckOutcome(paymentCheck) : describeEvmPaymentCheckOutcome(paymentCheck)
}

// One plain sentence per outcome, in the invoice's own coin and network names.
function describeEvmPaymentCheckOutcome(paymentCheck: PaymentCheckResponse): string {
  const payerStrings = readPayerStrings()
  const invoice = paymentCheck.invoice
  const chainName = invoice.chain.name
  const invoiceSymbol = invoice.asset?.symbol ?? ""
  const amountText = describeCheckedAmount(paymentCheck)
  switch (paymentCheck.outcome) {
    case "payment_counted":
      return payerStrings.checkFoundCounted(amountText)
    case "payment_confirming":
      return payerStrings.checkFoundConfirming(amountText, chainName, readUsualFinalityMinutes(invoice.chain))
    case "payment_after_close":
      return payerStrings.checkAfterClose(amountText)
    case "wrong_coin_recorded":
      return describeStrayPaymentLine(invoice) || payerStrings.checkWrongCoinRecorded(amountText)
    case "wrong_coin_confirming":
      return payerStrings.checkWrongCoinConfirming(amountText, invoiceSymbol, chainName)
    case "unlisted_token":
      return payerStrings.checkUnlistedToken
    case "other_address":
      return payerStrings.checkOtherAddress
    case "transaction_failed":
      return payerStrings.checkTransactionFailed(chainName)
    case "transaction_not_mined":
      return payerStrings.checkTransactionNotMined(chainName)
    case "transaction_unknown":
      return payerStrings.checkTransactionUnknown(chainName)
    case "hash_malformed":
      return payerStrings.checkHashMalformed(chainName)
    case "nothing_found":
      return payerStrings.checkNothingFound(chainName)
  }
}

export function PaymentCheckSheet({
  invoice,
  isOpen,
  onOpenChange,
  onInvoiceChange,
}: {
  invoice: PublicInvoice
  isOpen: boolean
  onOpenChange(nextIsOpen: boolean): void
  onInvoiceChange(nextInvoice: PublicInvoice): void
}) {
  const [sheetPhase, setSheetPhase] = useState<SheetPhase>("rereading_deposit")
  const [checkSentence, setCheckSentence] = useState<CheckSentence | null>(null)
  const [paymentWasFound, setPaymentWasFound] = useState(false)
  const [transactionHashText, setTransactionHashText] = useState("")
  const closeTimer = useRef<number | undefined>(undefined)
  // The latest props, for the reread that starts as the sheet opens.
  const latestInvoice = useRef(invoice)
  const latestCallbacks = useRef({ onOpenChange, onInvoiceChange })
  useEffect(() => {
    latestInvoice.current = invoice
    latestCallbacks.current = { onOpenChange, onInvoiceChange }
  })
  useEffect(() => () => window.clearTimeout(closeTimer.current), [])
  const chainName = invoice.chain.name
  const payerStrings = readPayerStrings()
  // Monero calls it a transaction id (64 hex, no 0x); EVM wallets a transaction hash.
  const isMoneroInvoice = invoiceIsOnMonero(invoice)
  const transactionReferenceName = isMoneroInvoice ? payerStrings.transactionIdLabel : payerStrings.transactionHashLabel

  // Opening the sheet rereads the deposit at once; the page mounts a fresh sheet for every opening (its `key`), so
  // each starts from the spinner with an empty field.
  useEffect(() => {
    if (!isOpen) return
    let isCurrent = true
    fetchApiJson<PaymentCheckResponse>(publicInvoiceApiPath(latestInvoice.current.id, "/payment-checks"), { method: "POST", body: {} })
      .then((paymentCheck) => {
        if (!isCurrent) return
        latestCallbacks.current.onInvoiceChange(paymentCheck.invoice)
        if (outcomesThatFoundThePayment.has(paymentCheck.outcome)) {
          setPaymentWasFound(true)
          setCheckSentence({ tone: "found", text: describePaymentCheckOutcome(paymentCheck) })
          // Off the spinner: the sentence and "Done" show until the sheet closes by itself.
          setSheetPhase("asking_for_hash")
          closeTimer.current = window.setTimeout(() => latestCallbacks.current.onOpenChange(false), foundPaymentCloseDelayMilliseconds)
          return
        }
        setCheckSentence({ tone: "not_found", text: describePaymentCheckOutcome(paymentCheck) })
        setSheetPhase("asking_for_hash")
      })
      .catch((rereadError) => {
        if (!isCurrent) return
        setCheckSentence({ tone: "error", text: describePayerError(rereadError) })
        setSheetPhase("asking_for_hash")
      })
    return () => {
      isCurrent = false
    }
  }, [isOpen])

  async function checkTransactionHash(submitEvent: FormEvent<HTMLFormElement>) {
    submitEvent.preventDefault()
    if (!transactionHashText.trim() || sheetPhase === "checking_hash") return
    setSheetPhase("checking_hash")
    try {
      const paymentCheck = await fetchApiJson<PaymentCheckResponse>(publicInvoiceApiPath(invoice.id, "/payment-checks"), {
        method: "POST",
        body: { transaction_hash: transactionHashText.trim() },
      })
      onInvoiceChange(paymentCheck.invoice)
      const foundThePayment = outcomesThatFoundThePayment.has(paymentCheck.outcome)
      setPaymentWasFound(foundThePayment)
      setCheckSentence({ tone: foundThePayment ? "found" : "not_found", text: describePaymentCheckOutcome(paymentCheck) })
    } catch (checkError) {
      setCheckSentence({ tone: "error", text: describePayerError(checkError) })
    } finally {
      setSheetPhase("asking_for_hash")
    }
  }

  async function pasteTransactionHash() {
    try {
      setTransactionHashText((await navigator.clipboard.readText()).replace(/\s+/g, ""))
    } catch {
      setCheckSentence({ tone: "error", text: payerStrings.clipboardNotAllowed })
    }
  }

  const sentenceLine = checkSentence ? (
    <RiseInEntrance key={checkSentence.text}>
      <Alert
        data-testid="payment_check_message"
        role="status"
        className={cn(
          checkSentence.tone === "found" && "border-success/40 bg-success/10",
          checkSentence.tone === "error" && "border-destructive/40",
        )}
      >
        <AlertDescription className={cn("text-[15px] text-foreground", checkSentence.tone === "error" && "text-destructive")}>
          {checkSentence.text}
        </AlertDescription>
      </Alert>
    </RiseInEntrance>
  ) : null

  return (
    <ResponsiveSheet
      isOpen={isOpen}
      onOpenChange={onOpenChange}
      title={payerStrings.checkYourPayment}
      description={payerStrings.weLookAtChain(chainName)}
    >
      <div data-testid="payment_check_sheet" className="flex flex-col gap-4 pb-1">
        {sheetPhase === "rereading_deposit" ? (
          <p data-testid="payment_check_progress" role="status" className="flex min-h-14 items-center justify-center gap-2 text-[15px] text-muted-foreground">
            <Spinner /> {payerStrings.checkingChain(chainName)}
          </p>
        ) : null}
        {sentenceLine}
        {sheetPhase !== "rereading_deposit" && !paymentWasFound ? (
          <form className="flex flex-col gap-4" onSubmit={(submitEvent) => void checkTransactionHash(submitEvent)}>
            <Field>
              <FieldLabel htmlFor="transaction_hash_input">{transactionReferenceName}</FieldLabel>
              <InputGroup className="min-h-12 rounded-xl">
                <InputGroupTextarea
                  id="transaction_hash_input"
                  rows={1}
                  autoComplete="off"
                  spellCheck={false}
                  autoCapitalize="off"
                  autoCorrect="off"
                  placeholder={payerStrings.examplePlaceholder(isMoneroInvoice ? "9f3c1a…e07b4d" : "0x5c50…a3e1")}
                  value={transactionHashText}
                  disabled={sheetPhase === "checking_hash"}
                  onChange={(changeEvent) => setTransactionHashText(changeEvent.target.value.replace(/\s+/g, ""))}
                  onKeyDown={(keyboardEvent) => {
                    if (keyboardEvent.key !== "Enter" || keyboardEvent.nativeEvent.isComposing) return
                    keyboardEvent.preventDefault()
                    keyboardEvent.currentTarget.form?.requestSubmit()
                  }}
                  className="min-h-0 px-3.5 py-3 font-mono text-[14px] leading-6 break-all placeholder:font-sans placeholder:text-muted-foreground"
                />
                <InputGroupAddon align="inline-end">
                  {transactionHashText ? (
                    <InputGroupButton size="icon-sm" className="size-11 rounded-lg" aria-label={isMoneroInvoice ? payerStrings.clearTransactionId : payerStrings.clearTransactionHash} onClick={() => setTransactionHashText("")}>
                      <X />
                    </InputGroupButton>
                  ) : clipboardCanBeRead ? (
                    <InputGroupButton id="paste_transaction_hash_button" size="sm" className="h-11 rounded-lg px-3" onClick={() => void pasteTransactionHash()}>
                      <ClipboardPaste /> {payerStrings.paste}
                    </InputGroupButton>
                  ) : null}
                </InputGroupAddon>
              </InputGroup>
              <FieldDescription>
                {isMoneroInvoice ? payerStrings.transactionIdHelp : payerStrings.transactionHashHelp}
              </FieldDescription>
            </Field>
            <Button
              id="check_transaction_hash_button"
              type="submit"
              className={cn(primaryActionButtonClassName, "w-full")}
              disabled={!transactionHashText.trim() || sheetPhase === "checking_hash"}
            >
              {sheetPhase === "checking_hash" ? <Spinner /> : null}
              {sheetPhase === "checking_hash" ? payerStrings.checkingChain(chainName) : payerStrings.checkThisTransaction}
            </Button>
          </form>
        ) : null}
        {paymentWasFound && sheetPhase !== "rereading_deposit" ? (
          <Button id="close_payment_check_button" variant="secondary" className={cn(primaryActionButtonClassName, "w-full")} onClick={() => onOpenChange(false)}>
            {payerStrings.done}
          </Button>
        ) : null}
      </div>
    </ResponsiveSheet>
  )
}
