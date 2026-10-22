// Who is signed in, shared by every page and the account sheet. The context lives here and the provider in
// components/account_session_provider.tsx, so the provider file exports only a component (React Fast Refresh).
import { createContext, useContext } from "react"

import type { AccountSession } from "@/lib/api_types"

export interface AccountSessionContextValue {
  accountSession: AccountSession | null
  sessionIsLoading: boolean
  setAccountSession(accountSession: AccountSession | null): void
  refreshAccountSession(): Promise<void>
  accountSheetIsOpen: boolean
  setAccountSheetIsOpen(isOpen: boolean): void
  // Signed out the sheet opens on the passkey sign-in; "New here?" elsewhere opens it with the sign-up form shown.
  accountSheetStartsOnSignUp: boolean
  openAccountSheetOnSignUp(): void
  // A valid receiver address typed on `/`: the sign-up form starts its payout address from it ("" for none).
  signUpPayoutAddressSuggestion: string
  setSignUpPayoutAddressSuggestion(payoutAddress: string): void
}

export const AccountSessionContext = createContext<AccountSessionContextValue | null>(null)

export function useAccountSession(): AccountSessionContextValue {
  const contextValue = useContext(AccountSessionContext)
  if (!contextValue) throw new Error("useAccountSession needs an AccountSessionProvider")
  return contextValue
}
