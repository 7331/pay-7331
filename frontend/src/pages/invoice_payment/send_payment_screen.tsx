// `awaiting_payment` and `underpaid`: the amount to send (tap to copy the exact string), its USD/EUR worth, the QR
// (EIP-681 payment URI, or the address only for wallets that read nothing else), the deposit address (tap to copy) and
// the time left. One primary: "Pay with wallet" with an injected wallet; else "Copy address" on a desktop (no app
// there handles `ethereum:` links) and "Open in wallet" on the payment URI on a phone, which says what to do instead
// when no wallet app took the link (UX pass 2026-10-11). Under it a quiet "I've paid" opens the payment
// check, and under that the seller's "Back to shop" while nothing was sent (2026-10-21); a payer who
// copied or opened the wallet and comes back to the page gets one muted line and a
// pulse pointing at it, the manual path having no transaction hash to lock on (2026-10-12). The page shows this screen
// only once the payer confirmed the coin and network.
//
// An XMR invoice (PLAN.md section 14) has no injected wallet: its one primary is "Open in wallet" on the `monero:` URI
// on every device (Monero's desktop wallets take the link too), the subaddress shows shortened the way Monero wallets
// do it (tap to copy the whole), and nothing from `lib/browser_wallet.ts` runs.
import { Copy, TriangleAlert, Wallet } from "lucide-react"
import { motion } from "motion/react"
import { useEffect, useRef, useState } from "react"

import { CoinLogo } from "@/components/coin_logo"
import { CountdownRing } from "@/components/countdown_ring"
import { PageColumn } from "@/components/page_column"
import { RiseInEntrance } from "@/components/rise_in_entrance"
import { RollingNumberText } from "@/components/rolling_number_text"
import { copyTextToClipboard, TapToCopyButton } from "@/components/tap_to_copy_button"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"
import { useMediaQuery } from "@/hooks/use_media_query"
import { formatAmountToSendForDisplay, formatDecimalForDisplay, shortenHexString, shortenMoneroAddress } from "@/lib/amount_formatting"
import type { PublicInvoice } from "@/lib/api_types"
import { hasBrowserWallet } from "@/lib/browser_wallet"
import { readPayerStrings } from "@/lib/payer_language"
import { vibrateBriefly } from "@/lib/haptic_feedback"
import { publicInvoiceApiPath } from "@/hooks/use_invoice_polling"
import { popSpring } from "@/lib/motion_presets"
import { cn } from "@/lib/utils"
import {
  InvoiceFlagLines,
  InvoiceNote,
  InvoiceRefundRows,
  InvoiceStatusHeading,
  InvoiceTopBar,
  SellerSupportLine,
  StrayPaymentAlert,
  BackToShopLink,
} from "@/pages/invoice_payment/invoice_screen_parts"
import { sentPaymentLockMilliseconds, type InvoicePageActions } from "@/pages/invoice_payment/invoice_page_actions"
import {
  buildExplorerTransactionUrl,
  describeFiatEquivalents,
  describeInvoiceStatusTitle,
  formatEstimatedUsdValue,
  invoiceIsOnMonero,
  padDecimalsToSamePlaces,
  readBackToShopUrlBeforePayment,
} from "@/pages/invoice_payment/invoice_view_rules"
import { pickPayerAmountFontSizeClassName } from "@/lib/payer_amount_sizing"
import { primaryActionButtonClassName } from "@/lib/button_size_class_names"

type QrKind = "payment" | "address"

// A mouse and hover: a desktop, where an `ethereum:` link has nothing to open unless a wallet extension injected itself.
const desktopPointerMediaQuery = "(hover: hover) and (pointer: fine)"

// How long "Open in wallet" waits for the page to be hidden by a wallet app before saying none opened.
const walletAppOpenWaitMilliseconds = 1500

// After "Open in wallet": true once the page stayed visible for the wait, i.e. no app took the `ethereum:` link.
function useOpenWalletFallback(): { openWalletFallbackIsShown: boolean; watchForWalletApp(): void } {
  const [openWalletFallbackIsShown, setOpenWalletFallbackIsShown] = useState(false)
  const fallbackTimer = useRef<number | undefined>(undefined)
  const pageWasHiddenSinceTap = useRef(false)

  useEffect(() => {
    function notePageHidden() {
      if (document.visibilityState !== "hidden") return
      pageWasHiddenSinceTap.current = true
      window.clearTimeout(fallbackTimer.current)
    }
    document.addEventListener("visibilitychange", notePageHidden)
    return () => {
      document.removeEventListener("visibilitychange", notePageHidden)
      window.clearTimeout(fallbackTimer.current)
    }
  }, [])

  function watchForWalletApp() {
    window.clearTimeout(fallbackTimer.current)
    pageWasHiddenSinceTap.current = false
    fallbackTimer.current = window.setTimeout(() => {
      if (!pageWasHiddenSinceTap.current && document.visibilityState === "visible") setOpenWalletFallbackIsShown(true)
    }, walletAppOpenWaitMilliseconds)
  }

  return { openWalletFallbackIsShown, watchForWalletApp }
}

// After a copy or "Open in wallet" the payer leaves for the wallet: once the page was hidden and shows again, true, so
// "I've paid" gets pointed at. `visibilitychange` fires when the tab's content is hidden or shown again (another tab
// or app, a minimized window, the screen off), which blur and focus don't tell
// (https://developer.mozilla.org/en-US/docs/Web/API/Page_Visibility_API).
function usePaymentCheckNudge(): { paymentCheckNudgeIsShown: boolean; armPaymentCheckNudge(): void } {
  const [paymentCheckNudgeIsShown, setPaymentCheckNudgeIsShown] = useState(false)
  const nudgeIsArmed = useRef(false)
  const pageWasHiddenSinceArmed = useRef(false)

  useEffect(() => {
    function notePageVisibility() {
      if (!nudgeIsArmed.current) return
      if (document.visibilityState === "hidden") {
        pageWasHiddenSinceArmed.current = true
      } else if (pageWasHiddenSinceArmed.current) {
        setPaymentCheckNudgeIsShown(true)
      }
    }
    document.addEventListener("visibilitychange", notePageVisibility)
    return () => document.removeEventListener("visibilitychange", notePageVisibility)
  }, [])

  function armPaymentCheckNudge() {
    nudgeIsArmed.current = true
  }

  return { paymentCheckNudgeIsShown, armPaymentCheckNudge }
}

function CopyDepositAddressButton({ depositAddress, onCopy }: { depositAddress: string; onCopy(): void }) {
  const [addressWasCopied, setAddressWasCopied] = useState(false)
  const resetTimer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(resetTimer.current), [])

  async function copyDepositAddress() {
    window.clearTimeout(resetTimer.current)
    onCopy()
    await copyTextToClipboard(depositAddress)
    vibrateBriefly()
    setAddressWasCopied(true)
    resetTimer.current = window.setTimeout(() => setAddressWasCopied(false), 1600)
  }

  return (
    <Button id="copy_address_button" className={cn(primaryActionButtonClassName, "w-full")} onClick={() => void copyDepositAddress()}>
      <Copy /> {addressWasCopied ? readPayerStrings().addressCopied : readPayerStrings().copyAddress}
    </Button>
  )
}

export function InvoiceSendPaymentScreen({
  invoice,
  currentTimeMilliseconds,
  pageActions,
  onOpenPaymentCheck,
}: {
  invoice: PublicInvoice
  currentTimeMilliseconds: number
  pageActions: InvoicePageActions
  onOpenPaymentCheck(): void
}) {
  const [qrKind, setQrKind] = useState<QrKind>("payment")
  // The send that the payer chose to show the QR for anyway, while it waits for the chain; a new send dims it again.
  const [qrShownAnywayForSendAt, setQrShownAnywayForSendAt] = useState<number | null>(null)
  const viewerIsOnDesktop = useMediaQuery(desktopPointerMediaQuery)
  const { openWalletFallbackIsShown, watchForWalletApp } = useOpenWalletFallback()
  const { paymentCheckNudgeIsShown, armPaymentCheckNudge } = usePaymentCheckNudge()
  const payerStrings = readPayerStrings()
  const asset = invoice.asset!
  const depositAddress = invoice.deposit_address!
  const amountRemaining = invoice.amount_remaining ?? "0"
  const isUnderpaid = invoice.status === "underpaid"
  const deadlineIso = isUnderpaid ? (invoice.fiat.currency && invoice.quote ? invoice.quote.expires_at : null) : invoice.expires_at
  const isMoneroInvoice = invoiceIsOnMonero(invoice)
  const walletIsInjected = !isMoneroInvoice && hasBrowserWallet()
  // A desktop gets "Copy address" for an `ethereum:` link nothing there opens; Monero's desktop wallets open `monero:`.
  const opensWalletLink = isMoneroInvoice || !viewerIsOnDesktop
  // `remaining` busts the image cache when a top-up changes the amount in the payment QR.
  const qrImagePath = `${publicInvoiceApiPath(invoice.id, "/qr.svg")}?kind=${qrKind}&remaining=${encodeURIComponent(amountRemaining)}`

  // Underpaid shows the rest beside what arrived, both to the same places so they line up.
  const [shownAmountRemaining, shownAmountReceived] = isUnderpaid
    ? padDecimalsToSamePlaces([amountRemaining, invoice.amount_received ?? "0"])
    : [amountRemaining, invoice.amount_received ?? "0"]
  const remainingUsdEstimate = isUnderpaid ? formatEstimatedUsdValue(amountRemaining, invoice) : ""
  const sentPaymentAwaitingChainSince = pageActions.sentPaymentAwaitingChainSinceMilliseconds
  // A send the chain shows on its way to this deposit never offers "Send again": that would pay twice for sure.
  const sentPaymentLockHasRunOut =
    sentPaymentAwaitingChainSince !== null &&
    !pageActions.sentPaymentIsSeenOnChain &&
    currentTimeMilliseconds - sentPaymentAwaitingChainSince >= sentPaymentLockMilliseconds
  const sentPaymentIsLocked = sentPaymentAwaitingChainSince !== null && !sentPaymentLockHasRunOut
  // Back from the wallet app after a copy or "Open in wallet": point at "I've paid" (not while a wallet send is locked).
  const paymentCheckIsPointedAt = paymentCheckNudgeIsShown && !sentPaymentIsLocked
  // A scan while a wallet send waits for the chain would pay twice: the QR dims until the payer insists.
  const qrIsDimmedForSentPayment = sentPaymentIsLocked && qrShownAnywayForSendAt !== sentPaymentAwaitingChainSince
  // The seller's "Back to shop" only before anything was sent from here or seen on chain (2026-10-21).
  const backToShopUrl = readBackToShopUrlBeforePayment(invoice, {
    paymentWasSent: sentPaymentAwaitingChainSince !== null || Boolean(pageActions.sentTransactionHash),
  })
  const shownAmountText = formatAmountToSendForDisplay(shownAmountRemaining)
  // The symbol (24 px, about 0.65 em a letter), the gap, the copy icon and the button's padding share the line.
  const amountLineExtraPixels = asset.symbol.length * 16 + 8 + 28 + 24

  const primaryAction = walletIsInjected ? (
    <Button
      id="pay_with_wallet_button"
      variant={sentPaymentLockHasRunOut ? "outline" : "default"}
      className={cn(primaryActionButtonClassName, "w-full")}
      disabled={pageActions.walletIsBusy || sentPaymentIsLocked}
      onClick={() => void pageActions.payWithBrowserWallet()}
    >
      {pageActions.walletIsBusy || sentPaymentIsLocked ? <Spinner /> : <Wallet />}
      {pageActions.walletIsBusy
        ? payerStrings.waitingForWallet
        : sentPaymentIsLocked
          ? payerStrings.waitingForConfirmation
          : sentPaymentLockHasRunOut
            ? payerStrings.sendAgain
            : payerStrings.payWithWallet}
    </Button>
  ) : !opensWalletLink ? (
    <CopyDepositAddressButton depositAddress={depositAddress} onCopy={armPaymentCheckNudge} />
  ) : invoice.payment_uri ? (
    <Button id="open_in_wallet_link" asChild className={cn(primaryActionButtonClassName, "w-full")}>
      <a
        href={invoice.payment_uri}
        onClick={() => {
          watchForWalletApp()
          armPaymentCheckNudge()
        }}
      >
        <Wallet /> {payerStrings.openInWallet}
      </a>
    </Button>
  ) : null

  return (
    <PageColumn
      topBar={
        <InvoiceTopBar
          trailing={
            deadlineIso ? (
              // The ring itself reads "29:59 left"; the accessible label says what for.
              <CountdownRing key={deadlineIso} deadlineIso={deadlineIso} currentTimeMilliseconds={currentTimeMilliseconds} accessibleLabel={isUnderpaid ? payerStrings.priceHeldFor : payerStrings.timeLeftToPay} />
            ) : null
          }
        />
      }
      footer={
        <>
          {pageActions.walletMessage ? (
            <p data-testid="invoice_wallet_message" role="status" className={cn("text-center text-sm break-words", pageActions.walletMessage.tone === "error" ? "text-destructive" : "text-muted-foreground")}>
              {pageActions.walletMessage.text}
              {pageActions.sentTransactionHash ? (
                <>
                  {" "}
                  <a className="underline underline-offset-2" href={buildExplorerTransactionUrl(invoice.chain.explorer_url, pageActions.sentTransactionHash)} target="_blank" rel="noopener noreferrer">
                    {shortenHexString(pageActions.sentTransactionHash, 6)}
                  </a>
                </>
              ) : null}
            </p>
          ) : null}
          {sentPaymentLockHasRunOut ? (
            <p data-testid="invoice_send_again_warning" role="status" className="text-center text-sm font-medium text-warning">
              {payerStrings.notSeenAfterTwoMinutes}
            </p>
          ) : null}
          {openWalletFallbackIsShown && !walletIsInjected && opensWalletLink ? (
            <p data-testid="invoice_open_wallet_fallback" role="status" className="text-center text-sm text-foreground">
              {payerStrings.noWalletOpened(isMoneroInvoice)}
            </p>
          ) : null}
          {pageActions.refundMessage ? (
            <p data-testid="invoice_refund_message" role="status" className={cn("text-center text-sm break-words", pageActions.refundMessage.tone === "error" ? "text-destructive" : "text-muted-foreground")}>
              {pageActions.refundMessage.text}
            </p>
          ) : null}
          {primaryAction}
          {invoice.refund_offer.available ? pageActions.renderRefundRequestButton("secondary") : null}
          {/* The back button sits with the other buttons, so "I've paid" stays the quiet line under them all. */}
          {backToShopUrl ? <BackToShopLink href={backToShopUrl} /> : null}
          {paymentCheckIsPointedAt ? (
            <RiseInEntrance>
              <p data-testid="payment_check_nudge" className="text-center text-sm text-muted-foreground">
                {payerStrings.sentItTapIvePaid}
              </p>
            </RiseInEntrance>
          ) : null}
          {/* A quiet text action, not a third button: rereads the deposit, then takes a tx hash.
              Pointed at, it pulses once and takes the foreground colour; reduced motion keeps only the colour. */}
          <motion.button
            type="button"
            id="i_paid_button"
            onClick={onOpenPaymentCheck}
            animate={paymentCheckIsPointedAt ? { scale: [1, 1.08, 1] } : { scale: 1 }}
            transition={popSpring}
            className={cn(
              "mx-auto min-h-11 px-3 text-[15px] font-medium underline underline-offset-4 outline-none hover:text-foreground focus-visible:rounded-lg focus-visible:ring-3 focus-visible:ring-ring/50",
              paymentCheckIsPointedAt ? "text-foreground" : "text-muted-foreground",
            )}
          >
            {payerStrings.ivePaid}
          </motion.button>
        </>
      }
    >
      <InvoiceStatusHeading title={describeInvoiceStatusTitle(invoice)} visible={false} />
      <section className="flex flex-col items-center pt-3 text-center">
        <TapToCopyButton
          textToCopy={amountRemaining}
          accessibleLabel={payerStrings.copyAmountLabel(amountRemaining)}
          onCopy={armPaymentCheckNudge}
          testId="invoice_amount_copy"
          feedbackShownBy="trailing_icon"
          copiedFeedbackText={payerStrings.copiedFeedback}
          copyFailedFeedbackText={payerStrings.selectItAndCopyFeedback}
          className="inline-flex max-w-full min-h-11 items-center gap-2 px-3 py-1"
        >
          <span className="flex min-w-0 flex-wrap items-baseline justify-center gap-x-2 tracking-tighter tabular-nums">
            <span
              data-testid="invoice_crypto_amount"
              className={cn("min-w-0 font-semibold break-all", pickPayerAmountFontSizeClassName(shownAmountText, "text-5xl", amountLineExtraPixels))}
            >
              <RollingNumberText decimalText={shownAmountRemaining} formatDisplayText={formatAmountToSendForDisplay} />
            </span>
            <span data-testid="invoice_crypto_symbol" className="text-2xl font-semibold text-muted-foreground">
              {asset.symbol}
            </span>
          </span>
        </TapToCopyButton>
        {isUnderpaid ? (
          remainingUsdEstimate ? (
            <p data-testid="invoice_remaining_fiat_amount" className="mt-1 text-[15px] text-muted-foreground tabular-nums">
              {remainingUsdEstimate}
            </p>
          ) : null
        ) : describeFiatEquivalents(invoice) ? (
          <p data-testid="invoice_fiat_amount" className="mt-1 text-[15px] text-muted-foreground tabular-nums">
            ≈ {describeFiatEquivalents(invoice)}
          </p>
        ) : null}
        <InvoiceNote invoice={invoice} className="mt-1" />
        {/* Up here, in view: a payer who sent the wrong coin must not think it vanished (INC-001). */}
        <StrayPaymentAlert invoice={invoice} className="mt-3" />
        {isUnderpaid ? (
          // The heading stays for screen readers; this says it in words a payer reads, with the two amounts once each.
          <Alert data-testid="invoice_underpaid_line" className="mt-3 border-warning/40 bg-warning/10 text-foreground">
            <TriangleAlert className="text-warning" />
            <AlertDescription className="text-[15px] text-foreground">
              {/* No refund path on Monero: what is short is the seller's to settle (PLAN.md section 14). */}
              {payerStrings.underpaidArrivedSendMore(formatDecimalForDisplay(shownAmountReceived), shownAmountText, asset.symbol, isMoneroInvoice)}
            </AlertDescription>
          </Alert>
        ) : null}
        {/* Wrong-network and wrong-coin sends are how payers lose money, so the coin chip says it in full strength. */}
        <Badge
          data-testid="invoice_network_warning"
          variant="secondary"
          className="mt-3 h-auto min-h-8 gap-1.5 rounded-full py-1 pr-3 pl-1 text-sm font-medium whitespace-normal text-foreground"
        >
          <CoinLogo assetSymbol={asset.symbol} className="size-6" />
          <TriangleAlert className="size-3.5! shrink-0 text-warning" aria-hidden="true" />
          {payerStrings.onlySendCoinOnChain(asset.symbol, invoice.chain.name)}
        </Badge>
      </section>

      <section className="mt-4 flex flex-col items-center">
        <div data-testid="invoice_qr" className="relative rounded-3xl border bg-white p-3">
          <img
            data-testid="invoice_qr_image"
            src={qrImagePath}
            width={196}
            height={196}
            alt={qrKind === "payment" ? payerStrings.paymentQrAlt(amountRemaining, asset.symbol) : payerStrings.addressOnlyQrAlt}
            className={cn("block size-[196px] transition-[opacity,filter]", qrIsDimmedForSentPayment && "opacity-10 blur-sm")}
          />
          {qrIsDimmedForSentPayment ? (
            <div data-testid="invoice_qr_lock_overlay" className="absolute inset-0 flex flex-col items-center justify-center gap-1 rounded-3xl p-4 text-center">
              <p className="text-[15px] font-medium text-neutral-900">{payerStrings.waitingForYourPayment}</p>
              <button
                type="button"
                id="show_qr_anyway_button"
                onClick={() => setQrShownAnywayForSendAt(sentPaymentAwaitingChainSince)}
                className="min-h-11 px-2 text-[13px] text-neutral-600 underline underline-offset-2"
              >
                {payerStrings.showQrAnyway}
              </button>
            </div>
          ) : null}
        </div>
        {qrKind === "address" ? (
          <p data-testid="invoice_qr_kind_note" className="mt-2 text-[13px] text-muted-foreground">
            {payerStrings.qrHoldsOnlyTheAddress}
          </p>
        ) : null}
        <button
          type="button"
          id="qr_kind_toggle"
          onClick={() => setQrKind(qrKind === "payment" ? "address" : "payment")}
          className="min-h-11 px-2 text-[13px] text-muted-foreground underline-offset-2 hover:underline"
        >
          {qrKind === "payment" ? payerStrings.useAddressOnlyQr : payerStrings.showFullQr}
        </button>
        <TapToCopyButton
          textToCopy={depositAddress}
          accessibleLabel={payerStrings.copyAddressLabel(depositAddress)}
          onCopy={armPaymentCheckNudge}
          testId="invoice_address_copy"
          feedbackShownBy="trailing_icon"
          copiedFeedbackText={payerStrings.copiedFeedback}
          copyFailedFeedbackText={payerStrings.selectItAndCopyFeedback}
          className="flex min-h-11 w-full items-center justify-center gap-2 px-1 py-2"
        >
          {isMoneroInvoice ? (
            // 95 characters would take three lines: Monero wallets show the first and last six, and copy the whole.
            <span data-testid="invoice_deposit_address" title={depositAddress} className="min-w-0 font-mono text-[15px] leading-snug">
              {shortenMoneroAddress(depositAddress)}
            </span>
          ) : (
            // 12px Geist Mono fits all 42 characters on one line from 390 px; narrower phones wrap it.
            <span data-testid="invoice_deposit_address" className="min-w-0 font-mono text-[12px] leading-snug tracking-tight break-all sm:text-[13px]">
              {depositAddress}
            </span>
          )}
        </TapToCopyButton>
      </section>

      <InvoiceFlagLines invoice={invoice} showStrayPaymentAlert={false} />
      <InvoiceRefundRows invoice={invoice} />
      <SellerSupportLine invoice={invoice} />
    </PageColumn>
  )
}
