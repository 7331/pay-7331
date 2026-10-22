// One of the account's invoices in an overlay: what it is, the link, and what the account may do with it (PLAN.md
// section 7): copy or share the link while a payer can still pay, cancel while nothing arrived, accept an underpayment
// as paid, or refund a sender the API offers, each refund and the cancel after a confirm. Money in a coin the invoice
// did not ask for shows as its own block with what becomes of it; it goes back by itself where the rules
// allow, so there is nothing to tap. A bottom Drawer on a phone, a
// Dialog from 768 px like the account sheet (shadcn's responsive pattern, https://ui.shadcn.com/docs/components/drawer).
import { Ban, Check, CheckCheck, CircleAlert, Copy, ExternalLink, Share, Undo2 } from "lucide-react"
import { useEffect, useRef, useState, type ReactNode } from "react"
import { toast } from "sonner"

import { OneLineLinkText } from "@/components/one_line_link_text"
import { copyTextToClipboard } from "@/components/tap_to_copy_button"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Drawer, DrawerContent, DrawerDescription, DrawerHeader, DrawerTitle } from "@/components/ui/drawer"
import { Separator } from "@/components/ui/separator"
import { Spinner } from "@/components/ui/spinner"
import { useMediaQuery } from "@/hooks/use_media_query"
import { formatDecimalForDisplay, shortenHexString } from "@/lib/amount_formatting"
import { describeError, fetchApiJson } from "@/lib/api_client"
import type { AccountInvoice } from "@/lib/api_types"
import { formatLocalDateTime } from "@/lib/time_formatting"
import { cn } from "@/lib/utils"
import {
  describeInvoiceAmount,
  describeSettleWithPayerLine,
  describeStrayHandlingForAccount,
  sumSeenAmount,
} from "@/pages/account_activity/invoice_descriptions"
import { InvoiceStatusBadge } from "@/pages/account_activity/invoice_status_badge"
import { listRefundsUnderway } from "@/pages/invoice_payment/invoice_refund_rules"
import { primaryActionButtonClassName, touchTargetButtonClassName } from "@/lib/button_size_class_names"

// shadcn's `md` breakpoint, as in the account sheet.
const wideScreenMediaQuery = "(min-width: 768px)"
// Nothing paid yet: sharing the link is the job, so Share is the primary.
const unpaidInvoiceStatuses = new Set(["awaiting_asset", "awaiting_payment"])
// A payer can still pay into these, so the link is worth sharing; a closed invoice's link only shows its end.
const shareableInvoiceStatuses = new Set(["awaiting_asset", "awaiting_payment", "underpaid"])

export function InvoiceActionsOverlay({
  invoice,
  onClose,
  onInvoiceChange,
}: {
  invoice: AccountInvoice | null
  onClose(): void
  onInvoiceChange(nextInvoice: AccountInvoice): void
}) {
  const isWideScreen = useMediaQuery(wideScreenMediaQuery)
  const changeOpen = (isOpen: boolean) => {
    if (!isOpen) onClose()
  }
  const titleText = invoice ? describeInvoiceAmount(invoice) : ""
  const descriptionText = invoice?.note || "No note"
  const titleRow = (title: ReactNode) => (
    <div className="flex w-full items-center justify-between gap-3">
      {title}
      {invoice ? <InvoiceStatusBadge invoiceStatus={invoice.status} paymentIsSeen={invoice.seen} /> : null}
    </div>
  )
  const actions = invoice ? <InvoiceActions key={invoice.id} invoice={invoice} onInvoiceChange={onInvoiceChange} /> : null

  if (isWideScreen) {
    return (
      <Dialog open={invoice !== null} onOpenChange={changeOpen}>
        <DialogContent data-testid="invoice_actions_overlay" className="max-h-[90dvh] gap-0 overflow-y-auto rounded-3xl p-6 sm:max-w-[440px]">
          <DialogHeader className="pb-4 text-left">
            {titleRow(<DialogTitle className="text-3xl font-semibold tracking-tight tabular-nums">{titleText}</DialogTitle>)}
            <DialogDescription className="text-[15px]">{descriptionText}</DialogDescription>
          </DialogHeader>
          {actions}
        </DialogContent>
      </Dialog>
    )
  }
  return (
    <Drawer open={invoice !== null} onOpenChange={changeOpen}>
      <DrawerContent data-testid="invoice_actions_overlay">
        <div className="mx-auto w-full max-w-[440px] overflow-y-auto px-5 pb-[max(env(safe-area-inset-bottom),1.25rem)]">
          <DrawerHeader className="items-start px-0 pt-5 text-left md:text-left">
            {titleRow(<DrawerTitle className="text-3xl font-semibold tracking-tight tabular-nums">{titleText}</DrawerTitle>)}
            <DrawerDescription className="text-[15px]">{descriptionText}</DrawerDescription>
          </DrawerHeader>
          {actions}
        </div>
      </DrawerContent>
    </Drawer>
  )
}

function InvoiceActions({ invoice, onInvoiceChange }: { invoice: AccountInvoice; onInvoiceChange(nextInvoice: AccountInvoice): void }) {
  const [busyAction, setBusyAction] = useState("")
  const [linkWasCopied, setLinkWasCopied] = useState(false)
  const copiedResetTimer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(copiedResetTimer.current), [])
  const browserCanShare = typeof navigator.share === "function"
  const amountText = describeInvoiceAmount(invoice)
  const assetSymbol = invoice.asset?.symbol ?? ""

  async function copyInvoiceLink() {
    try {
      await copyTextToClipboard(invoice.url)
    } catch {
      toast.error("Couldn't copy: select the link and copy it.")
      return
    }
    setLinkWasCopied(true)
    window.clearTimeout(copiedResetTimer.current)
    copiedResetTimer.current = window.setTimeout(() => setLinkWasCopied(false), 1600)
  }

  // The Web Share API where the browser has it (phones), else a copy.
  async function shareInvoiceLink() {
    if (!browserCanShare) {
      await copyInvoiceLink()
      return
    }
    try {
      await navigator.share({ title: `Pay ${amountText}`, url: invoice.url })
    } catch (shareError) {
      if ((shareError as Error).name !== "AbortError") await copyInvoiceLink()
    }
  }

  async function runInvoiceAction(actionName: string, path: string, body: unknown, successText: string) {
    setBusyAction(actionName)
    try {
      const nextInvoice = await fetchApiJson<AccountInvoice>(`/api/v1/invoices/${encodeURIComponent(invoice.id)}${path}`, { method: "POST", body })
      onInvoiceChange(nextInvoice)
      toast.success(successText)
    } catch (actionError) {
      toast.error(describeError(actionError))
    } finally {
      setBusyAction("")
    }
  }

  const settleWithPayerLine = describeSettleWithPayerLine(invoice)
  const canCancel = unpaidInvoiceStatuses.has(invoice.status) && !invoice.seen && !Number(invoice.amount_received ?? 0)
  // The API refuses to accept an invoice whose own coin is being or was given back (any refund not `failed`).
  const canAcceptPartial = invoice.status === "underpaid" && listRefundsUnderway(invoice).length === 0
  // Only what the API offers (operator-destination invoices, past the window, plain-account senders, over the minimum);
  // any other refund it would refuse.
  const refundableSenders = invoice.refund_offer.available ? invoice.refund_offer.senders : []
  const shareIsPrimary = unpaidInvoiceStatuses.has(invoice.status)
  const linkIsShareable = shareableInvoiceStatuses.has(invoice.status)
  const seenAmount = sumSeenAmount(invoice) ?? "0"
  const receivedText = ["paid", "overpaid"].includes(invoice.status)
    ? `${formatDecimalForDisplay(invoice.amount_received ?? seenAmount)} ${assetSymbol}`
    : `${formatDecimalForDisplay(seenAmount)} of ${formatDecimalForDisplay(invoice.amount_due ?? "0")} ${assetSymbol}`

  const copyLinkButton = (
    <Button id="copy_invoice_link_button" variant="secondary" className={cn(touchTargetButtonClassName, "w-full")} onClick={() => void copyInvoiceLink()}>
      {linkWasCopied ? <Check /> : <Copy />} {linkWasCopied ? "Copied" : "Copy link"}
    </Button>
  )
  const shareLinkButton = (primary: boolean) => (
    <Button
      id="share_invoice_link_button"
      variant={primary ? "default" : "secondary"}
      className={cn(primary ? primaryActionButtonClassName : touchTargetButtonClassName, "w-full")}
      onClick={() => void shareInvoiceLink()}
    >
      {browserCanShare ? <Share /> : <Copy />} {browserCanShare ? "Share link" : "Copy link"}
    </Button>
  )
  const openInvoiceButton = (primary: boolean) => (
    <Button id="open_invoice_button" asChild variant={primary ? "default" : "secondary"} className={cn(primary ? primaryActionButtonClassName : touchTargetButtonClassName, "w-full")}>
      <a href={`/i/${encodeURIComponent(invoice.id)}`}>
        Open invoice <ExternalLink />
      </a>
    </Button>
  )

  // Without the Web Share API "Share link" is only a copy, so it never sits beside "Copy link".
  const firstLinkButton = shareIsPrimary && !browserCanShare ? null : copyLinkButton
  const secondLinkButton = shareIsPrimary ? openInvoiceButton(false) : linkIsShareable && browserCanShare ? shareLinkButton(false) : null

  return (
    <div className="flex flex-col">
      <dl className="divide-y rounded-2xl border text-sm">
        {invoice.amount_due ? (
          <div className="flex min-h-11 items-center justify-between gap-3 px-4">
            <dt className="text-muted-foreground">Received</dt>
            <dd className="tabular-nums">{receivedText}</dd>
          </div>
        ) : null}
        <div className="flex min-h-11 items-center justify-between gap-3 px-4">
          <dt className="text-muted-foreground">Pays</dt>
          <dd className="font-mono">{shortenHexString(invoice.destination_address, 6)}</dd>
        </div>
        <div className="flex min-h-11 items-center justify-between gap-3 px-4">
          <dt className="text-muted-foreground">Made</dt>
          <dd>{formatLocalDateTime(invoice.created_at)}</dd>
        </div>
      </dl>
      {settleWithPayerLine ? (
        <p data-testid="settle_with_payer_line" className="mt-3 flex items-start gap-2 rounded-2xl border border-warning/40 bg-warning/10 p-4 text-sm">
          <CircleAlert className="mt-0.5 size-4 shrink-0 text-warning" /> {settleWithPayerLine}
        </p>
      ) : null}
      {invoice.stray_payments.length > 0 ? (
        <div data-testid="invoice_stray_payment_block" className="mt-3 rounded-2xl border border-warning/40 bg-warning/10 p-4 text-sm">
          <p className="flex items-center gap-2 font-medium">
            <CircleAlert className="size-4 shrink-0 text-warning" /> Wrong coin received
          </p>
          <ul className="mt-1.5 flex flex-col gap-1">
            {invoice.stray_payments.map((strayPayment, strayPaymentIndex) => (
              <li key={`${strayPayment.tx_hash ?? "no_transaction"}_${strayPaymentIndex}`} data-testid="invoice_stray_payment_row" className="tabular-nums">
                {formatDecimalForDisplay(strayPayment.amount)} {strayPayment.asset_symbol} from{" "}
                <span className="font-mono">{strayPayment.sender ? shortenHexString(strayPayment.sender) : "a contract"}</span>
                <span className="text-muted-foreground">
                  {" · "}
                  {strayPayment.final ? "not counted" : "confirming"}
                </span>
              </li>
            ))}
          </ul>
          <p className="mt-1.5 text-muted-foreground">This invoice takes {assetSymbol || "another coin"} only, so it wasn't counted.</p>
          <p data-testid="stray_refund_handling_line" className="mt-1.5">
            {describeStrayHandlingForAccount(invoice)}
          </p>
        </div>
      ) : null}
      <OneLineLinkText linkUrl={invoice.url} testId="invoice_drawer_link" className="mt-3 px-1 text-xs select-all" />
      {/* Unpaid: Share is the primary below, copy and open beside each other. Otherwise copy (and share while a
          payer can still top up) here, and "Open invoice" is the primary. */}
      <div className={cn("mt-2 grid gap-2", firstLinkButton && secondLinkButton && "grid-cols-2")}>
        {firstLinkButton}
        {secondLinkButton}
      </div>
      {canCancel || canAcceptPartial || refundableSenders.length > 0 ? <Separator className="my-4" /> : null}
      <div className="flex flex-col gap-2">
        {refundableSenders.map((refundableSender) => {
          const refundAmountText = `${formatDecimalForDisplay(refundableSender.amount)} ${assetSymbol}`
          const senderText = shortenHexString(refundableSender.sender)
          return (
            <AlertDialog key={refundableSender.sender}>
              <AlertDialogTrigger asChild>
                <Button data-testid="refund_sender_button" variant="secondary" className={cn(touchTargetButtonClassName, "w-full justify-start")} disabled={busyAction !== ""}>
                  {busyAction === `refund_${refundableSender.sender}` ? <Spinner /> : <Undo2 />}
                  Refund {refundAmountText} to {senderText}
                </Button>
              </AlertDialogTrigger>
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogTitle>
                    Refund {refundAmountText} to {senderText}?
                  </AlertDialogTitle>
                  <AlertDialogDescription>
                    {invoice.status === "underpaid"
                      ? "It goes back to the wallet that sent it, and this invoice closes. This can't be undone."
                      : "The extra goes back to the wallet that sent it. This can't be undone."}
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel className={touchTargetButtonClassName}>Keep it</AlertDialogCancel>
                  <AlertDialogAction
                    data-testid="confirm_refund_sender_button"
                    className={touchTargetButtonClassName}
                    onClick={() =>
                      void runInvoiceAction(
                        `refund_${refundableSender.sender}`,
                        "/refunds",
                        { sender: refundableSender.sender, amount: refundableSender.amount },
                        "Refund queued.",
                      )
                    }
                  >
                    Refund {refundAmountText}
                  </AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>
          )
        })}
        {canAcceptPartial ? (
          <Button variant="secondary" className={cn(touchTargetButtonClassName, "w-full justify-start")} disabled={busyAction !== ""} onClick={() => void runInvoiceAction("accept_partial", "/accept-partial", undefined, "Accepted as paid.")}>
            {busyAction === "accept_partial" ? <Spinner /> : <CheckCheck />} Accept what arrived as paid
          </Button>
        ) : null}
        {canCancel ? (
          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button id="cancel_invoice_button" variant="destructive" className={cn(touchTargetButtonClassName, "w-full justify-start")} disabled={busyAction !== ""}>
                {busyAction === "cancel" ? <Spinner /> : <Ban />} Cancel invoice
              </Button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Cancel this {amountText} invoice?</AlertDialogTitle>
                <AlertDialogDescription>The link stops working for the payer. This can't be undone.</AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel className={touchTargetButtonClassName}>Keep it</AlertDialogCancel>
                <AlertDialogAction
                  id="confirm_cancel_invoice_button"
                  variant="destructive"
                  className={touchTargetButtonClassName}
                  onClick={() => void runInvoiceAction("cancel", "/cancel", undefined, "Invoice cancelled.")}
                >
                  Cancel invoice
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        ) : null}
      </div>
      <div className="mt-4 flex flex-col">{shareIsPrimary ? shareLinkButton(true) : openInvoiceButton(true)}</div>
    </div>
  )
}
