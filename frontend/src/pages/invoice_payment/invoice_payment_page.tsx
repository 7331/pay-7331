// `/i/:invoice_id`, the payer's page (PLAN.md section 10): one screen per state, at most a forward and a back button,
// no username anywhere. Polls while the invoice can change; a status change slides the next screen in. The tab that
// just made the invoice lands here with the share overlay on top (its history entry says so); everyone else gets the
// payer page alone.
import { Fragment, useEffect, useState, type ReactNode } from "react"
import { Link, useLocation, useNavigate, useParams } from "react-router"

import { InterfaceLabelsContext } from "@/components/interface_labels_context"
import { PageColumn } from "@/components/page_column"
import { PageContentRisesInContext } from "@/components/page_content_rises_in_context"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { useAccountSession } from "@/hooks/use_account_session"
import {
  buildCreatedInvoiceHistoryState,
  buildMakeAnotherInvoiceHistoryState,
  readCreatedInvoiceShare,
} from "@/hooks/use_created_invoice_share"
import { useCurrentTimeEverySecond } from "@/hooks/use_current_time_every_second"
import { useInvoicePolling } from "@/hooks/use_invoice_polling"
import { usePayerDocumentLanguage } from "@/hooks/use_payer_document_language"
import { fetchApiJson } from "@/lib/api_client"
import type { AccountInvoice, PublicInvoice } from "@/lib/api_types"
import {
  buildCoinNetworkConfirmationKey,
  readCoinNetworkConfirmation,
  rememberCoinNetworkConfirmation,
} from "@/lib/coin_network_confirmation"
import { InvoiceCoinChoiceScreen } from "@/pages/invoice_payment/coin_choice_screen"
import { CreatorShareOverlay } from "@/pages/invoice_payment/creator_share_overlay"
import { InvoiceMakerViewContext, type InvoiceMakerView } from "@/pages/invoice_payment/invoice_maker_view_context"
import { useInvoicePageActions } from "@/pages/invoice_payment/invoice_page_actions"
import { InvoiceStatusHeading, InvoiceTopBar } from "@/pages/invoice_payment/invoice_screen_parts"
import { classifyInvoiceView } from "@/pages/invoice_payment/invoice_view_rules"
import { PaymentCheckSheet } from "@/pages/invoice_payment/payment_check_sheet"
import { InvoiceSendPaymentScreen } from "@/pages/invoice_payment/send_payment_screen"
import {
  InvoiceClosedScreen,
  InvoiceConfirmingScreen,
  InvoicePaidScreen,
  InvoicePriceLapsedScreen,
  InvoiceRefundUnderwayScreen,
} from "@/pages/invoice_payment/settled_screens"
import { primaryActionButtonClassName } from "@/lib/button_size_class_names"
import { readPayerStrings } from "@/lib/payer_language"
import { cn } from "@/lib/utils"

// main.tsx loads this route through Promise.all beside the payer language, which knip does not follow.
/** @public */
export function InvoicePaymentPage() {
  usePayerDocumentLanguage()
  return (
    <CreatorShareLayer>
      <InterfaceLabelsContext value={readPayerStrings().interfaceLabels}>
        <InvoicePaymentScreens />
      </InterfaceLabelsContext>
    </CreatorShareLayer>
  )
}

// Opens the share overlay for the creating tab and gives its top bar the way back to it; tells the screens when the
// viewer made the invoice (that tab, or the signed-in account that owns it). Closing the overlay is written into the
// history entry, so a reload of that entry shows the payer view with the share icon rather than the overlay again.
function CreatorShareLayer({ children }: { children: ReactNode }) {
  const { invoiceId = "" } = useParams()
  const pageLocation = useLocation()
  const navigate = useNavigate()
  const { accountSession } = useAccountSession()
  const createdInvoiceShare = readCreatedInvoiceShare(pageLocation.state)
  const [shareOverlayIsOpen, setShareOverlayIsOpen] = useState(() => createdInvoiceShare !== null && !createdInvoiceShare.shareOverlayWasClosed)
  const [invoiceOwnerUsername, setInvoiceOwnerUsername] = useState<string | null>(null)
  const signedInUsername = accountSession?.username ?? null

  // A signed-in account reading its own invoice: GET /invoices/{id} answers only for the owner (404 for anyone else).
  useEffect(() => {
    if (!signedInUsername || createdInvoiceShare) return
    let isCurrent = true
    fetchApiJson<AccountInvoice>(`/api/v1/invoices/${encodeURIComponent(invoiceId)}`)
      .then(() => isCurrent && setInvoiceOwnerUsername(signedInUsername))
      .catch(() => undefined)
    return () => {
      isCurrent = false
    }
  }, [signedInUsername, invoiceId, createdInvoiceShare])

  function changeShareOverlayOpen(nextIsOpen: boolean) {
    setShareOverlayIsOpen(nextIsOpen)
    if (!nextIsOpen && createdInvoiceShare && !createdInvoiceShare.shareOverlayWasClosed) {
      void navigate(pageLocation.pathname, {
        replace: true,
        state: buildCreatedInvoiceHistoryState({ ...createdInvoiceShare, shareOverlayWasClosed: true }),
      })
    }
  }

  const invoiceMakerView: InvoiceMakerView = {
    viewerMadeThisInvoice: createdInvoiceShare !== null || (signedInUsername !== null && invoiceOwnerUsername === signedInUsername),
    reopenCreatorShare: createdInvoiceShare ? () => setShareOverlayIsOpen(true) : null,
  }
  return (
    <InvoiceMakerViewContext value={invoiceMakerView}>
      {children}
      {createdInvoiceShare ? (
        <CreatorShareOverlay
          createdInvoiceShare={createdInvoiceShare}
          isOpen={shareOverlayIsOpen}
          onOpenChange={changeShareOverlayOpen}
          onMakeAnother={() =>
            void navigate(createdInvoiceShare.madeOnPath, {
              state: buildMakeAnotherInvoiceHistoryState(createdInvoiceShare.receiverAddress ?? ""),
            })
          }
        />
      ) : null}
    </InvoiceMakerViewContext>
  )
}

// Whether the payer confirmed this invoice's coin and network: this page's tap, or one remembered in sessionStorage.
function useCoinNetworkConfirmation(invoice: PublicInvoice | null): {
  coinNetworkIsConfirmed: boolean
  confirmCoinNetwork(confirmedInvoice: PublicInvoice): void
} {
  const [confirmationKeysThisPage, setConfirmationKeysThisPage] = useState<string[]>([])
  const confirmationKey = invoice?.asset ? buildCoinNetworkConfirmationKey(invoice.id, invoice.chain.key, invoice.asset.symbol) : null
  const coinNetworkIsConfirmed =
    confirmationKey !== null && (confirmationKeysThisPage.includes(confirmationKey) || readCoinNetworkConfirmation(confirmationKey))
  function confirmCoinNetwork(confirmedInvoice: PublicInvoice) {
    if (!confirmedInvoice.asset) return
    const confirmedKey = buildCoinNetworkConfirmationKey(confirmedInvoice.id, confirmedInvoice.chain.key, confirmedInvoice.asset.symbol)
    rememberCoinNetworkConfirmation(confirmedKey)
    setConfirmationKeysThisPage((keys) => [...keys, confirmedKey])
  }
  return { coinNetworkIsConfirmed, confirmCoinNetwork }
}

function InvoicePaymentScreens() {
  const { invoiceId = "" } = useParams()
  const { invoice, loadErrorMessage, connectionProblemMessage, applyInvoiceAndResumePolling } = useInvoicePolling(invoiceId)
  const currentTimeMilliseconds = useCurrentTimeEverySecond()
  const pageActions = useInvoicePageActions(invoice, applyInvoiceAndResumePolling)
  const { coinNetworkIsConfirmed, confirmCoinNetwork } = useCoinNetworkConfirmation(invoice)
  const payerStrings = readPayerStrings()
  // On the page, not the send screen: a payment the check finds moves the page to another screen under the open sheet.
  const [paymentCheckSheetIsOpen, setPaymentCheckSheetIsOpen] = useState(false)
  const [paymentCheckOpeningCount, setPaymentCheckOpeningCount] = useState(0)
  function openPaymentCheck() {
    setPaymentCheckOpeningCount((openingCount) => openingCount + 1)
    setPaymentCheckSheetIsOpen(true)
  }

  if (!invoice) {
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
            <InvoiceStatusHeading title={payerStrings.thisLinkDoesNotWork} />
            <p className="text-[15px] text-muted-foreground">{loadErrorMessage}</p>
          </section>
        ) : (
          <section aria-busy="true" className="flex flex-col items-center gap-4 pt-10">
            <Skeleton className="h-14 w-48 rounded-2xl" />
            <Skeleton className="h-5 w-32" />
            <Skeleton className="mt-6 size-[220px] rounded-3xl" />
            <Skeleton className="h-10 w-full rounded-xl" />
          </section>
        )}
      </PageColumn>
    )
  }

  const viewKind = classifyInvoiceView(invoice)
  // currentTimeMilliseconds re-renders the page each second, so a deadline passing switches the screen by itself.
  const screen = (() => {
    switch (viewKind) {
      case "send_payment":
        if (coinNetworkIsConfirmed) {
          return (
            <InvoiceSendPaymentScreen
              invoice={invoice}
              currentTimeMilliseconds={currentTimeMilliseconds}
              pageActions={pageActions}
              onOpenPaymentCheck={openPaymentCheck}
            />
          )
        }
        // A coin fixed without this tab's tap (one coin only, or committed elsewhere): the same screen, to confirm.
        return (
          <InvoiceCoinChoiceScreen
            invoice={invoice}
            onInvoiceChange={applyInvoiceAndResumePolling}
            onCoinNetworkConfirmed={confirmCoinNetwork}
          />
        )
      case "choose_coin":
        return (
          <InvoiceCoinChoiceScreen
            invoice={invoice}
            onInvoiceChange={applyInvoiceAndResumePolling}
            onCoinNetworkConfirmed={confirmCoinNetwork}
          />
        )
      case "price_lapsed":
        return <InvoicePriceLapsedScreen invoice={invoice} pageActions={pageActions} onInvoiceChange={applyInvoiceAndResumePolling} />
      case "refund_underway":
        return <InvoiceRefundUnderwayScreen invoice={invoice} />
      case "confirming":
        return <InvoiceConfirmingScreen invoice={invoice} />
      case "paid":
        return <InvoicePaidScreen invoice={invoice} pageActions={pageActions} />
      default:
        return <InvoiceClosedScreen invoice={invoice} pageActions={pageActions} />
    }
  })()

  return (
    <>
      {connectionProblemMessage ? (
        <Alert variant="destructive" className="fixed inset-x-3 top-3 z-50 mx-auto w-auto max-w-[416px] shadow-lg">
          <AlertDescription>{payerStrings.connectionProblemStillTrying(connectionProblemMessage)}</AlertDescription>
        </Alert>
      ) : null}
      {/* A state change remounts the screen: its content rises in, the top bar stays still and no frame goes blank. */}
      <PageContentRisesInContext value={true}>
        <Fragment key={`${viewKind}:${invoice.status}:${coinNetworkIsConfirmed}`}>{screen}</Fragment>
      </PageContentRisesInContext>
      {invoice.asset ? (
        <PaymentCheckSheet
          key={paymentCheckOpeningCount}
          invoice={invoice}
          isOpen={paymentCheckSheetIsOpen}
          onOpenChange={setPaymentCheckSheetIsOpen}
          onInvoiceChange={applyInvoiceAndResumePolling}
        />
      ) : null}
    </>
  )
}
