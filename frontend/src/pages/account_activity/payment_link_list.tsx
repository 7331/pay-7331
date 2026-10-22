// `/activity`'s "Links" tab (2026-10-11): the account's quantity links (loaded by the page,
// use_account_payment_links.ts), each row "Sold 3 of 10", and a drawer per link with its payers and "Close link".
import { ChevronRight } from "lucide-react"
import { useState } from "react"

import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty"
import { Item, ItemActions, ItemContent, ItemDescription, ItemGroup, ItemTitle } from "@/components/ui/item"
import { Skeleton } from "@/components/ui/skeleton"
import type { AccountPaymentLink } from "@/lib/api_types"
import { describePaymentLinkAmount, describePaymentLinkSales } from "@/pages/account_activity/payment_link_descriptions"
import { PaymentLinkDrawer } from "@/pages/account_activity/payment_link_drawer"
import { PaymentLinkStatusBadge } from "@/pages/account_activity/payment_link_status_badge"

export function AccountPaymentLinkList({
  paymentLinks,
  linkLoadError,
  liveReloadCount,
  onPaymentLinkChange,
}: {
  paymentLinks: AccountPaymentLink[] | null
  linkLoadError: string
  liveReloadCount: number
  onPaymentLinkChange(nextPaymentLink: AccountPaymentLink): void
}) {
  const [openedPaymentLinkId, setOpenedPaymentLinkId] = useState<string | null>(null)
  const openedPaymentLink = paymentLinks?.find((paymentLink) => paymentLink.id === openedPaymentLinkId) ?? null

  return (
    <>
      {paymentLinks === null && !linkLoadError ? (
        <div className="flex flex-col gap-2">
          {[0, 1].map((placeholderIndex) => (
            <Skeleton key={placeholderIndex} className="h-16 w-full rounded-2xl" />
          ))}
        </div>
      ) : linkLoadError ? (
        <p className="py-6 text-center text-sm text-destructive">Can't load links: {linkLoadError}</p>
      ) : paymentLinks && paymentLinks.length === 0 ? (
        <Empty>
          <EmptyHeader>
            <EmptyTitle>No links for several payers yet</EmptyTitle>
            <EmptyDescription>In New, Options, set "How many payers" above 1.</EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <ItemGroup id="payment_link_list" className="gap-2">
          {paymentLinks?.map((paymentLink) => (
            <Item key={paymentLink.id} asChild variant="outline" className="min-h-16 rounded-2xl">
              <button type="button" data-testid="payment_link_row" onClick={() => setOpenedPaymentLinkId(paymentLink.id)} className="w-full text-left">
                <ItemContent className="min-w-0">
                  <ItemTitle className="tabular-nums">
                    {describePaymentLinkAmount(paymentLink)} <span className="font-normal text-muted-foreground">× {paymentLink.quantity}</span>
                  </ItemTitle>
                  <ItemDescription data-testid="payment_link_row_sales" className="truncate">
                    {[describePaymentLinkSales(paymentLink), paymentLink.note].filter(Boolean).join(" · ")}
                  </ItemDescription>
                </ItemContent>
                <ItemActions>
                  <PaymentLinkStatusBadge paymentLinkStatus={paymentLink.status} />
                  <ChevronRight className="size-4 text-muted-foreground" />
                </ItemActions>
              </button>
            </Item>
          ))}
        </ItemGroup>
      )}
      <PaymentLinkDrawer
        paymentLink={openedPaymentLink}
        reloadKey={liveReloadCount}
        onClose={() => setOpenedPaymentLinkId(null)}
        onPaymentLinkChange={onPaymentLinkChange}
      />
    </>
  )
}
