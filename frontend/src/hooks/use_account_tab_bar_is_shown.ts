// Where the signed-in account's tab bar (components/account_tab_bar.tsx) shows: on `/`, `/activity` and `/settings`,
// and only signed in. PageColumn asks, so no page opts in by itself and a payer page (/i/, /l/) never gets it.
import { useLocation } from "react-router"

import { useAccountSession } from "@/hooks/use_account_session"

export const accountTabPaths = ["/", "/activity", "/settings"] as const

export function useAccountTabBarIsShown(): boolean {
  const { accountSession } = useAccountSession()
  const { pathname } = useLocation()
  return accountSession !== null && (accountTabPaths as readonly string[]).includes(pathname)
}
