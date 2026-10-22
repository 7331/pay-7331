// The payer's hero amounts step down a font size as they grow, so "$999,999,999.99" or a quarter-million POL with all
// its decimals stays inside a 360 px phone instead of being cut at both edges (UX pass 2026-10-11). Like `/`'s amount
// field, but sized from the formatted text's estimated width rather than its length, since grouped and long-decimal
// amounts mix wide digits and narrow punctuation. The hero also wraps (`break-all`) as a last resort.

// Tailwind's text sizes, largest first, with their pixel size.
const heroFontSizes = [
  { className: "text-6xl", pixels: 60 },
  { className: "text-5xl", pixels: 48 },
  { className: "text-4xl", pixels: 36 },
  { className: "text-3xl", pixels: 30 },
  { className: "text-2xl", pixels: 24 },
  { className: "text-xl", pixels: 20 },
] as const

// The width a 320 px phone column leaves the amount, with some air.
const heroWidthBudgetPixels = 300

// Geist semibold with tabular digits and tight tracking: digits and currency marks near 0.6 em, separators near 0.3 em.
function estimateTextWidthInEm(shownText: string): number {
  let widthInEm = 0
  for (const character of shownText) widthInEm += /[.,\s]/.test(character) ? 0.3 : 0.6
  return widthInEm
}

// The largest size, no larger than `largestClassName`, at which the text (plus `fixedExtraPixels` beside it, like a
// coin symbol) fits the column.
export function pickPayerAmountFontSizeClassName(shownText: string, largestClassName: string, fixedExtraPixels = 0): string {
  const allowedSizes = heroFontSizes.slice(Math.max(0, heroFontSizes.findIndex((size) => size.className === largestClassName)))
  const widthInEm = estimateTextWidthInEm(shownText)
  const fittingSize = allowedSizes.find((size) => widthInEm * size.pixels + fixedExtraPixels <= heroWidthBudgetPixels)
  return (fittingSize ?? allowedSizes[allowedSizes.length - 1]).className
}
