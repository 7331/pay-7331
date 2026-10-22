// `<html lang>` follows the payer page's language while it is shown (screen readers pick their voice by it, and the
// browser its hyphenation and translate offer: https://developer.mozilla.org/en-US/docs/Web/HTML/Global_attributes/lang),
// and goes back to index.html's "en" when the payer leaves for the English account pages.
import { useEffect } from "react"

import { readPayerLanguageTag } from "@/lib/payer_language"

export function usePayerDocumentLanguage(): void {
  useEffect(() => {
    document.documentElement.lang = readPayerLanguageTag()
    return () => {
      document.documentElement.lang = "en"
    }
  }, [])
}
