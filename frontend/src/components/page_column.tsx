// The one layout every screen uses: full-bleed on a phone, a centered card-like column on a wide screen (never
// stretched). It is an app shell (2026-10-21: the whole screen could be dragged, and the header and footer were
// not anchored): the column is fixed to the viewport and the document never scrolls, so the top bar stays at the top and `footer` (the
// screen's one or two buttons) at the bottom, and only the content between them scrolls. `overscroll-behavior: contain`
// keeps that scroll from chaining to the page; iOS Safari ignores it on a box with nothing to scroll
// (https://webkit.org/b/243452), which is why the bars sit outside the scroller instead of relying on it. A hairline
// shows under the top bar or over the footer while content is hidden there, so a row cut off at a bar reads as "more
// under here". On a short screen (a phone on its side) pinned bars would leave a sliver of content, so there the whole
// column scrolls instead (the `short-screen` variant, index.css). Signed in on `/`, `/activity` and `/settings` the
// account's tab bar closes the column under the footer, over the safe-area inset (hooks/use_account_tab_bar_is_shown.ts
// says where); every other page, the payer's above all, never gets it. Inside `PageContentRisesInContext` the content
// and footer slide in when the column mounts while the top bar stays put: the payer page swaps screens by remounting
// them, and the brand must not blink with every state change.
import { motion } from "motion/react"
import { use, type ReactNode } from "react"

import { AccountTabBar } from "@/components/account_tab_bar"
import { PageContentRisesInContext } from "@/components/page_content_rises_in_context"
import { SandboxBanner } from "@/components/sandbox_banner"
import { useAccountTabBarIsShown } from "@/hooks/use_account_tab_bar_is_shown"
import { useScrollEdgeVisibility } from "@/hooks/use_scroll_edge_visibility"
import { settleSpring } from "@/lib/motion_presets"
import { cn } from "@/lib/utils"

const contentEntranceAnimation = { initial: { opacity: 0, y: 12 }, animate: { opacity: 1, y: 0 }, transition: settleSpring }

export function PageColumn({
  topBar,
  children,
  footer,
  className,
}: {
  topBar?: ReactNode
  children: ReactNode
  footer?: ReactNode
  className?: string
}) {
  const entranceAnimation = use(PageContentRisesInContext) ? contentEntranceAnimation : {}
  const accountTabBarIsShown = useAccountTabBarIsShown()
  const { setScroller, contentIsAboveView, contentIsBelowView } = useScrollEdgeVisibility()
  const columnEndsWithContent = !footer && !accountTabBarIsShown
  return (
    <div className="fixed inset-0 flex justify-center bg-background sm:items-center sm:bg-muted/50 sm:px-6 sm:py-12 short-screen:py-0">
      <main
        className={cn(
          "flex h-full w-full max-w-[440px] flex-col overflow-hidden bg-background",
          "sm:h-[min(820px,100%)] sm:rounded-[28px] sm:border sm:shadow-xl sm:shadow-black/5",
          "short-screen:overflow-y-auto short-screen:overscroll-contain",
          className,
        )}
      >
        <div
          className={cn(
            "shrink-0 border-b px-5 pt-[max(env(safe-area-inset-top),0.75rem)] transition-colors sm:px-7",
            contentIsAboveView ? "border-border" : "border-transparent",
          )}
        >
          <SandboxBanner className="mb-1" />
          {topBar}
        </div>
        {/* Focusable so the keyboard can scroll text below the last control (Safari makes no scroller focusable). */}
        <div
          ref={setScroller}
          id="page_column_content"
          tabIndex={0}
          className={cn(
            "flex min-h-0 flex-1 flex-col overflow-x-hidden overflow-y-auto overscroll-contain px-5 outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:ring-inset sm:px-7",
            "short-screen:flex-none short-screen:overflow-visible",
            columnEndsWithContent ? "pb-[max(env(safe-area-inset-bottom),1.25rem)] sm:pb-7" : "pb-3",
          )}
        >
          <motion.div className="flex flex-1 flex-col" {...entranceAnimation}>
            {children}
          </motion.div>
        </div>
        {footer ? (
          <motion.div
            {...entranceAnimation}
            className={cn(
              "flex shrink-0 flex-col gap-2 border-t px-5 pt-3 transition-colors sm:px-7",
              contentIsBelowView ? "border-border" : "border-transparent",
              accountTabBarIsShown ? "pb-3" : "pb-[max(env(safe-area-inset-bottom),1.25rem)] sm:pb-7",
            )}
          >
            {footer}
          </motion.div>
        ) : null}
        {accountTabBarIsShown ? (
          <div className="shrink-0 border-t pb-[max(env(safe-area-inset-bottom),0.375rem)] sm:pb-2">
            <AccountTabBar />
          </div>
        ) : null}
      </main>
    </div>
  )
}
