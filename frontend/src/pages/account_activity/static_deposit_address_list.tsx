// `/activity`'s "Customers" tab (PLAN.md section 17, 2026-10-21): the account's static deposit addresses, one per
// customer its server made through the API, each row the customer reference, what it received in USD and, when a
// sweep of its money failed, "Held up"; a drawer per address shows its payments.
import { ChevronRight } from "lucide-react"
import { useState } from "react"

import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty"
import { Item, ItemActions, ItemContent, ItemDescription, ItemGroup, ItemTitle } from "@/components/ui/item"
import { Skeleton } from "@/components/ui/skeleton"
import { formatFiatAmount, shortenHexString } from "@/lib/amount_formatting"
import type { StaticDepositAddress } from "@/lib/api_types"
import { formatShortTime } from "@/lib/time_formatting"
import { LoadOlderButton } from "@/pages/account_activity/load_older_button"
import { StaticDepositAddressDrawer } from "@/pages/account_activity/static_deposit_address_drawer"
import { StaticDepositAddressHeldUpBadge } from "@/pages/account_activity/static_deposit_address_held_up_badge"

export function AccountStaticDepositAddressList({
  staticDepositAddresses,
  addressLoadError,
  hasOlderAddresses,
  olderAddressesAreLoading,
  olderAddressesLoadError,
  onLoadOlderAddresses,
  liveReloadCount,
}: {
  staticDepositAddresses: StaticDepositAddress[] | null
  addressLoadError: string
  hasOlderAddresses: boolean
  olderAddressesAreLoading: boolean
  olderAddressesLoadError: string
  onLoadOlderAddresses(): void
  liveReloadCount: number
}) {
  const [openedAddressId, setOpenedAddressId] = useState<string | null>(null)
  const openedAddress = staticDepositAddresses?.find((staticDepositAddress) => staticDepositAddress.id === openedAddressId) ?? null

  return (
    <>
      {staticDepositAddresses === null && !addressLoadError ? (
        <div className="flex flex-col gap-2">
          {[0, 1].map((placeholderIndex) => (
            <Skeleton key={placeholderIndex} className="h-16 w-full rounded-2xl" />
          ))}
        </div>
      ) : addressLoadError ? (
        <p className="py-6 text-center text-sm text-destructive">Can't load customer addresses: {addressLoadError}</p>
      ) : staticDepositAddresses && staticDepositAddresses.length === 0 ? (
        <Empty>
          <EmptyHeader>
            <EmptyTitle>No customer addresses yet</EmptyTitle>
            <EmptyDescription>
              Your server makes one per customer with an API token. See <a className="underline underline-offset-4" href="/docs#static-deposit-addresses">the docs</a>.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <ItemGroup id="static_deposit_address_list" className="gap-2">
          {staticDepositAddresses?.map((staticDepositAddress) => (
            <Item key={staticDepositAddress.id} asChild variant="outline" className="min-h-16 rounded-2xl">
              <button type="button" data-testid="static_deposit_address_row" onClick={() => setOpenedAddressId(staticDepositAddress.id)} className="w-full text-left">
                <ItemContent className="min-w-0">
                  <ItemTitle className="block w-full truncate">{staticDepositAddress.customer_reference}</ItemTitle>
                  <ItemDescription className="truncate tabular-nums">
                    {[
                      shortenHexString(staticDepositAddress.deposit_address),
                      staticDepositAddress.chain.name,
                      staticDepositAddress.last_payment_at ? `paid ${formatShortTime(staticDepositAddress.last_payment_at)}` : "",
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </ItemDescription>
                </ItemContent>
                <ItemActions className="flex-col items-end gap-1">
                  <span className="font-medium tabular-nums">{formatFiatAmount(staticDepositAddress.received_usd, "USD")}</span>
                  <StaticDepositAddressHeldUpBadge staticDepositAddress={staticDepositAddress} />
                </ItemActions>
                <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
              </button>
            </Item>
          ))}
        </ItemGroup>
      )}
      {staticDepositAddresses && staticDepositAddresses.length > 0 && hasOlderAddresses ? (
        <LoadOlderButton
          id="load_older_static_deposit_addresses_button"
          isLoading={olderAddressesAreLoading}
          loadError={olderAddressesLoadError}
          onLoadOlder={onLoadOlderAddresses}
        />
      ) : null}
      <StaticDepositAddressDrawer
        staticDepositAddress={openedAddress}
        reloadKey={liveReloadCount}
        onClose={() => setOpenedAddressId(null)}
      />
    </>
  )
}
