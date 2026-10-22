// Pieces several payer screens share: the top bar, the status heading, the settled screens' context and outcome
// lines, the refund rows and the flag lines.
import { ArrowLeft, ArrowUpRight, ChevronDown, CircleAlert, Eye, Share } from "lucide-react"
import { useLayoutEffect, useRef, useState, type ReactNode } from "react"

import { BrandMark } from "@/components/brand_mark"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { formatDecimalForDisplay, shortenHexString } from "@/lib/amount_formatting"
import type { PublicInvoice } from "@/lib/api_types"
import { touchTargetButtonClassName } from "@/lib/button_size_class_names"
import { readPayerStrings } from "@/lib/payer_language"
import { cn } from "@/lib/utils"
import { useInvoiceMakerView } from "@/pages/invoice_payment/invoice_maker_view_context"
import {
  buildExplorerTransactionUrl,
  describeRefundStatus,
  describeStrayPaymentLine,
  type OutcomeSentence,
} from "@/pages/invoice_payment/invoice_view_rules"

// The invoice's maker (the creating tab, or the signed-in account that owns it) gets one quiet line saying this is the
// payer's view, and the creating tab a share icon back to its overlay; a payer sees just the brand.
export function InvoiceTopBar({ trailing }: { trailing?: ReactNode }) {
  const { viewerMadeThisInvoice, reopenCreatorShare } = useInvoiceMakerView()
  return (
    <>
      <header className="flex min-h-14 items-center justify-between gap-3">
        <BrandMark linksHome={false} />
        <div className="flex items-center gap-1">
          {trailing}
          {reopenCreatorShare ? (
            <Button
              id="reopen_share_overlay_button"
              variant="ghost"
              size="icon-lg"
              className="-mr-2 size-11 rounded-full"
              aria-label="Share your link"
              onClick={reopenCreatorShare}
            >
              <Share className="size-5" />
            </Button>
          ) : null}
        </div>
      </header>
      {viewerMadeThisInvoice ? (
        <p id="payer_preview_bar" className="flex items-center justify-center gap-1.5 rounded-full bg-muted px-3 py-1 text-xs text-muted-foreground">
          <Eye className="size-3.5" /> This is what your payer sees
        </p>
      ) : null}
    </>
  )
}

// Every state has one heading the e2e reads; some screens show it, others keep it for screen readers only.
export function InvoiceStatusHeading({ title, visible = true, className }: { title: string; visible?: boolean; className?: string }) {
  return (
    <h1
      data-testid="invoice_status"
      aria-live="polite"
      className={cn(visible ? "text-3xl font-semibold tracking-tight" : "sr-only", className)}
    >
      {title}
    </h1>
  )
}

// Two lines at most; a longer note becomes a tap target that opens to the whole text and closes again.
export function InvoiceNote({ invoice, className }: { invoice: PublicInvoice; className?: string }) {
  const noteElement = useRef<HTMLSpanElement>(null)
  const [noteIsClamped, setNoteIsClamped] = useState(false)
  const [noteIsExpanded, setNoteIsExpanded] = useState(false)

  useLayoutEffect(() => {
    const shownNote = noteElement.current
    if (!shownNote || noteIsExpanded) return
    const measureClamp = () => setNoteIsClamped(shownNote.scrollHeight > shownNote.clientHeight + 1)
    measureClamp()
    const resizeObserver = new ResizeObserver(measureClamp)
    resizeObserver.observe(shownNote)
    return () => resizeObserver.disconnect()
    // noteIsClamped swaps the wrapper (a paragraph or a button), so the measured element is a new one.
  }, [invoice.note, noteIsExpanded, noteIsClamped])

  if (!invoice.note) return null
  const noteText = (
    <span ref={noteElement} data-testid="invoice_note" className={cn("block break-words", !noteIsExpanded && "line-clamp-2")}>
      {invoice.note}
    </span>
  )
  if (!noteIsClamped && !noteIsExpanded) {
    return <p className={cn("text-[15px] text-muted-foreground", className)}>{noteText}</p>
  }
  return (
    <button
      type="button"
      aria-expanded={noteIsExpanded}
      onClick={() => setNoteIsExpanded(!noteIsExpanded)}
      className={cn("-mx-2 min-h-11 rounded-lg px-2 text-[15px] text-muted-foreground outline-none focus-visible:ring-3 focus-visible:ring-ring/50", className)}
    >
      {noteText}
      <span className="mt-0.5 inline-flex items-center gap-0.5 text-[13px] font-medium text-foreground">
        {noteIsExpanded ? readPayerStrings().showLessOfNote : readPayerStrings().readWholeNote}
        <ChevronDown className={cn("size-3.5 transition-transform", noteIsExpanded && "rotate-180")} />
      </span>
    </button>
  )
}

// A settled screen's one muted line under its heading: the amount, then the note ("1.00 USDC · Logo design").
export function InvoiceContextLine({
  invoice,
  amountText,
  amountTestId,
  testId,
}: {
  invoice: PublicInvoice
  amountText: string
  amountTestId?: string
  testId?: string
}) {
  if (!amountText && !invoice.note) return null
  return (
    <p data-testid={testId} className="mt-1 text-[17px] break-words text-muted-foreground">
      {amountText ? (
        <span data-testid={amountTestId} className="tabular-nums">
          {amountText}
        </span>
      ) : null}
      {amountText && invoice.note ? " · " : null}
      {invoice.note ? <span data-testid="invoice_note">{invoice.note}</span> : null}
    </p>
  )
}

// A settled screen's one sentence on what became of the money; one that ends on where a refund went links that refund
// on the explorer right after it.
export function OutcomeSentenceLine({ sentence, testId }: { sentence: OutcomeSentence; testId: string }) {
  if (!sentence.refundTransactionUrl) {
    return (
      <p data-testid={testId} className="mt-3 text-[15px] break-words">
        {sentence.text}
      </p>
    )
  }
  // The link's arrow stays on the line of the sentence's last word (the address), never alone on a line of its own.
  const lastSpaceIndex = sentence.text.lastIndexOf(" ")
  return (
    <p data-testid={testId} className="mt-3 text-[15px] break-words">
      {sentence.text.slice(0, lastSpaceIndex + 1)}
      <span className="whitespace-nowrap">
        {sentence.text.slice(lastSpaceIndex + 1)}
        <a
          data-testid={`${testId}_refund_link`}
          href={sentence.refundTransactionUrl}
          target="_blank"
          rel="noopener noreferrer"
          aria-label={readPayerStrings().viewRefundOnExplorer}
          className="-my-3 inline-flex size-11 items-center justify-center rounded-full align-middle text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          <ArrowUpRight className="size-4" />
        </a>
      </span>
    </p>
  )
}

// A quiet back link to the seller's shop, in the screen's back-button slot.
export function BackToShopLink({ href }: { href: string }) {
  return (
    <Button id="back_to_shop_link" variant="ghost" asChild className={cn(touchTargetButtonClassName, "w-full")}>
      <a href={href} rel="noopener">
        <ArrowLeft /> {readPayerStrings().backToShop}
      </a>
    </Button>
  )
}

// One small muted line, never a button (the payer page keeps at most two), when the seller's account set a
// contact. It sits at the foot of the content, just above the screen's buttons. The API only hands out
// mailto: and https: links; anything else is not linked.
export function SellerSupportLine({ invoice }: { invoice: PublicInvoice }) {
  const supportContact = invoice.seller_support_contact
  if (!supportContact || !/^(mailto:|https:\/\/)/.test(supportContact.href)) return null
  const opensElsewhere = supportContact.kind !== "email"
  const payerStrings = readPayerStrings()
  return (
    <p data-testid="seller_support_line" className="mt-auto pt-6 text-center text-sm text-muted-foreground">
      {payerStrings.needHelp}{" "}
      <a
        data-testid="seller_support_link"
        href={supportContact.href}
        title={supportContact.display_text}
        target={opensElsewhere ? "_blank" : undefined}
        rel={opensElsewhere ? "noopener noreferrer" : undefined}
        className="inline-flex min-h-11 items-center font-medium text-foreground underline underline-offset-4"
      >
        {payerStrings.contactTheSeller}
      </a>
    </p>
  )
}

// Money in a coin the invoice did not ask for (INC-001, 2026-10-11): calm, not an error. The payer's money is safe and
// the seller has it, so the page says so in one Alert on every screen past the coin choice.
export function StrayPaymentAlert({ invoice, className }: { invoice: PublicInvoice; className?: string }) {
  const strayPaymentLine = describeStrayPaymentLine(invoice)
  if (!strayPaymentLine) return null
  return (
    <Alert data-testid="invoice_stray_payment_alert" className={cn("text-foreground", className)}>
      <CircleAlert />
      <AlertDescription data-testid="invoice_stray_payment_line" className="text-[15px] text-foreground">
        {strayPaymentLine}
      </AlertDescription>
    </Alert>
  )
}

// The stray-money Alert (unless the screen shows it higher up), then the late and held-up lines.
export function InvoiceFlagLines({ invoice, showStrayPaymentAlert = true }: { invoice: PublicInvoice; showStrayPaymentAlert?: boolean }) {
  return (
    <>
      {showStrayPaymentAlert ? <StrayPaymentAlert invoice={invoice} className="mt-4" /> : null}
      {invoice.late_payment ? (
        <Alert className="mt-4">
          <AlertDescription>{readPayerStrings().latePaymentWentToReceiver}</AlertDescription>
        </Alert>
      ) : null}
      {invoice.sweep_blocked ? (
        <Alert className="mt-4">
          <AlertDescription>{readPayerStrings().sweepHeldUp}</AlertDescription>
        </Alert>
      ) : null}
    </>
  )
}

export function InvoiceRefundRows({ invoice }: { invoice: PublicInvoice }) {
  if (invoice.refunds.length === 0) return null
  return (
    <ul data-testid="invoice_refund_list" className="mt-5 w-full divide-y rounded-2xl border text-left">
      {invoice.refunds.map((refund, refundIndex) => (
        <li key={`${refund.tx_hash ?? "pending"}_${refundIndex}`} data-testid="invoice_refund_row" className="flex min-h-14 items-center justify-between gap-3 px-4 py-2.5">
          <div className="min-w-0">
            <div className="font-mono text-sm font-medium break-all tabular-nums">
              {formatDecimalForDisplay(refund.amount)} {refund.asset_symbol ?? invoice.asset?.symbol ?? ""}
            </div>
            <div className="truncate text-xs text-muted-foreground">
              {readPayerStrings().refundRowBackTo} <span className="font-mono">{shortenHexString(refund.recipient)}</span>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2 text-sm">
            <span data-testid="invoice_refund_status" className={cn(refund.status === "final" ? "text-success" : "text-muted-foreground")}>
              {describeRefundStatus(refund.status)}
            </span>
            {refund.tx_hash ? (
              <a
                href={buildExplorerTransactionUrl(invoice.chain.explorer_url, refund.tx_hash)}
                target="_blank"
                rel="noopener noreferrer"
                aria-label={readPayerStrings().viewRefundOnExplorer}
                className="grid size-11 place-items-center rounded-full text-muted-foreground hover:bg-muted"
              >
                <ArrowUpRight className="size-4" />
              </a>
            ) : null}
          </div>
        </li>
      ))}
    </ul>
  )
}
