// The signed-in account's bottom tab bar: New (`/`), Activity and Settings, each a lucide icon over
// a short label, at least 44 px tall, the current one marked with `aria-current="page"` and a pill that slides between
// tabs (https://motion.dev/docs/react-layout-animations). PageColumn puts it at the bottom of the column, over the
// phone's safe-area inset, where hooks/use_account_tab_bar_is_shown.ts says: these three routes, signed in.
import { Plus, Rows3, Settings } from "lucide-react"
import { motion } from "motion/react"
import { Link, useLocation } from "react-router"

import { accountTabPaths } from "@/hooks/use_account_tab_bar_is_shown"
import { settleSpring } from "@/lib/motion_presets"
import { cn } from "@/lib/utils"

const accountTabs: { path: (typeof accountTabPaths)[number]; id: string; label: string; icon: typeof Plus }[] = [
  { path: "/", id: "account_tab_new", label: "New", icon: Plus },
  { path: "/activity", id: "account_tab_activity", label: "Activity", icon: Rows3 },
  { path: "/settings", id: "account_tab_settings", label: "Settings", icon: Settings },
]

export function AccountTabBar() {
  const { pathname } = useLocation()
  return (
    <nav aria-label="Account" className="grid grid-cols-3 gap-1 px-2 pt-1.5">
      {accountTabs.map((accountTab) => {
        const isCurrentTab = accountTab.path === pathname
        const TabIcon = accountTab.icon
        return (
          <Link
            key={accountTab.path}
            id={accountTab.id}
            to={accountTab.path}
            aria-current={isCurrentTab ? "page" : undefined}
            className={cn(
              "relative flex min-h-14 flex-col items-center justify-center gap-0.5 rounded-2xl text-xs font-medium outline-none",
              "transition-colors focus-visible:ring-[3px] focus-visible:ring-ring/50",
              isCurrentTab ? "text-foreground" : "text-muted-foreground hover:text-foreground",
            )}
          >
            {isCurrentTab ? (
              <motion.span
                layoutId="account_tab_current_pill"
                aria-hidden="true"
                transition={settleSpring}
                style={{ borderRadius: 16 }}
                className="absolute inset-0 bg-muted"
              />
            ) : null}
            <TabIcon className="relative size-5" strokeWidth={isCurrentTab ? 2.4 : 2} aria-hidden="true" />
            <span className="relative">{accountTab.label}</span>
          </Link>
        )
      })}
    </nav>
  )
}
