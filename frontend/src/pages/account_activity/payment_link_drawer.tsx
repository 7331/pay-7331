// One of the account's quantity links in a drawer (2026-10-11): sold N of M, the link (copy, share, open), the payers'
// invoices under it (`GET /api/v1/payment-links/{id}`), and "Close link" after a confirm (`POST .../close`): no new
// payer after that, invoices already made run their course.
import { Ban, Check, ChevronRight, Copy, ExternalLink, Share } from "lucide-react"
import { useEffect, useRef, useState } from "react"
import { toast } from "sonner"

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
import { Drawer, DrawerContent, DrawerDescription, DrawerFooter, DrawerHeader, DrawerTitle } from "@/components/ui/drawer"
import { Item, ItemActions, ItemContent, ItemDescription, ItemGroup, ItemTitle } from "@/components/ui/item"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { describeError, fetchApiJson } from "@/lib/api_client"
import type { AccountPaymentLink, AccountPaymentLinkDetail } from "@/lib/api_types"
import { formatShortTime } from "@/lib/time_formatting"
import { describeInvoiceLine } from "@/pages/account_activity/invoice_descriptions"
import { InvoiceStatusBadge } from "@/pages/account_activity/invoice_status_badge"
import { describePaymentLinkAmount, describePaymentLinkSales } from "@/pages/account_activity/payment_link_descriptions"
import { PaymentLinkStatusBadge } from "@/pages/account_activity/payment_link_status_badge"
import { primaryActionButtonClassName, touchTargetButtonClassName } from "@/lib/button_size_class_names"
import { cn } from "@/lib/utils"

export function PaymentLinkDrawer({
  paymentLink,
  reloadKey,
  onClose,
  onPaymentLinkChange,
}: {
  paymentLink: AccountPaymentLink | null
  // Changes when an account event arrives, so the open drawer's invoices follow.
  reloadKey: number
  onClose(): void
  onPaymentLinkChange(nextPaymentLink: AccountPaymentLink): void
}) {
  const [paymentLinkDetail, setPaymentLinkDetail] = useState<AccountPaymentLinkDetail | null>(null)
  const [detailLoadError, setDetailLoadError] = useState("")
  const [isClosing, setIsClosing] = useState(false)
  const [linkWasCopied, setLinkWasCopied] = useState(false)
  const copiedResetTimer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(copiedResetTimer.current), [])
  const browserCanShare = typeof navigator.share === "function"
  const paymentLinkId = paymentLink?.id ?? null

  // The state changes in the promise callbacks, never synchronously in the effect.
  useEffect(() => {
    if (!paymentLinkId) return
    let isCurrent = true
    fetchApiJson<AccountPaymentLinkDetail>(`/api/v1/payment-links/${encodeURIComponent(paymentLinkId)}`).then(
      (loadedDetail) => {
        if (!isCurrent) return
        setPaymentLinkDetail(loadedDetail)
        setDetailLoadError("")
      },
      (loadError: unknown) => isCurrent && setDetailLoadError(describeError(loadError)),
    )
    return () => {
      isCurrent = false
    }
  }, [paymentLinkId, reloadKey])

  async function copyPaymentLink(linkUrl: string) {
    try {
      await copyTextToClipboard(linkUrl)
    } catch {
      toast.error("Couldn't copy: select the link and copy it.")
      return
    }
    setLinkWasCopied(true)
    window.clearTimeout(copiedResetTimer.current)
    copiedResetTimer.current = window.setTimeout(() => setLinkWasCopied(false), 1600)
  }

  async function sharePaymentLink(linkUrl: string, amountText: string) {
    if (!browserCanShare) {
      await copyPaymentLink(linkUrl)
      return
    }
    try {
      await navigator.share({ title: `Pay ${amountText}`, url: linkUrl })
    } catch (shareError) {
      if ((shareError as Error).name !== "AbortError") await copyPaymentLink(linkUrl)
    }
  }

  async function closePaymentLink(paymentLinkToClose: AccountPaymentLink) {
    setIsClosing(true)
    try {
      const closedPaymentLink = await fetchApiJson<AccountPaymentLink>(
        `/api/v1/payment-links/${encodeURIComponent(paymentLinkToClose.id)}/close`,
        { method: "POST" },
      )
      onPaymentLinkChange(closedPaymentLink)
      setPaymentLinkDetail((currentDetail) => (currentDetail ? { ...currentDetail, ...closedPaymentLink } : currentDetail))
      toast.success("Link closed.")
    } catch (closeError) {
      toast.error(describeError(closeError))
    } finally {
      setIsClosing(false)
    }
  }

  const shownDetail = paymentLinkDetail?.id === paymentLinkId ? paymentLinkDetail : null

  return (
    <Drawer open={paymentLink !== null} onOpenChange={(isOpen) => !isOpen && onClose()}>
      <DrawerContent className="sm:mx-auto sm:max-w-[440px]">
        {paymentLink ? (
          <div className="mx-auto w-full max-w-[440px] overflow-y-auto px-5">
            <DrawerHeader className="items-start px-0 pt-5 text-left md:text-left">
              <div className="flex w-full items-center justify-between gap-3">
                <DrawerTitle className="text-3xl font-semibold tracking-tight tabular-nums">{describePaymentLinkAmount(paymentLink)}</DrawerTitle>
                <PaymentLinkStatusBadge paymentLinkStatus={paymentLink.status} />
              </div>
              <DrawerDescription className="text-[15px]">{paymentLink.note || "No note"}</DrawerDescription>
            </DrawerHeader>
            <p id="payment_link_drawer_sales" className="text-[15px] font-medium tabular-nums">
              {describePaymentLinkSales(paymentLink)}
            </p>
            <p data-testid="payment_link_drawer_link" className="mt-3 px-1 font-mono text-xs break-all text-muted-foreground select-all">
              {paymentLink.url}
            </p>
            <div className="mt-2 grid grid-cols-2 gap-2">
              <Button id="copy_payment_link_button" variant="secondary" className={touchTargetButtonClassName} onClick={() => void copyPaymentLink(paymentLink.url)}>
                {linkWasCopied ? <Check /> : <Copy />} {linkWasCopied ? "Copied" : "Copy link"}
              </Button>
              <Button variant="secondary" className={touchTargetButtonClassName} onClick={() => void sharePaymentLink(paymentLink.url, describePaymentLinkAmount(paymentLink))}>
                <Share /> Share
              </Button>
            </div>
            <Separator className="my-4" />
            <h2 className="mb-2 px-1 text-sm font-medium text-muted-foreground">Payers</h2>
            {shownDetail ? (
              shownDetail.invoices.length === 0 ? (
                <p className="px-1 py-3 text-sm text-muted-foreground">Nobody yet. Share the link.</p>
              ) : (
                <ItemGroup id="payment_link_invoice_list" className="gap-0 rounded-2xl border">
                  {shownDetail.invoices.map((payerInvoice) => (
                    <Item key={payerInvoice.id} asChild size="sm" className="min-h-14 rounded-none border-b-border last:border-b-transparent">
                      <a href={`/i/${encodeURIComponent(payerInvoice.id)}`} target="_blank" rel="noopener noreferrer">
                        <ItemContent className="min-w-0">
                          <ItemTitle className="tabular-nums">{payerInvoice.asset?.symbol ?? "No coin yet"}</ItemTitle>
                          <ItemDescription className="truncate">
                            {[formatShortTime(payerInvoice.created_at), describeInvoiceLine({ ...payerInvoice, note: "" })].filter(Boolean).join(" · ")}
                          </ItemDescription>
                        </ItemContent>
                        <ItemActions>
                          <InvoiceStatusBadge invoiceStatus={payerInvoice.status} paymentIsSeen={payerInvoice.seen} />
                          <ChevronRight className="size-4 text-muted-foreground" />
                        </ItemActions>
                      </a>
                    </Item>
                  ))}
                </ItemGroup>
              )
            ) : detailLoadError ? (
              <p className="py-3 text-sm text-destructive">Can't load the payers: {detailLoadError}</p>
            ) : (
              <Skeleton className="h-14 w-full rounded-2xl" />
            )}
            {paymentLink.status !== "closed" ? (
              <AlertDialog>
                <AlertDialogTrigger asChild>
                  <Button id="close_payment_link_button" variant="destructive" className={cn(touchTargetButtonClassName, "mt-4 w-full justify-start")} disabled={isClosing}>
                    {isClosing ? <Spinner /> : <Ban />} Close link
                  </Button>
                </AlertDialogTrigger>
                <AlertDialogContent>
                  <AlertDialogHeader>
                    <AlertDialogTitle>Close this {describePaymentLinkAmount(paymentLink)} link?</AlertDialogTitle>
                    <AlertDialogDescription>
                      Nobody new can pay it. Invoices already started can still be paid. This can't be undone.
                    </AlertDialogDescription>
                  </AlertDialogHeader>
                  <AlertDialogFooter>
                    <AlertDialogCancel className={touchTargetButtonClassName}>Keep it open</AlertDialogCancel>
                    <AlertDialogAction id="confirm_close_payment_link_button" variant="destructive" className={touchTargetButtonClassName} onClick={() => void closePaymentLink(paymentLink)}>
                      Close link
                    </AlertDialogAction>
                  </AlertDialogFooter>
                </AlertDialogContent>
              </AlertDialog>
            ) : null}
            <DrawerFooter className="px-0">
              <Button asChild className={cn(primaryActionButtonClassName, "w-full")}>
                <a href={`/l/${encodeURIComponent(paymentLink.id)}`}>
                  Open link <ExternalLink />
                </a>
              </Button>
            </DrawerFooter>
          </div>
        ) : null}
      </DrawerContent>
    </Drawer>
  )
}
