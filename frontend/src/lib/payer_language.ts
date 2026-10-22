// The payer's pages (`/i/` and `/l/`) speak the browser's language (2026-10-21; PLAN.md
// section 10): the first of `navigator.languages` (most preferred first,
// https://developer.mozilla.org/en-US/docs/Web/API/Navigator/languages) that we have, else English. No switcher: the
// payer page keeps at most a forward and a back button. The account pages and the docs stay English and never read
// this. A small typed dictionary rather than a library: no runtime catalogue fetch, no build plugin, nothing from
// another origin (the CSP). English ships with the page; any other language is its own chunk from this origin, loaded
// by the route before the page renders, so nothing flashes in English first.
import { englishPayerStrings } from "@/lib/payer_strings/payer_strings_english"
import type { PayerStrings, TranslatedPayerStrings } from "@/lib/payer_strings/payer_strings_type"

type PayerLanguageTag = "en" | "es" | "pt-BR" | "ru" | "tr" | "zh-CN" | "de" | "fr"

const payerStringLoaders: Record<Exclude<PayerLanguageTag, "en">, () => Promise<TranslatedPayerStrings>> = {
  es: async () => (await import("@/lib/payer_strings/payer_strings_spanish")).spanishPayerStrings,
  "pt-BR": async () => (await import("@/lib/payer_strings/payer_strings_portuguese_brazil")).portugueseBrazilPayerStrings,
  ru: async () => (await import("@/lib/payer_strings/payer_strings_russian")).russianPayerStrings,
  tr: async () => (await import("@/lib/payer_strings/payer_strings_turkish")).turkishPayerStrings,
  "zh-CN": async () => (await import("@/lib/payer_strings/payer_strings_chinese_simplified")).chineseSimplifiedPayerStrings,
  de: async () => (await import("@/lib/payer_strings/payer_strings_german")).germanPayerStrings,
  fr: async () => (await import("@/lib/payer_strings/payer_strings_french")).frenchPayerStrings,
}

let activePayerLanguageTag: PayerLanguageTag = "en"
let activePayerStrings: PayerStrings = englishPayerStrings
let payerLanguageLoading: Promise<void> | null = null

function maximizeBrowserLocale(browserLanguageTag: string): Intl.Locale | null {
  try {
    return new Intl.Locale(browserLanguageTag).maximize()
  } catch {
    return null
  }
}

// One browser language tag to ours, or null. Portuguese of any region reads the Brazilian strings; Chinese only in
// the Simplified script (zh, zh-CN, zh-SG, zh-Hans): `Intl.Locale#maximize` fills in the likely script
// (https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Intl/Locale/maximize), so zh-TW
// and zh-HK (Traditional) fall through to the next preference. A bare "zh" counts as Simplified only when no
// Traditional tag sits anywhere in the list: a reader who also lists zh-TW gets English, not the other script.
function matchPayerLanguageTag(browserLanguageTag: string, browserListsTraditionalChinese: boolean): PayerLanguageTag | null {
  const browserLocale = maximizeBrowserLocale(browserLanguageTag)
  if (!browserLocale) return null
  switch (browserLocale.language) {
    case "en":
      return "en"
    case "es":
      return "es"
    case "pt":
      return "pt-BR"
    case "ru":
      return "ru"
    case "tr":
      return "tr"
    case "de":
      return "de"
    case "fr":
      return "fr"
    case "zh": {
      const tagIsBareChinese = browserLanguageTag.toLowerCase() === "zh"
      if (tagIsBareChinese && browserListsTraditionalChinese) return null
      return browserLocale.script === "Hans" ? "zh-CN" : null
    }
    default:
      return null
  }
}

function pickPayerLanguageTag(browserLanguageTags: readonly string[]): PayerLanguageTag {
  const browserListsTraditionalChinese = browserLanguageTags.some((browserLanguageTag) => {
    const browserLocale = maximizeBrowserLocale(browserLanguageTag)
    return browserLocale?.language === "zh" && browserLocale.script === "Hant"
  })
  for (const browserLanguageTag of browserLanguageTags) {
    const payerLanguageTag = matchPayerLanguageTag(browserLanguageTag, browserListsTraditionalChinese)
    if (payerLanguageTag) return payerLanguageTag
  }
  return "en"
}

function readBrowserLanguageTags(): readonly string[] {
  if (typeof navigator === "undefined") return []
  return navigator.languages?.length ? navigator.languages : [navigator.language]
}

// Called by the `/i/` and `/l/` routes before their page renders; once per page load. A language chunk that fails to
// load leaves the page in English rather than blank (main.tsx's preload-error reload skips these chunks, except in
// Safari, whose error does not name the chunk).
export function loadPayerLanguage(): Promise<void> {
  payerLanguageLoading ??= (async () => {
    const payerLanguageTag = pickPayerLanguageTag(readBrowserLanguageTags())
    if (payerLanguageTag === "en") return
    try {
      activePayerStrings = await payerStringLoaders[payerLanguageTag]()
      activePayerLanguageTag = payerLanguageTag
    } catch {
      activePayerStrings = englishPayerStrings
      activePayerLanguageTag = "en"
    }
  })()
  return payerLanguageLoading
}

export function readPayerStrings(): PayerStrings {
  return activePayerStrings
}

export function readPayerLanguageTag(): PayerLanguageTag {
  return activePayerLanguageTag
}
