// Holds who is signed in for every page. The cookie is HttpOnly, so the app learns it from
// GET /api/v1/sessions/current once at start and after every sign-in, sign-up or sign-out.
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react"

import { AccountSessionContext } from "@/hooks/use_account_session"
import type { AccountSession } from "@/lib/api_types"
import { readCurrentAccountSession } from "@/lib/passkey_access"

export function AccountSessionProvider({ children }: { children: ReactNode }) {
  const [accountSession, setAccountSession] = useState<AccountSession | null>(null)
  const [sessionIsLoading, setSessionIsLoading] = useState(true)
  const [accountSheetIsOpen, setAccountSheetIsOpen] = useState(false)
  const [accountSheetStartsOnSignUp, setAccountSheetStartsOnSignUp] = useState(false)
  const [signUpPayoutAddressSuggestion, setSignUpPayoutAddressSuggestion] = useState("")

  // The state changes in the promise callbacks, once the server answers, never synchronously in the effect below.
  const refreshAccountSession = useCallback(
    () =>
      readCurrentAccountSession()
        .then(setAccountSession, () => setAccountSession(null))
        .finally(() => setSessionIsLoading(false)),
    [],
  )

  useEffect(() => {
    void refreshAccountSession()
  }, [refreshAccountSession])

  const changeAccountSheetIsOpen = useCallback((isOpen: boolean) => {
    setAccountSheetIsOpen(isOpen)
    if (!isOpen) setAccountSheetStartsOnSignUp(false)
  }, [])
  const openAccountSheetOnSignUp = useCallback(() => {
    setAccountSheetStartsOnSignUp(true)
    setAccountSheetIsOpen(true)
  }, [])

  const contextValue = useMemo(
    () => ({
      accountSession,
      sessionIsLoading,
      setAccountSession,
      refreshAccountSession,
      accountSheetIsOpen,
      setAccountSheetIsOpen: changeAccountSheetIsOpen,
      accountSheetStartsOnSignUp,
      openAccountSheetOnSignUp,
      signUpPayoutAddressSuggestion,
      setSignUpPayoutAddressSuggestion,
    }),
    [
      accountSession,
      sessionIsLoading,
      refreshAccountSession,
      accountSheetIsOpen,
      changeAccountSheetIsOpen,
      accountSheetStartsOnSignUp,
      openAccountSheetOnSignUp,
      signUpPayoutAddressSuggestion,
    ],
  )
  return <AccountSessionContext.Provider value={contextValue}>{children}</AccountSessionContext.Provider>
}
