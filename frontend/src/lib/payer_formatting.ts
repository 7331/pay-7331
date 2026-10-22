// Dates and lists on the payer's pages in the payer's language, through Intl
// (https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Intl). Amounts never come through
// here: crypto stays the exact formatted API string and fiat stays formatFiatAmount's en-US
// "$5.00" in every language, so a fiat figure never uses another decimal mark than the crypto amount beside it
// ("50,00" next to "201.946" could be misread a thousandfold). English keeps the pages' old output.
import { readPayerLanguageTag } from "@/lib/payer_language"
import { formatLocalDateTime } from "@/lib/time_formatting"

export function formatPayerDateTime(isoTimestamp: string): string {
  const payerLanguageTag = readPayerLanguageTag()
  if (payerLanguageTag === "en") return formatLocalDateTime(isoTimestamp)
  return new Date(isoTimestamp).toLocaleString(payerLanguageTag, { dateStyle: "medium", timeStyle: "short" })
}

// "1.00 USDC and 2.00 POL"; three or more: "a, b and c" in English, Intl.ListFormat's conjunction elsewhere
// (https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Intl/ListFormat).
export function formatPayerList(listItems: string[]): string {
  const payerLanguageTag = readPayerLanguageTag()
  if (payerLanguageTag === "en" || typeof Intl.ListFormat !== "function") {
    return listItems.length > 1 ? `${listItems.slice(0, -1).join(", ")} and ${listItems.at(-1)}` : (listItems[0] ?? "")
  }
  return new Intl.ListFormat(payerLanguageTag, { style: "long", type: "conjunction" }).format(listItems)
}
