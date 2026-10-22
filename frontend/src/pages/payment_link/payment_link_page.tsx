// `/l/:payment_link_id`, a quantity link's payer page (2026-10-11): up to N payers, each with an invoice of their own.
// Dumb simple like `/i/`: the amount, the note, how many are left, the coins; a tap selects, "Pay 2.00 USDC on Polygon"
// takes a slot (`POST /public/payment-links/{id}/invoices`) and goes on to that invoice's send screen, the tap being
// the payer's confirmation of coin and network there too (2026-10-14); "Back" deselects. Viewing the page takes
// nothing, so a bot reading links holds no slot. The tab that made the link lands here under the share
// overlay; the maker (that tab, or the signed-in account that owns it) sees the payer view with the commit disabled.
import { ArrowLeft, ArrowRight, RefreshCw } from "lucide-react"
import { useCallback, useEffect, useState, type ReactNode } from "react"
import { Link, useLocation, useNavigate, useParams } from "react-router"

import { CoinChoiceRows } from "@/components/coin_choice_rows"
import { CoinNetworkPayFooter } from "@/components/coin_network_pay_footer"
import { InterfaceLabelsContext } from "@/components/interface_labels_context"
import { PageColumn } from "@/components/page_column"
import { PaymentTrustLine } from "@/components/payment_trust_line"
import { RollingNumberText } from "@/components/rolling_number_text"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { useAccountSession } from "@/hooks/use_account_session"
import {
  buildCreatedInvoiceHistoryState,
  buildInvoicePagePath,
  buildMakeAnotherInvoiceHistoryState,
  readCreatedInvoiceShare,
} from "@/hooks/use_created_invoice_share"
import { buildPaymentLinkTakenInvoiceHistoryState, readPaymentLinkTakenInvoiceId } from "@/hooks/use_created_payment_link_share"
import { usePayerDocumentLanguage } from "@/hooks/use_payer_document_language"
import { ApiError, fetchApiJson } from "@/lib/api_client"
import type { AccountPaymentLinkDetail, PublicInvoice, PublicPaymentLink } from "@/lib/api_types"
import { buildCoinNetworkConfirmationKey, rememberCoinNetworkConfirmation } from "@/lib/coin_network_confirmation"
import { describePayerError } from "@/lib/payer_errors"
import { formatFiatAmount } from "@/lib/amount_formatting"
import { readPayerStrings } from "@/lib/payer_language"
import { buildPaymentOptionKey, groupPaymentOptionsByCoin } from "@/lib/payment_options"
import { pickPayerAmountFontSizeClassName } from "@/lib/payer_amount_sizing"
import { cn } from "@/lib/utils"
import { CreatorShareOverlay } from "@/pages/invoice_payment/creator_share_overlay"
import { InvoiceMakerViewContext, useInvoiceMakerView, type InvoiceMakerView } from "@/pages/invoice_payment/invoice_maker_view_context"
import { InvoiceTopBar } from "@/pages/invoice_payment/invoice_screen_parts"
import { primaryActionButtonClassName, touchTargetButtonClassName } from "@/lib/button_size_class_names"

// Refusals that mean the link changed under the payer: show its fresh state rather than an error line.
const slotRefusalCodes = new Set(["sold_out", "payment_link_closed"])

function publicPaymentLinkApiPath(paymentLinkId: string, suffix = ""): string {
  return `/api/v1/public/payment-links/${encodeURIComponent(paymentLinkId)}${suffix}`
}

// main.tsx loads this route through Promise.all beside the payer language, which knip does not follow.
/** @public */
export function PaymentLinkPage() {
  usePayerDocumentLanguage()
  const { paymentLinkId = "" } = useParams()
  const pageLocation = useLocation()
  const navigate = useNavigate()
  const { accountSession } = useAccountSession()
  const createdLinkShare = readCreatedInvoiceShare(pageLocation.state)
  const [shareOverlayIsOpen, setShareOverlayIsOpen] = useState(() => createdLinkShare !== null && !createdLinkShare.shareOverlayWasClosed)
  const [linkOwnerUsername, setLinkOwnerUsername] = useState<string | null>(null)
  const signedInUsername = accountSession?.username ?? null

  // A signed-in account reading its own link: GET /payment-links/{id} answers only for the owner (404 for anyone else).
  useEffect(() => {
    if (!signedInUsername || createdLinkShare) return
    let isCurrent = true
    fetchApiJson<AccountPaymentLinkDetail>(`/api/v1/payment-links/${encodeURIComponent(paymentLinkId)}`)
      .then(() => isCurrent && setLinkOwnerUsername(signedInUsername))
      .catch(() => undefined)
    return () => {
      isCurrent = false
    }
  }, [signedInUsername, paymentLinkId, createdLinkShare])

  function changeShareOverlayOpen(nextIsOpen: boolean) {
    setShareOverlayIsOpen(nextIsOpen)
    if (!nextIsOpen && createdLinkShare && !createdLinkShare.shareOverlayWasClosed) {
      void navigate(pageLocation.pathname, {
        replace: true,
        state: buildCreatedInvoiceHistoryState({ ...createdLinkShare, shareOverlayWasClosed: true }),
      })
    }
  }

  const makerView: InvoiceMakerView = {
    viewerMadeThisInvoice: createdLinkShare !== null || (signedInUsername !== null && linkOwnerUsername === signedInUsername),
    reopenCreatorShare: createdLinkShare ? () => setShareOverlayIsOpen(true) : null,
  }
  return (
    <InvoiceMakerViewContext value={makerView}>
      <InterfaceLabelsContext value={readPayerStrings().interfaceLabels}>
        <PaymentLinkScreens paymentLinkId={paymentLinkId} />
      </InterfaceLabelsContext>
      {createdLinkShare ? (
        <CreatorShareOverlay
          createdInvoiceShare={createdLinkShare}
          isOpen={shareOverlayIsOpen}
          onOpenChange={changeShareOverlayOpen}
          onMakeAnother={() =>
            void navigate(createdLinkShare.madeOnPath, {
              state: buildMakeAnotherInvoiceHistoryState(createdLinkShare.receiverAddress ?? ""),
            })
          }
        />
      ) : null}
    </InvoiceMakerViewContext>
  )
}

function PaymentLinkScreens({ paymentLinkId }: { paymentLinkId: string }) {
  const pageLocation = useLocation()
  const navigate = useNavigate()
  const { viewerMadeThisInvoice: viewerMadeThisLink } = useInvoiceMakerView()
  const takenInvoiceId = readPaymentLinkTakenInvoiceId(pageLocation.state)
  const [paymentLink, setPaymentLink] = useState<PublicPaymentLink | null>(null)
  const [loadErrorMessage, setLoadErrorMessage] = useState("")
  // The picked (network, coin) pair's key; null until a tap.
  const [selectedOptionKey, setSelectedOptionKey] = useState<string | null>(null)
  const [isCommitting, setIsCommitting] = useState(false)
  const [isReloading, setIsReloading] = useState(false)
  const [commitErrorMessage, setCommitErrorMessage] = useState("")
  const payerStrings = readPayerStrings()

  // The state changes in the promise callbacks, once the API answers, never synchronously in the effect below.
  const loadPaymentLink = useCallback(
    () =>
      fetchApiJson<PublicPaymentLink>(publicPaymentLinkApiPath(paymentLinkId)).then(
        (loadedPaymentLink) => {
          setPaymentLink(loadedPaymentLink)
          setLoadErrorMessage("")
        },
        (loadError: unknown) =>
          setLoadErrorMessage(
            loadError instanceof ApiError && loadError.httpStatus === 404
              ? readPayerStrings().checkYouHaveTheWholeLink
              : describePayerError(loadError),
          ),
      ),
    [paymentLinkId],
  )
  useEffect(() => {
    void loadPaymentLink()
  }, [loadPaymentLink])

  async function reloadPaymentLink() {
    setIsReloading(true)
    await loadPaymentLink()
    setIsReloading(false)
  }

  async function payWithSelectedCoin() {
    const selectedPaymentOption = paymentLink?.payment_options.find((option) => buildPaymentOptionKey(option) === selectedOptionKey)
    if (!selectedPaymentOption || viewerMadeThisLink) return
    setIsCommitting(true)
    setCommitErrorMessage("")
    try {
      const payerInvoice = await fetchApiJson<PublicInvoice>(publicPaymentLinkApiPath(paymentLinkId, "/invoices"), {
        method: "POST",
        // This tap alone takes the slot, and names the exact pair.
        body: { asset: selectedPaymentOption.asset.symbol, chain: selectedPaymentOption.chain.key },
      })
      // This tap confirmed the coin and network: the invoice opens on its send screen, not on the same question again.
      if (payerInvoice.asset) {
        rememberCoinNetworkConfirmation(buildCoinNetworkConfirmationKey(payerInvoice.id, payerInvoice.chain.key, payerInvoice.asset.symbol))
      }
      // Back from the invoice comes here and offers it again, rather than a second slot.
      await navigate(pageLocation.pathname, { replace: true, state: buildPaymentLinkTakenInvoiceHistoryState(payerInvoice.id) })
      await navigate(buildInvoicePagePath(payerInvoice.id))
    } catch (takeSlotError) {
      setIsCommitting(false)
      if (takeSlotError instanceof ApiError && takeSlotError.code && slotRefusalCodes.has(takeSlotError.code)) {
        setSelectedOptionKey(null)
        await loadPaymentLink()
        return
      }
      setCommitErrorMessage(describePayerError(takeSlotError))
    }
  }

  if (!paymentLink) {
    return (
      <PageColumn
        topBar={<InvoiceTopBar />}
        footer={
          loadErrorMessage ? (
            <Button asChild variant="secondary" className={cn(primaryActionButtonClassName, "w-full")}>
              <Link to="/">{payerStrings.makeYourOwnInvoice}</Link>
            </Button>
          ) : undefined
        }
      >
        {loadErrorMessage ? (
          <section className="flex flex-1 flex-col items-center justify-center gap-2 pb-16 text-center">
            <PaymentLinkStatusHeading title={payerStrings.thisLinkDoesNotWork} />
            <p className="text-[15px] text-muted-foreground">{loadErrorMessage}</p>
          </section>
        ) : (
          <section aria-busy="true" className="flex flex-col items-center gap-4 pt-10">
            <Skeleton className="h-14 w-48 rounded-2xl" />
            <Skeleton className="h-5 w-32" />
            <Skeleton className="mt-6 h-16 w-full rounded-2xl" />
            <Skeleton className="h-16 w-full rounded-2xl" />
          </section>
        )}
      </PageColumn>
    )
  }

  const paymentOptions = groupPaymentOptionsByCoin(paymentLink.payment_options)
  const selectedPaymentOption = paymentOptions.find((option) => buildPaymentOptionKey(option) === selectedOptionKey) ?? null
  const topBar = <InvoiceTopBar />

  if (takenInvoiceId && !viewerMadeThisLink) {
    return (
      <PageColumn
        topBar={topBar}
        footer={
          <>
            <Button id="payment_link_continue_button" asChild className={cn(primaryActionButtonClassName, "w-full")}>
              <Link to={buildInvoicePagePath(takenInvoiceId)}>
                {payerStrings.goToYourInvoice} <ArrowRight />
              </Link>
            </Button>
            <Button
              id="payment_link_start_over_button"
              variant="ghost"
              className={cn(touchTargetButtonClassName, "w-full")}
              onClick={() => void navigate(pageLocation.pathname, { replace: true, state: null })}
            >
              <ArrowLeft /> {payerStrings.startOver}
            </Button>
          </>
        }
      >
        <PaymentLinkAmountHero paymentLink={paymentLink} />
        <section className="flex flex-col items-center gap-2 text-center">
          <PaymentLinkStatusHeading title={payerStrings.youStartedPaying} />
          <p className="text-[15px] text-muted-foreground">{payerStrings.yourInvoiceIsWaiting}</p>
        </section>
      </PageColumn>
    )
  }

  if (paymentLink.status !== "open") {
    const closedScreenCopy = {
      all_taken: { title: payerStrings.allTakenForNow, line: payerStrings.allTakenForNowLine },
      sold_out: { title: payerStrings.soldOut, line: payerStrings.allArePaidFor(paymentLink.quantity) },
      closed: { title: payerStrings.linkClosed, line: payerStrings.linkClosedLine },
    }[paymentLink.status]
    return (
      <PageColumn
        topBar={topBar}
        footer={
          paymentLink.status === "all_taken" ? (
            <Button id="payment_link_check_again_button" variant="secondary" className={cn(primaryActionButtonClassName, "w-full")} disabled={isReloading} onClick={() => void reloadPaymentLink()}>
              {isReloading ? <Spinner /> : <RefreshCw />} {payerStrings.checkAgain}
            </Button>
          ) : undefined
        }
      >
        <PaymentLinkAmountHero paymentLink={paymentLink} />
        <section className="flex flex-col items-center gap-2 text-center">
          <PaymentLinkStatusHeading title={closedScreenCopy.title} />
          <p className="text-[15px] text-muted-foreground">{closedScreenCopy.line}</p>
        </section>
      </PageColumn>
    )
  }

  return (
    <PageColumn
      topBar={topBar}
      footer={
        selectedPaymentOption ? (
          <CoinNetworkPayFooter
            assetSymbol={selectedPaymentOption.asset.symbol}
            amountToSend={selectedPaymentOption.amount}
            chainName={selectedPaymentOption.chain.name}
            footnoteText={
              viewerMadeThisLink ? "Your payer picks the coin." : payerStrings.sendOnChainOnlyNoSwitching(selectedPaymentOption.chain.name)
            }
            errorMessage={commitErrorMessage}
            isCommitting={isCommitting}
            payButtonIsDisabled={viewerMadeThisLink}
            onPay={() => void payWithSelectedCoin()}
            onBack={() => setSelectedOptionKey(null)}
          />
        ) : null
      }
    >
      <PaymentLinkAmountHero paymentLink={paymentLink} showsSlotsLeft />
      <PaymentLinkStatusHeading title={payerStrings.statusChooseHowToPay} className="mb-2.5 px-1 text-[15px] font-medium tracking-normal text-muted-foreground" />
      <CoinChoiceRows
        options={paymentOptions}
        selectedOptionKey={selectedOptionKey}
        onSelectedOptionKeyChange={setSelectedOptionKey}
        rowsAreDisabled={isCommitting}
      />
      <PaymentTrustLine chainKind={(selectedPaymentOption?.chain ?? paymentLink.chain).kind} />
    </PageColumn>
  )
}

// Every state has one heading the e2e reads (`payment_link_status`).
function PaymentLinkStatusHeading({ title, className }: { title: string; className?: string }) {
  return (
    <h1 data-testid="payment_link_status" aria-live="polite" className={cn("text-3xl font-semibold tracking-tight", className)}>
      {title}
    </h1>
  )
}

// The amount in the link's currency, the other currency under it, the note, and while open how many are left.
function PaymentLinkAmountHero({ paymentLink, showsSlotsLeft = false }: { paymentLink: PublicPaymentLink; showsSlotsLeft?: boolean }): ReactNode {
  const fiatCurrency = paymentLink.fiat.currency ?? "USD"
  const otherCurrency = fiatCurrency === "EUR" ? "USD" : "EUR"
  const otherAmount = otherCurrency === "USD" ? paymentLink.fiat.usd : paymentLink.fiat.eur
  return (
    <section className="flex flex-col items-center pt-8 pb-6 text-center">
      <div
        data-testid="payment_link_fiat_total"
        className={cn(
          "max-w-full font-semibold tracking-tighter break-all tabular-nums",
          pickPayerAmountFontSizeClassName(formatFiatAmount(paymentLink.fiat.amount, fiatCurrency), "text-6xl"),
        )}
      >
        <RollingNumberText
          decimalText={paymentLink.fiat.amount}
          formatDisplayText={(fiatDecimalText: string | null) => formatFiatAmount(fiatDecimalText, fiatCurrency)}
        />
      </div>
      {otherAmount ? <div className="mt-1 text-[15px] text-muted-foreground tabular-nums">≈ {formatFiatAmount(otherAmount, otherCurrency)}</div> : null}
      {paymentLink.note ? (
        <p data-testid="payment_link_note" className="mt-3 line-clamp-2 text-base text-foreground">
          {paymentLink.note}
        </p>
      ) : null}
      {showsSlotsLeft ? (
        <p data-testid="payment_link_slots_left" className="mt-2 rounded-full bg-muted px-3 py-1 text-sm text-muted-foreground tabular-nums">
          {readPayerStrings().slotsLeft(paymentLink.slots_left, paymentLink.quantity)}
        </p>
      ) : null}
    </section>
  )
}
