// How the amount on `/` reads in its currency (invoice_options.ts has the currency): "$" or "€" before it with two
// decimals and the $1/$2/$5/$10 chips, or the coin's symbol after it with that coin's decimals (18 at most) and no
// chips; and the sentence for anything typed beyond them.
import type { ChainAsset } from "@/lib/api_types"

const quickFiatAmounts = ["1", "2", "5", "10"]
const fiatSignByCurrencyCode: Record<string, string> = { USD: "$", EUR: "€" }
const fiatSpokenNameByCurrencyCode: Record<string, string> = { USD: "US dollars", EUR: "euros" }
const fiatCutMessageByCurrencyCode: Record<string, string> = {
  USD: "Dollars and cents only, up to 999,999,999.99.",
  EUR: "Euros and cents only, up to 999,999,999.99.",
}

export interface AmountUnit {
  sign: string | null
  symbol: string | null
  fractionDigits: number
  spokenName: string
  cutMessage: string
  quickAmounts: string[]
}

export function describeAmountUnit(currencyCode: string, chainAssets: ChainAsset[] | null): AmountUnit {
  const fiatSign = fiatSignByCurrencyCode[currencyCode]
  if (fiatSign) {
    return {
      sign: fiatSign,
      symbol: null,
      fractionDigits: 2,
      spokenName: fiatSpokenNameByCurrencyCode[currencyCode],
      cutMessage: fiatCutMessageByCurrencyCode[currencyCode],
      quickAmounts: quickFiatAmounts,
    }
  }
  // The same symbol on two networks may count in different decimals: the fewest keeps the amount valid on both.
  const coinDecimalCounts = (chainAssets ?? []).filter((asset) => asset.symbol === currencyCode).map((asset) => asset.decimals)
  const coinDecimals = Math.min(...coinDecimalCounts, 18)
  return {
    sign: null,
    symbol: currencyCode,
    fractionDigits: coinDecimals,
    spokenName: currencyCode,
    cutMessage: `Up to 999,999,999 ${currencyCode}, with at most ${coinDecimals} decimals.`,
    quickAmounts: [],
  }
}
