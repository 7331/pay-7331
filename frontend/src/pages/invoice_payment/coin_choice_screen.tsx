// The coin pick and the coin-and-network confirmation, one screen: the amount in the invoice's
// currency, the note, and the accepted coins with what each would ask. `awaiting_asset`: a tap only selects, "Pay 5.00
// USDC on Polygon" commits (one-way on the server) and is the payer's one tap confirming coin and network
// (2026-10-12), "Back" deselects. An invoice whose coin is already fixed (a one-coin invoice, or a coin committed in
// another tab) shows that coin picked and the same button, which only confirms; nothing to deselect. Nobody sees the
// deposit address before that tap. The seller's `return_url` takes the back slot whenever "Back" has nothing to
// deselect (2026-10-21): the one back button, never beside it.
import { useState } from "react"

import { CoinChoiceRows } from "@/components/coin_choice_rows"
import { CoinNetworkPayFooter } from "@/components/coin_network_pay_footer"
import { PageColumn } from "@/components/page_column"
import { PaymentTrustLine } from "@/components/payment_trust_line"
import { RollingNumberText } from "@/components/rolling_number_text"
import { fetchApiJson } from "@/lib/api_client"
import type { PublicInvoice } from "@/lib/api_types"
import { describePayerError } from "@/lib/payer_errors"
import { formatFiatAmount } from "@/lib/amount_formatting"
import { readPayerStrings } from "@/lib/payer_language"
import { buildPaymentOptionKey, listInvoicePaymentOptions } from "@/lib/payment_options"
import { pickPayerAmountFontSizeClassName } from "@/lib/payer_amount_sizing"
import { cn } from "@/lib/utils"
import { publicInvoiceApiPath } from "@/hooks/use_invoice_polling"
import { useInvoiceMakerView } from "@/pages/invoice_payment/invoice_maker_view_context"
import {
  BackToShopLink,
  InvoiceNote,
  InvoiceStatusHeading,
  InvoiceTopBar,
  SellerSupportLine,
  StrayPaymentAlert,
} from "@/pages/invoice_payment/invoice_screen_parts"
import { describeInvoiceStatusTitle, readBackToShopUrlBeforePayment } from "@/pages/invoice_payment/invoice_view_rules"

export function InvoiceCoinChoiceScreen({
  invoice,
  onInvoiceChange,
  onCoinNetworkConfirmed,
}: {
  invoice: PublicInvoice
  onInvoiceChange(nextInvoice: PublicInvoice): void
  onCoinNetworkConfirmed(confirmedInvoice: PublicInvoice): void
}) {
  // A pair already fixed (committed in another tab, or the only one) is picked and only confirmed; else every pair on
  // offer, each with its own estimate, and nothing is picked until the payer taps.
  const { options: paymentOptions, pairIsFixed } = listInvoicePaymentOptions(invoice)
  const [pickedOptionKey, setPickedOptionKey] = useState<string | null>(null)
  // One pair on offer and nothing to choose: it starts picked, with no Back.
  const onlyOption = paymentOptions.length === 1 ? paymentOptions[0] : null
  const selectedOptionKey = onlyOption ? buildPaymentOptionKey(onlyOption) : pickedOptionKey
  const selectedOption = paymentOptions.find((option) => buildPaymentOptionKey(option) === selectedOptionKey) ?? null
  const nothingToDeselect = pairIsFixed || onlyOption !== null
  const [isCommitting, setIsCommitting] = useState(false)
  const [errorMessage, setErrorMessage] = useState("")
  // The invoice's maker previews this screen; committing a coin for good is the payer's call alone.
  const { viewerMadeThisInvoice } = useInvoiceMakerView()
  const fiatCurrency = invoice.fiat.currency
  const otherCurrency = fiatCurrency === "EUR" ? "USD" : "EUR"
  const otherAmount = otherCurrency === "USD" ? invoice.fiat.usd : invoice.fiat.eur
  const heroDecimalText = fiatCurrency && invoice.fiat.amount ? invoice.fiat.amount : invoice.fiat.usd
  const heroCurrency = fiatCurrency && invoice.fiat.amount ? fiatCurrency : "USD"

  async function payWithSelectedCoin() {
    if (!selectedOption) return
    if (pairIsFixed) {
      onCoinNetworkConfirmed(invoice)
      return
    }
    if (viewerMadeThisInvoice) return
    setIsCommitting(true)
    setErrorMessage("")
    try {
      const committedInvoice = await fetchApiJson<PublicInvoice>(publicInvoiceApiPath(invoice.id, "/asset"), {
        method: "POST",
        // The exact pair: the same coin on two networks is two options.
        body: { asset: selectedOption.asset.symbol, chain: selectedOption.chain.key },
      })
      onCoinNetworkConfirmed(committedInvoice)
      onInvoiceChange(committedInvoice)
    } catch (chooseError) {
      setErrorMessage(describePayerError(chooseError))
      setIsCommitting(false)
    }
  }

  const backToShopUrl = readBackToShopUrlBeforePayment(invoice)
  const backToShopLink = backToShopUrl ? <BackToShopLink href={backToShopUrl} /> : null

  // The maker's own line stays English, like the rest of what only the maker sees.
  const payerStrings = readPayerStrings()
  const footnoteText = selectedOption
    ? pairIsFixed
      ? payerStrings.sendOnChainOnly(selectedOption.chain.name)
      : viewerMadeThisInvoice
        ? "Your payer picks the coin."
        : payerStrings.sendOnChainOnlyNoSwitching(selectedOption.chain.name)
    : ""

  return (
    <PageColumn
      topBar={<InvoiceTopBar />}
      footer={
        selectedOption ? (
          <CoinNetworkPayFooter
            assetSymbol={selectedOption.asset.symbol}
            amountToSend={selectedOption.amount}
            chainName={selectedOption.chain.name}
            footnoteText={footnoteText}
            errorMessage={errorMessage}
            isCommitting={isCommitting}
            // The maker may confirm a fixed coin (it changes nothing on the server) to preview the send screen.
            payButtonIsDisabled={viewerMadeThisInvoice && !pairIsFixed}
            onPay={() => void payWithSelectedCoin()}
            onBack={nothingToDeselect ? null : () => setPickedOptionKey(null)}
            backWhenNothingToDeselect={backToShopLink}
          />
        ) : (
          backToShopLink
        )
      }
    >
      <section className="flex flex-col items-center pt-8 pb-6 text-center">
        {heroDecimalText ? (
          <div
            data-testid="invoice_fiat_total"
            className={cn(
              "max-w-full font-semibold tracking-tighter break-all tabular-nums",
              pickPayerAmountFontSizeClassName(formatFiatAmount(heroDecimalText, heroCurrency), "text-6xl"),
            )}
          >
            <RollingNumberText
              decimalText={heroDecimalText}
              formatDisplayText={(fiatDecimalText: string | null) => formatFiatAmount(fiatDecimalText, heroCurrency)}
            />
          </div>
        ) : null}
        {fiatCurrency && otherAmount ? (
          <div className="mt-1 text-[15px] text-muted-foreground tabular-nums">≈ {formatFiatAmount(otherAmount, otherCurrency)}</div>
        ) : null}
        <InvoiceNote invoice={invoice} className="mt-3 text-base text-foreground" />
      </section>
      {pairIsFixed ? (
        // Nothing to choose: the heading stays for screen readers and the e2e only.
        <InvoiceStatusHeading title={describeInvoiceStatusTitle(invoice)} visible={false} />
      ) : (
        <InvoiceStatusHeading title={payerStrings.statusChooseHowToPay} className="mb-2.5 px-1 text-[15px] font-medium tracking-normal text-muted-foreground" />
      )}
      <CoinChoiceRows
        options={paymentOptions}
        selectedOptionKey={selectedOptionKey}
        onSelectedOptionKeyChange={(nextOptionKey) => {
          if (!nothingToDeselect) setPickedOptionKey(nextOptionKey)
        }}
        rowsAreDisabled={isCommitting}
      />
      {/* In view before the address: a payer who sent the wrong coin must not think it vanished (INC-001). */}
      <StrayPaymentAlert invoice={invoice} className="mt-4" />
      <SellerSupportLine invoice={invoice} />
      <PaymentTrustLine chainKind={(selectedOption?.chain ?? invoice.chain).kind} />
    </PageColumn>
  )
}
