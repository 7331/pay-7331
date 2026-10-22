// One customer's static deposit address in a drawer (PLAN.md section 17): the address to copy, where its money goes,
// and its payments (`GET /api/v1/static-deposit-addresses/{id}`, newest first). An address has no off switch: every
// final payment to it is counted (PLAN.md section 17).
import { AnimatePresence, motion } from "motion/react"
import { useEffect, useState } from "react"

import { TapToCopyButton } from "@/components/tap_to_copy_button"
import { Drawer, DrawerContent, DrawerDescription, DrawerHeader, DrawerTitle } from "@/components/ui/drawer"
import { Item, ItemContent, ItemDescription, ItemGroup, ItemTitle } from "@/components/ui/item"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import { formatDecimalForDisplay, formatFiatAmount, shortenHexString } from "@/lib/amount_formatting"
import { describeError, fetchApiJson } from "@/lib/api_client"
import type { StaticDepositAddress, StaticDepositPayment } from "@/lib/api_types"
import { popSpring } from "@/lib/motion_presets"
import { formatShortTime } from "@/lib/time_formatting"
import { cn } from "@/lib/utils"
import { StaticDepositAddressHeldUpBadge } from "@/pages/account_activity/static_deposit_address_held_up_badge"
import { buildExplorerTransactionUrl } from "@/pages/invoice_payment/invoice_view_rules"

const uncreditedReasonTexts: Record<NonNullable<StaticDepositPayment["uncredited_reason"]>, string> = {
  wrong_coin: "Not counted: a coin this address doesn't take. Sent to your wallet anyway.",
}

function describeStaticDepositPaymentLine(payment: StaticDepositPayment): string {
  return [
    formatShortTime(payment.created_at),
    payment.sender ? `from ${shortenHexString(payment.sender)}` : "",
    payment.uncredited_reason ? uncreditedReasonTexts[payment.uncredited_reason] : "",
  ]
    .filter(Boolean)
    .join(" · ")
}

export function StaticDepositAddressDrawer({
  staticDepositAddress,
  reloadKey,
  onClose,
}: {
  staticDepositAddress: StaticDepositAddress | null
  // Changes when an account event arrives, so the open drawer's payments follow.
  reloadKey: number
  onClose(): void
}) {
  const [addressDetail, setAddressDetail] = useState<StaticDepositAddress | null>(null)
  const [detailLoadError, setDetailLoadError] = useState("")
  const staticDepositAddressId = staticDepositAddress?.id ?? null

  // The state changes in the promise callbacks, never synchronously in the effect.
  useEffect(() => {
    if (!staticDepositAddressId) return
    let isCurrent = true
    fetchApiJson<StaticDepositAddress>(`/api/v1/static-deposit-addresses/${encodeURIComponent(staticDepositAddressId)}`).then(
      (loadedDetail) => {
        if (!isCurrent) return
        setAddressDetail(loadedDetail)
        setDetailLoadError("")
      },
      (loadError: unknown) => isCurrent && setDetailLoadError(describeError(loadError)),
    )
    return () => {
      isCurrent = false
    }
  }, [staticDepositAddressId, reloadKey])

  const shownDetail = addressDetail?.id === staticDepositAddressId ? addressDetail : null
  const shownAddress = shownDetail ?? staticDepositAddress

  return (
    <Drawer open={staticDepositAddress !== null} onOpenChange={(isOpen) => !isOpen && onClose()}>
      <DrawerContent className="sm:mx-auto sm:max-w-[440px]">
        {shownAddress ? (
          <div className="mx-auto w-full max-w-[440px] overflow-y-auto px-5 pb-6">
            <DrawerHeader className="items-start px-0 pt-5 text-left md:text-left">
              <div className="flex w-full items-center justify-between gap-3">
                <DrawerTitle className="min-w-0 truncate text-2xl font-semibold tracking-tight">{shownAddress.customer_reference}</DrawerTitle>
                <StaticDepositAddressHeldUpBadge staticDepositAddress={shownAddress} />
              </div>
              <DrawerDescription className="text-[15px] tabular-nums">
                {formatFiatAmount(shownAddress.received_usd, "USD")} received · {shownAddress.assets.join(", ")} on {shownAddress.chain.name}
              </DrawerDescription>
            </DrawerHeader>
            <TapToCopyButton
              id="static_deposit_address_copy_button"
              textToCopy={shownAddress.deposit_address}
              accessibleLabel="Copy the deposit address"
              className="rounded-xl border px-3.5 py-3 text-left font-mono text-[15px] break-all"
            >
              {shownAddress.deposit_address}
            </TapToCopyButton>
            <p className="mt-2 px-1 text-sm text-muted-foreground">
              Goes to <span className="font-mono">{shortenHexString(shownAddress.destination_address)}</span>, the payout address it was made with.
            </p>
            {shownAddress.sweep_blocked ? (
              <p className="mt-2 px-1 text-sm text-destructive">Sending its money to your wallet failed. The operator moves it by hand.</p>
            ) : null}
            <Separator className="my-4" />
            <h2 className="mb-2 px-1 text-sm font-medium text-muted-foreground">Payments</h2>
            {shownDetail ? (
              shownDetail.payments.length === 0 ? (
                <p className="px-1 py-3 text-sm text-muted-foreground">Nothing yet.</p>
              ) : (
                <ItemGroup id="static_deposit_payment_list" className="gap-0 rounded-2xl border">
                  <AnimatePresence initial={false}>
                    {shownDetail.payments.map((payment) => (
                      <motion.div key={payment.id} layout initial={{ opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }} transition={popSpring}>
                        <Item asChild size="sm" className="min-h-14 rounded-none border-b-border">
                          <a
                            href={payment.tx_hash ? buildExplorerTransactionUrl(shownAddress.chain.explorer_url, payment.tx_hash) : undefined}
                            target="_blank"
                            rel="noopener noreferrer"
                            data-testid="static_deposit_payment_row"
                          >
                            <ItemContent className="min-w-0">
                              <ItemTitle className={cn("tabular-nums", !payment.credited && "text-muted-foreground")}>
                                {formatDecimalForDisplay(payment.amount)} {payment.asset_symbol}
                                {payment.credited && payment.amount_usd ? <span className="font-normal text-muted-foreground"> · {formatFiatAmount(payment.amount_usd, "USD")}</span> : null}
                              </ItemTitle>
                              <ItemDescription className="line-clamp-2">{describeStaticDepositPaymentLine(payment)}</ItemDescription>
                            </ItemContent>
                          </a>
                        </Item>
                      </motion.div>
                    ))}
                  </AnimatePresence>
                </ItemGroup>
              )
            ) : detailLoadError ? (
              <p className="py-3 text-sm text-destructive">Can't load the payments: {detailLoadError}</p>
            ) : (
              <Skeleton className="h-14 w-full rounded-2xl" />
            )}
          </div>
        ) : null}
      </DrawerContent>
    </Drawer>
  )
}
