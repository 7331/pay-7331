// The payer's choices are (network, coin) pairs (PLAN.md section 15): "USDC · Arbitrum" and "USDC · Polygon" are two
// options with two estimates, and one tap picks exactly one of them. Everything here keys on the pair, never on the
// symbol alone.
import type { InvoiceChain, PaymentOption, PublicInvoice } from "@/lib/api_types"

// The pair's identity: the network's key, the token's address (native coins have none) and the symbol.
export function buildPaymentOptionKey(option: Pick<PaymentOption, "chain" | "asset">): string {
  return `${option.chain.key}:${option.asset.token_address?.toLowerCase() ?? "native"}:${option.asset.symbol}`
}

// More than one network on offer: rows then name theirs ("USDC · Arbitrum"). One network keeps the plain coin rows.
export function paymentOptionsSpanNetworks(options: PaymentOption[]): boolean {
  return new Set(options.map((option) => option.chain.key)).size > 1
}

export function describePaymentOptionLabel(option: PaymentOption, showsNetwork: boolean): string {
  return showsNetwork ? `${option.asset.symbol} · ${option.chain.name}` : option.asset.symbol
}

// The e2e's handle on a row: `coin_option_USDC` on one network, `coin_option_USDC_arbitrum` across several.
export function buildPaymentOptionTestId(option: PaymentOption, showsNetwork: boolean): string {
  return showsNetwork ? `coin_option_${option.asset.symbol}_${option.chain.key}` : `coin_option_${option.asset.symbol}`
}

// Rows grouped by coin (the same coin on every network side by side), coins and networks in the server's order.
export function groupPaymentOptionsByCoin(options: PaymentOption[]): PaymentOption[] {
  const firstIndexBySymbol = new Map<string, number>()
  options.forEach((option, optionIndex) => {
    if (!firstIndexBySymbol.has(option.asset.symbol)) firstIndexBySymbol.set(option.asset.symbol, optionIndex)
  })
  return options
    .map((option, optionIndex) => ({ option, optionIndex }))
    .sort(
      (left, right) =>
        (firstIndexBySymbol.get(left.option.asset.symbol) ?? 0) - (firstIndexBySymbol.get(right.option.asset.symbol) ?? 0) ||
        left.optionIndex - right.optionIndex,
    )
    .map(({ option }) => option)
}

// What the payer picks from on `/i/`: an invoice whose pair is already fixed (committed in another tab, or one pair
// only) offers that pair alone, asking what is left to send; otherwise every pair the API offers.
export function listInvoicePaymentOptions(invoice: PublicInvoice): { options: PaymentOption[]; pairIsFixed: boolean } {
  if (invoice.asset) {
    const amountToSend = invoice.amount_remaining ?? invoice.amount_due
    return { options: [{ chain: invoice.chain, asset: invoice.asset, amount: amountToSend || null }], pairIsFixed: true }
  }
  return { options: groupPaymentOptionsByCoin(invoice.payment_options), pairIsFixed: false }
}

// How long this chain's `finalized` tag usually takes, from its `expected_finality_seconds` (about 15 minutes on
// Arbitrum, https://docs.arbitrum.io/how-arbitrum-works/reference/finality-and-reorgs): null when it is quick (under
// two minutes, Polygon's few seconds), else whole minutes, rounded up to 5 above 5. The copy says "usually" and "about";
// it never promises a time.
export function readUsualFinalityMinutes(chain: Pick<InvoiceChain, "expected_finality_seconds">): number | null {
  const waitSeconds = chain.expected_finality_seconds
  if (!(waitSeconds >= 120)) return null
  const waitMinutes = Math.ceil(waitSeconds / 60)
  return waitMinutes <= 5 ? waitMinutes : Math.ceil(waitMinutes / 5) * 5
}
