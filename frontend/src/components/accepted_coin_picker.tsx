// "Accept": the chain's coins as logo toggles, in the server's order. The API says which start ticked: the account's
// saved coins signed in, else the chain's `default_accepted_assets` (POL, USDC, USDT); the rest sit one tap away
// behind "+N more". At least one stays ticked: unticking the last one shakes it and says so under the chips.
// Unticked chips are greyed (logo in grayscale, muted text), so ticked ones stand out at a glance (2026-10-11).
import { Check, ChevronDown } from "lucide-react"
import { AnimatePresence, motion, useAnimate, useReducedMotion } from "motion/react"
import { useEffect, useRef, useState } from "react"

import { CoinLogo } from "@/components/coin_logo"
import { Toggle } from "@/components/ui/toggle"
import type { ChainAsset } from "@/lib/api_types"
import { popSpring } from "@/lib/motion_presets"
import { cn } from "@/lib/utils"

const coinsShownBeforeExpanding = 4
const lastCoinMessageMilliseconds = 2400
// A short sideways "no": https://motion.dev/docs/react-use-animate
const refusalShakeKeyframes = [0, -6, 6, -4, 4, 0]

export function AcceptedCoinPicker({
  chainAssets,
  networkKey,
  networkName,
  acceptedSymbols,
  onAcceptedSymbolsChange,
  lockedToSymbol,
}: {
  chainAssets: ChainAsset[]
  // Set when `/` ticks coins on several networks at once: their toggles' test ids then end in the network's key.
  networkKey?: string
  networkName?: string
  acceptedSymbols: string[]
  onAcceptedSymbolsChange(nextSymbols: string[]): void
  // A coin-denominated invoice is paid in that coin only.
  lockedToSymbol?: string | null
}) {
  const [showsEveryCoin, setShowsEveryCoin] = useState(false)
  const [showsLastCoinMessage, setShowsLastCoinMessage] = useState(false)
  const lastCoinMessageTimer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(lastCoinMessageTimer.current), [])
  const [coinPickerScope, animateInCoinPicker] = useAnimate<HTMLDivElement>()
  const prefersReducedMotion = useReducedMotion()
  const firstCoins = chainAssets.slice(0, coinsShownBeforeExpanding)
  const tickedOutsideFirstCoins = chainAssets.slice(coinsShownBeforeExpanding).filter((asset) => acceptedSymbols.includes(asset.symbol))
  const visibleCoins = showsEveryCoin ? chainAssets : [...firstCoins, ...tickedOutsideFirstCoins]
  const hiddenCoinCount = chainAssets.length - visibleCoins.length
  const buildToggleTestId = (assetSymbol: string) => `accepted_coin_toggle_${assetSymbol}${networkKey ? `_${networkKey}` : ""}`

  function toggleCoin(assetSymbol: string, isPressed: boolean) {
    const nextSymbols = chainAssets
      .map((asset) => asset.symbol)
      .filter((symbol) => (symbol === assetSymbol ? isPressed : acceptedSymbols.includes(symbol)))
    if (nextSymbols.length > 0) {
      setShowsLastCoinMessage(false)
      onAcceptedSymbolsChange(nextSymbols)
      return
    }
    refuseUntickingLastCoin(assetSymbol)
  }

  function refuseUntickingLastCoin(assetSymbol: string) {
    window.clearTimeout(lastCoinMessageTimer.current)
    setShowsLastCoinMessage(true)
    lastCoinMessageTimer.current = window.setTimeout(() => setShowsLastCoinMessage(false), lastCoinMessageMilliseconds)
    if (prefersReducedMotion) return
    void animateInCoinPicker(`[data-testid="${buildToggleTestId(assetSymbol)}"]`, { x: refusalShakeKeyframes }, { duration: 0.35 })
  }

  return (
    <div className="flex flex-col">
      <div ref={coinPickerScope} className="flex flex-wrap gap-2" role="group" aria-label={networkName ? `Coins the payer may use on ${networkName}` : "Coins the payer may use"}>
        {/* "+N more" grows the row: chips already shown glide to their new places, new ones pop in
            (https://motion.dev/docs/react-layout-animations). */}
        <AnimatePresence initial={false} mode="popLayout">
          {visibleCoins.map((asset) => {
            const isAccepted = acceptedSymbols.includes(asset.symbol)
            return (
              <motion.div
                key={asset.symbol}
                layout
                initial={{ opacity: 0, scale: 0.85 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.85 }}
                transition={popSpring}
              >
                <Toggle
                  data-testid={buildToggleTestId(asset.symbol)}
                  variant="outline"
                  pressed={isAccepted}
                  disabled={Boolean(lockedToSymbol)}
                  onPressedChange={(isPressed) => toggleCoin(asset.symbol, isPressed)}
                  aria-label={`Accept ${asset.symbol}`}
                  className={cn(
                    "h-11 gap-2 rounded-full pr-3.5 pl-1.5 text-[15px] font-medium data-[state=on]:border-foreground data-[state=on]:bg-transparent",
                    "data-[state=off]:text-muted-foreground",
                    "disabled:opacity-100 data-[state=off]:disabled:opacity-40",
                  )}
                >
                  <span className="relative">
                    <CoinLogo
                      assetSymbol={asset.symbol}
                      className={cn("size-8 transition-[filter,opacity] duration-200", !isAccepted && "opacity-60 grayscale")}
                    />
                    <AnimatePresence initial={false}>
                      {isAccepted ? (
                        <motion.span
                          initial={{ scale: 0, opacity: 0 }}
                          animate={{ scale: 1, opacity: 1 }}
                          exit={{ scale: 0, opacity: 0, transition: { duration: 0.12 } }}
                          transition={popSpring}
                          className="absolute -right-1 -bottom-1 grid size-4 place-items-center rounded-full bg-foreground text-background ring-2 ring-background"
                        >
                          <Check className="size-2.5" strokeWidth={3.5} />
                        </motion.span>
                      ) : null}
                    </AnimatePresence>
                  </span>
                  {asset.symbol}
                </Toggle>
              </motion.div>
            )
          })}
          {hiddenCoinCount > 0 && !lockedToSymbol ? (
            <motion.div key="accepted_coin_show_more" layout exit={{ opacity: 0, scale: 0.85 }} transition={popSpring}>
              <Toggle
                variant="outline"
                pressed={false}
                onPressedChange={() => setShowsEveryCoin(true)}
                className="h-11 rounded-full px-3.5 text-[15px] text-muted-foreground"
                data-testid="accepted_coin_show_more"
              >
                +{hiddenCoinCount} more <ChevronDown />
              </Toggle>
            </motion.div>
          ) : null}
        </AnimatePresence>
      </div>
      <p id={`accepted_coin_last_coin_message${networkKey ? `_${networkKey}` : ""}`} role="status" aria-live="polite" className={cn("text-sm text-destructive", showsLastCoinMessage && "mt-2")}>
        {showsLastCoinMessage ? "Keep at least one coin." : ""}
      </p>
    </div>
  )
}
