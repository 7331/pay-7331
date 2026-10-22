// "Accept" on `/` as one tappable row: the ticked coins' logos overlapping, their symbols ("POL, USDC, USDT") and a
// chevron, with the ticked networks under them ("on Polygon, Arbitrum") once the maker has more than one to choose from.
// A tap opens a sheet (a Drawer on a phone, a Dialog on a wide screen) with the network toggles when there are several
// (PLAN.md section 15: the payer picks the network, so every EVM network starts ticked; Monero, for an account with a
// wallet, stands alone) and one coin picker per ticked network (components/accepted_coin_picker.tsx), each starting from
// that network's saved or default coins, so USDC can be accepted on Polygon alone. A coin-priced invoice is paid in that
// coin only, so the row shows it and the sheet's coin toggles are locked.
import { ChevronRight } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { AcceptedCoinPicker } from "@/components/accepted_coin_picker"
import { CoinLogo } from "@/components/coin_logo"
import { ResponsiveSheet } from "@/components/responsive_sheet"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Toggle } from "@/components/ui/toggle"
import type { ListedChain } from "@/lib/api_types"
import { primaryActionButtonClassName } from "@/lib/button_size_class_names"
import { cn } from "@/lib/utils"

const logosShownInRow = 4
const lastNetworkMessageMilliseconds = 2400

// The keys after a tap on one network, or null when the tap would leave none. Monero never mixes with EVM networks.
function readNetworkKeysAfterTap(
  makerNetworks: ListedChain[],
  selectedNetworks: ListedChain[],
  tappedNetwork: ListedChain,
  isPressed: boolean,
): string[] | null {
  if (tappedNetwork.kind === "monero") return isPressed ? [tappedNetwork.key] : null
  const selectedEvmKeys = selectedNetworks.filter((network) => network.kind === "evm").map((network) => network.key)
  const nextKeys = new Set(isPressed ? [...selectedEvmKeys, tappedNetwork.key] : selectedEvmKeys.filter((key) => key !== tappedNetwork.key))
  const nextOrderedKeys = makerNetworks.filter((network) => nextKeys.has(network.key)).map((network) => network.key)
  return nextOrderedKeys.length > 0 ? nextOrderedKeys : null
}

export function AcceptedCoinsRow({
  makerNetworks,
  selectedNetworks,
  acceptingNetworks,
  onSelectedNetworkKeysChange,
  acceptedSymbolsByNetworkKey,
  onNetworkAcceptedSymbolsChange,
  lockedToSymbol,
}: {
  // Every network the maker can use; null while the chain list loads.
  makerNetworks: ListedChain[] | null
  // The networks the maker ticked, and the ones the link goes to: a coin-priced link only to those listing that coin.
  selectedNetworks: ListedChain[]
  acceptingNetworks: ListedChain[]
  onSelectedNetworkKeysChange(nextNetworkKeys: string[]): void
  // The coins ticked on each ticked network, by network key.
  acceptedSymbolsByNetworkKey: Record<string, string[]>
  onNetworkAcceptedSymbolsChange(networkKey: string, nextSymbols: string[]): void
  lockedToSymbol: string | null
}) {
  const [coinSheetIsOpen, setCoinSheetIsOpen] = useState(false)
  const [showsLastNetworkMessage, setShowsLastNetworkMessage] = useState(false)
  const lastNetworkMessageTimer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(lastNetworkMessageTimer.current), [])
  if (!makerNetworks) return <Skeleton className="h-12 w-full rounded-xl" />
  const choosesNetworks = makerNetworks.length > 1
  const acceptedSymbols = [...new Set(acceptingNetworks.flatMap((network) => acceptedSymbolsByNetworkKey[network.key] ?? []))]
  const shownLogoSymbols = acceptedSymbols.slice(0, logosShownInRow)
  // Equal ticks everywhere read "on Polygon, Arbitrum"; differing ones say which coins sit on which network.
  const everyNetworkAcceptsTheSame = acceptingNetworks.every(
    (network) => (acceptedSymbolsByNetworkKey[network.key] ?? []).join() === (acceptedSymbolsByNetworkKey[acceptingNetworks[0].key] ?? []).join(),
  )
  const networksSummary = everyNetworkAcceptsTheSame
    ? `on ${acceptingNetworks.map((network) => network.name).join(", ")}`
    : acceptingNetworks.map((network) => `${network.name}: ${(acceptedSymbolsByNetworkKey[network.key] ?? []).join(", ")}`).join(" · ")

  function changeNetwork(tappedNetwork: ListedChain, isPressed: boolean) {
    const nextNetworkKeys = lockedToSymbol
      ? (isPressed ? [tappedNetwork.key] : null)
      : readNetworkKeysAfterTap(makerNetworks ?? [], selectedNetworks, tappedNetwork, isPressed)
    if (nextNetworkKeys) {
      setShowsLastNetworkMessage(false)
      onSelectedNetworkKeysChange(nextNetworkKeys)
      return
    }
    window.clearTimeout(lastNetworkMessageTimer.current)
    setShowsLastNetworkMessage(true)
    lastNetworkMessageTimer.current = window.setTimeout(() => setShowsLastNetworkMessage(false), lastNetworkMessageMilliseconds)
  }

  return (
    <>
      <button
        id="accepted_coins_button"
        type="button"
        aria-label={`Accept ${acceptedSymbols.join(", ")}${choosesNetworks ? ` (${networksSummary})` : ""}`}
        onClick={() => setCoinSheetIsOpen(true)}
        className={cn(
          "flex min-h-12 w-full items-center gap-3 rounded-xl border border-input px-3 py-1.5 text-left text-[15px] font-medium outline-none",
          "transition-colors hover:bg-muted/60 focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50",
        )}
      >
        <span aria-hidden="true" className="flex shrink-0 -space-x-2">
          {shownLogoSymbols.map((assetSymbol) => (
            <CoinLogo key={assetSymbol} assetSymbol={assetSymbol} className="size-7 ring-2 ring-background" />
          ))}
        </span>
        <span className="flex min-w-0 flex-1 flex-col">
          <span id="accepted_coins_summary" className="truncate">
            {acceptedSymbols.join(", ")}
          </span>
          {choosesNetworks ? (
            <span id="accepted_networks_summary" className="truncate text-xs font-normal text-muted-foreground">
              {networksSummary}
            </span>
          ) : null}
        </span>
        <ChevronRight aria-hidden="true" className="size-5 shrink-0 text-muted-foreground" />
      </button>
      <ResponsiveSheet isOpen={coinSheetIsOpen} onOpenChange={setCoinSheetIsOpen} title="Accept" sheetId="accepted_coins_sheet">
        {choosesNetworks ? (
          <div className="mb-5 flex flex-col gap-2">
            <div className="flex flex-wrap gap-2" role="group" aria-label="Networks the payer may use">
              {makerNetworks.map((network) => (
                <Toggle
                  key={network.key}
                  data-testid={`accepted_network_toggle_${network.key}`}
                  variant="outline"
                  pressed={selectedNetworks.some((selectedNetwork) => selectedNetwork.key === network.key)}
                  onPressedChange={(isPressed) => changeNetwork(network, isPressed)}
                  aria-label={`Accept payments on ${network.name}`}
                  className="h-11 rounded-full px-4 text-[15px] font-medium data-[state=off]:text-muted-foreground data-[state=on]:border-foreground data-[state=on]:bg-transparent"
                >
                  {network.name}
                </Toggle>
              ))}
            </div>
            <p id="accepted_network_last_message" role="status" aria-live="polite" className="text-sm text-destructive">
              {showsLastNetworkMessage ? "Keep at least one network." : ""}
            </p>
          </div>
        ) : null}
        <div className="flex flex-col gap-5">
          {acceptingNetworks.map((network) => (
            <div key={network.key} className="flex flex-col gap-2">
              {acceptingNetworks.length > 1 ? <p className="px-1 text-sm font-medium text-muted-foreground">{network.name}</p> : null}
              <AcceptedCoinPicker
                chainAssets={network.assets}
                networkKey={acceptingNetworks.length > 1 ? network.key : undefined}
                networkName={acceptingNetworks.length > 1 ? network.name : undefined}
                acceptedSymbols={acceptedSymbolsByNetworkKey[network.key] ?? []}
                onAcceptedSymbolsChange={(nextSymbols) => onNetworkAcceptedSymbolsChange(network.key, nextSymbols)}
                lockedToSymbol={lockedToSymbol}
              />
            </div>
          ))}
        </div>
        <Button
          id="accepted_coins_done_button"
          type="button"
          variant="secondary"
          className={cn(primaryActionButtonClassName, "mt-4 w-full")}
          onClick={() => setCoinSheetIsOpen(false)}
        >
          Done
        </Button>
      </ResponsiveSheet>
    </>
  )
}
