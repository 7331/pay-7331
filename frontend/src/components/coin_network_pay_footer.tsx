// The coin choice's footer on `/i/` and `/l/`: the forward button names the coin, its amount and the network ("Pay 5.00
// USDC on Polygon"), and that one tap is the payer's confirmation of coin and network before the address shows
// (2026-10-12; one screen with the pick since 2026-10-14). Under it one muted line, then "Back" while the pick can
// still change, or else the screen's own back (the seller's "Back to shop", 2026-10-21). It rises in once a coin is
// picked and sticks to the bottom, in view however many coins are listed.
import { ArrowLeft } from "lucide-react"
import type { ReactNode } from "react"

import { RiseInEntrance } from "@/components/rise_in_entrance"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"
import { formatAmountToSendForDisplay } from "@/lib/amount_formatting"
import { primaryActionButtonClassName, touchTargetButtonClassName } from "@/lib/button_size_class_names"
import { readPayerStrings } from "@/lib/payer_language"
import { cn } from "@/lib/utils"

export function CoinNetworkPayFooter({
  assetSymbol,
  amountToSend,
  chainName,
  footnoteText,
  errorMessage,
  isCommitting,
  payButtonIsDisabled,
  onPay,
  onBack,
  backWhenNothingToDeselect,
}: {
  assetSymbol: string
  // Exact decimal string; shown rounded up like every amount the payer must send. Null without a price.
  amountToSend: string | null
  chainName: string
  footnoteText: string
  errorMessage: string
  isCommitting: boolean
  payButtonIsDisabled: boolean
  onPay(): void
  // Null once the coin is fixed: nothing left to deselect.
  onBack: (() => void) | null
  // The back slot's other use when there is nothing to deselect (the payer page's "Back to shop").
  backWhenNothingToDeselect?: ReactNode
}) {
  const payerStrings = readPayerStrings()
  const payButtonText = amountToSend
    ? payerStrings.payAmountOnChain(formatAmountToSendForDisplay(amountToSend), assetSymbol, chainName)
    : payerStrings.payWithCoinOnChain(assetSymbol, chainName)
  return (
    <RiseInEntrance className="flex flex-col gap-2">
      {errorMessage ? (
        <Alert variant="destructive">
          <AlertDescription>{errorMessage}</AlertDescription>
        </Alert>
      ) : null}
      <Button
        id="pay_with_coin_button"
        className={cn(primaryActionButtonClassName, "h-auto min-h-13 w-full py-3 whitespace-normal")}
        disabled={isCommitting || payButtonIsDisabled}
        onClick={onPay}
      >
        {isCommitting ? <Spinner /> : null}
        {payButtonText}
      </Button>
      <p data-testid="coin_choice_footnote" className="text-center text-[13px] text-muted-foreground">
        {footnoteText}
      </p>
      {onBack ? (
        <Button id="coin_choice_back_button" variant="ghost" className={cn(touchTargetButtonClassName, "w-full")} disabled={isCommitting} onClick={onBack}>
          <ArrowLeft /> {payerStrings.back}
        </Button>
      ) : (
        backWhenNothingToDeselect
      )}
    </RiseInEntrance>
  )
}
