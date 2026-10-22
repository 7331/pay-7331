// Amounts are decimal strings from the API. Base-unit math is BigInt string math, never floats; display rounds to at
// most six places and shows at least two ("5" reads "5.00").

const plainDecimalPattern = /^\d+(\.\d+)?$/

export function isPlainDecimalString(text: string): boolean {
  return plainDecimalPattern.test(text.trim())
}

// "1.5" with 18 decimals gives 1500000000000000000n.
export function convertDecimalToBaseUnits(decimalText: string, decimals: number): bigint {
  const trimmedDecimal = decimalText.trim()
  if (!plainDecimalPattern.test(trimmedDecimal)) throw new Error(`Not a plain decimal amount: ${decimalText}`)
  const [wholePart, fractionPart = ""] = trimmedDecimal.split(".")
  const fractionWithoutTrailingZeros = fractionPart.replace(/0+$/, "")
  if (fractionWithoutTrailingZeros.length > decimals) throw new Error(`More than ${decimals} decimal places: ${decimalText}`)
  return BigInt(wholePart + fractionWithoutTrailingZeros.padEnd(decimals, "0"))
}

// 1500000000000000000n with 18 decimals gives "1.5".
export function convertBaseUnitsToDecimal(baseUnits: bigint, decimals: number): string {
  const isNegative = baseUnits < 0n
  const digits = (isNegative ? -baseUnits : baseUnits).toString().padStart(decimals + 1, "0")
  const wholePart = digits.slice(0, digits.length - decimals)
  const fractionPart = digits.slice(digits.length - decimals).replace(/0+$/, "")
  return (isNegative ? "-" : "") + wholePart + (fractionPart ? `.${fractionPart}` : "")
}

// The most fraction digits an amount shows (2026-10-11); copies and links keep the exact API string.
const maximumDisplayedDecimalPlaces = 6

// "12345.678" reads "12,345.678"; "5" reads "5.00"; "40.000066726951455907" reads "40.000067" (half up, BigInt). A
// shorter fraction keeps its own width, trailing zeros included, so amounts padded to the same places stay aligned.
export function formatDecimalForDisplay(decimalText: string | null | undefined): string {
  return formatDecimalWithRounding(decimalText, "half_up")
}

// An amount the payer must send rounds up, never down: someone who types the shown figure instead of copying must
// not come up short (WBTC quotes 7 places; display stops at 6). Copy still gives the exact API string (2026-10-11).
export function formatAmountToSendForDisplay(decimalText: string | null | undefined): string {
  return formatDecimalWithRounding(decimalText, "up")
}

function formatDecimalWithRounding(decimalText: string | null | undefined, rounding: "half_up" | "up"): string {
  if (decimalText == null) return ""
  if (!isPlainDecimalString(decimalText)) return decimalText
  let [wholePart, fractionPart = ""] = decimalText.trim().split(".")
  if (fractionPart.length > maximumDisplayedDecimalPlaces) {
    const droppedDigits = fractionPart.slice(maximumDisplayedDecimalPlaces)
    const roundsUp = rounding === "up" ? /[1-9]/.test(droppedDigits) : droppedDigits[0] >= "5"
    const scaledAmount = BigInt(wholePart + fractionPart.slice(0, maximumDisplayedDecimalPlaces)) + (roundsUp ? 1n : 0n)
    const scaledDigits = scaledAmount.toString().padStart(maximumDisplayedDecimalPlaces + 1, "0")
    wholePart = scaledDigits.slice(0, -maximumDisplayedDecimalPlaces)
    fractionPart = scaledDigits.slice(-maximumDisplayedDecimalPlaces)
  }
  return `${BigInt(wholePart).toLocaleString("en-US")}.${fractionPart.padEnd(2, "0")}`
}

// Fiat arrives as 2-decimal strings; Number is exact enough to show cents.
export function formatFiatAmount(amountText: string | null | undefined, currencyCode: string): string {
  if (amountText == null) return ""
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency: currencyCode }).format(Number(amountText))
  } catch {
    return `${amountText} ${currencyCode}`
  }
}

// "0x1234567890abcdef…" reads "0x1234…cdef".
export function shortenHexString(hexText: string | null | undefined, keptCharacters = 4): string {
  if (!hexText) return ""
  if (hexText.length <= 2 + keptCharacters * 2 + 1) return hexText
  return `${hexText.slice(0, 2 + keptCharacters)}…${hexText.slice(-keptCharacters)}`
}

// A Monero address (95 characters, a subaddress too) reads the way Monero wallets shorten it: "8Bx4k2…Yq7rWd".
export function shortenMoneroAddress(moneroAddress: string | null | undefined): string {
  if (!moneroAddress) return ""
  if (moneroAddress.length <= 6 + 6 + 1) return moneroAddress
  return `${moneroAddress.slice(0, 6)}…${moneroAddress.slice(-6)}`
}

export const addressPattern = /^0x[0-9a-fA-F]{40}$/
