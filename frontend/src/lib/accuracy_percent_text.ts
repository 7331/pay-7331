// An accepted-shortfall field's text as the API reads it. The field is a text field (`inputMode="decimal"`), not
// `type="number"`: a number field the browser cannot parse ("0,5" where the browser's locale writes a dot) reads as
// "", which would quietly mean "the network's default". A shortfall is at most 5 with two decimals, so it has no digit
// grouping, and one "," is its decimal comma.
export function normalizeAccuracyPercentText(percentText: string): string {
  return percentText.trim().replace(",", ".")
}
