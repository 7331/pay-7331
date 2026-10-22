// The payer's coin rows, one card per accepted coin with what it would ask now: `/i/` while `awaiting_asset` (or its
// one fixed coin, picked, until the payer confirms it) and a quantity link's `/l/`. A shadcn RadioGroup styled as cards, so the list is one keyboard stop with arrow keys moving
// the pick (UX pass 2026-10-11); a tap only selects, the screen's forward button commits and its Back deselects. The
// picked row scrolls into view above the footer that appears with it.
import { motion } from "motion/react"
import { useEffect, useState } from "react"

import { CoinLogo } from "@/components/coin_logo"
import { Field, FieldContent, FieldDescription, FieldLabel, FieldTitle } from "@/components/ui/field"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import { formatAmountToSendForDisplay } from "@/lib/amount_formatting"
import type { PaymentOption } from "@/lib/api_types"
import { readPayerStrings } from "@/lib/payer_language"
import { popSpring, settleSpring } from "@/lib/motion_presets"
import {
  buildPaymentOptionKey,
  buildPaymentOptionTestId,
  describePaymentOptionLabel,
  paymentOptionsSpanNetworks,
} from "@/lib/payment_options"
import { cn } from "@/lib/utils"

function buildCoinOptionInputId(optionKey: string): string {
  return `coin_option_input_${optionKey.replace(/[^A-Za-z0-9]/g, "_")}`
}

export function CoinChoiceRows({
  options,
  selectedOptionKey,
  onSelectedOptionKeyChange,
  rowsAreDisabled,
}: {
  // Each row is one (network, coin) pair with its own estimate; a pick is the pair's key (lib/payment_options.ts).
  options: PaymentOption[]
  selectedOptionKey: string | null
  onSelectedOptionKeyChange(nextSelectedOptionKey: string | null): void
  rowsAreDisabled: boolean
}) {
  const [coinRowsHaveEntered, setCoinRowsHaveEntered] = useState(false)
  // Across several networks a row names its own ("USDC · Arbitrum"); on one it stays the plain coin.
  const rowsNameTheirNetwork = paymentOptionsSpanNetworks(options)

  useEffect(() => {
    if (!selectedOptionKey) return
    document.getElementById(buildCoinOptionInputId(selectedOptionKey))?.closest("label")?.scrollIntoView({ block: "nearest" })
  }, [selectedOptionKey])

  return (
    <RadioGroup
      aria-label={readPayerStrings().payWithGroupLabel}
      value={selectedOptionKey ?? ""}
      onValueChange={onSelectedOptionKeyChange}
      disabled={rowsAreDisabled}
      // One minmax(0, 1fr) column: an auto track grows to a row's widest content and pushes past a 360 px phone.
      className="grid-cols-1 gap-2"
    >
      {options.map((option, coinIndex) => {
        const optionKey = buildPaymentOptionKey(option)
        const coinName = option.asset.name
        const isSelected = selectedOptionKey === optionKey
        const coinOptionInputId = buildCoinOptionInputId(optionKey)
        const rowLabel = describePaymentOptionLabel(option, rowsNameTheirNetwork)
        return (
          <motion.div
            key={optionKey}
            className="min-w-0"
            initial={{ opacity: 0, y: 12 }}
            // Unpicked rows stay at full strength: dimmed, they read as disabled (2026-10-11 walkthrough).
            animate={{ opacity: 1, y: 0, scale: isSelected ? 1.015 : 1 }}
            // Rows stagger in once; after that a pick springs at once, without the stagger delay.
            transition={{ default: { ...settleSpring, delay: coinRowsHaveEntered ? 0 : coinIndex * 0.04 }, scale: popSpring }}
            onAnimationComplete={() => {
              if (coinIndex === options.length - 1) setCoinRowsHaveEntered(true)
            }}
          >
            <FieldLabel
              htmlFor={coinOptionInputId}
              className={cn(
                // A card: the whole row picks the pair.
                "rounded-2xl bg-card transition-colors hover:bg-muted/60 active:bg-muted",
                // The stock FieldLabel's own `has-data-checked:` and `dark:has-data-checked:` colours, replaced (cn merges them).
                "has-data-checked:border-foreground has-data-checked:bg-card has-data-checked:ring-1 has-data-checked:ring-foreground dark:has-data-checked:border-foreground dark:has-data-checked:bg-card",
                "has-[>[data-slot=field]]:rounded-2xl *:data-[slot=field]:px-3.5 *:data-[slot=field]:py-2.5",
              )}
            >
              <Field orientation="horizontal" className="min-h-16 gap-3 has-[>[data-slot=field-content]]:items-center">
                <CoinLogo assetSymbol={option.asset.symbol} />
                <FieldContent className="min-w-0">
                  <FieldTitle className="text-base font-semibold">{rowLabel}</FieldTitle>
                  {coinName !== option.asset.symbol ? <FieldDescription className="truncate">{coinName}</FieldDescription> : null}
                </FieldContent>
                {option.amount ? (
                  <span className="font-mono text-[15px] font-normal tabular-nums">{formatAmountToSendForDisplay(option.amount)}</span>
                ) : null}
                <RadioGroupItem
                  id={coinOptionInputId}
                  value={optionKey}
                  data-testid={buildPaymentOptionTestId(option, rowsNameTheirNetwork)}
                  data-chain-key={option.chain.key}
                  className="size-6 border-2 border-muted-foreground/40 data-checked:border-foreground data-checked:bg-foreground [&_[data-slot=radio-group-indicator]]:size-6 [&_[data-slot=radio-group-indicator]_span]:bg-background"
                />
              </Field>
            </FieldLabel>
          </motion.div>
        )
      })}
    </RadioGroup>
  )
}
