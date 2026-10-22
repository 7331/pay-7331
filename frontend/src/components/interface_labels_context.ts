// The few words the shared components say on their own (the sandbox strip, a dialog's screen-reader "Close", a
// spinner's "Loading"). English by default; the payer's pages provide them in the payer's language (lib/payer_language.ts),
// so the account pages never load the payer's strings.
import { createContext } from "react"

export interface InterfaceLabels {
  sandboxBanner: string
  closeButton: string
  loading: string
}

export const englishInterfaceLabels: InterfaceLabels = {
  sandboxBanner: "Sandbox — testnet money only",
  closeButton: "Close",
  loading: "Loading",
}

export const InterfaceLabelsContext = createContext<InterfaceLabels>(englishInterfaceLabels)
