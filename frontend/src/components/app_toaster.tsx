import { Toaster } from "@/components/ui/sonner"
import { useAccountSession } from "@/hooks/use_account_session"
import { useMediaQuery } from "@/hooks/use_media_query"

const phoneToastBottomOffset = "calc(max(env(safe-area-inset-bottom), 1.25rem) + 4.25rem)"
// Signed in, the account pages end in the tab bar (about 63 px over the safe-area inset, components/page_column.tsx)
// with the primary button 12 px above it.
const phoneToastBottomOffsetOverTabBar = "calc(max(env(safe-area-inset-bottom), 0.375rem) + 9.25rem)"

// The stock shadcn Toaster with the app's placement and theme passed in as props (components/ui/ stays what the CLI
// writes). Its own `useTheme` is next-themes', which has no provider here; "system" lets Sonner follow
// prefers-color-scheme like the pages (https://sonner.emilkowal.ski/toaster).
export function AppToaster() {
  // Phones get toasts at the bottom, where they cover no page header (the /activity total); wider screens at the top.
  const viewportIsPhone = useMediaQuery("(max-width: 639px)")
  // The Toaster sits outside the router, so it goes by the session alone: a signed-in maker's toast on a payer page
  // just sits a little higher.
  const { accountSession } = useAccountSession()
  const toastBottomOffset = accountSession ? phoneToastBottomOffsetOverTabBar : phoneToastBottomOffset

  return (
    <Toaster
      theme="system"
      position={viewportIsPhone ? "bottom-center" : "top-center"}
      // On a phone a screen's primary button sits at the bottom (PageColumn's footer: a 52 px button over the safe-area
      // padding), so toasts start just above it rather than over it. Sonner reads `mobileOffset` up to 600 px
      // and `offset` above that (https://sonner.emilkowal.ski/toaster).
      mobileOffset={{ bottom: toastBottomOffset }}
      offset={viewportIsPhone ? { bottom: toastBottomOffset } : undefined}
      toastOptions={{
        classNames: {
          // The stock Toaster's own class; these props replace its toastOptions, so it is passed again.
          toast: "cn-toast",
          // The action (the /activity "View") keeps its small look and gets a 44px hit area.
          actionButton: "relative touch-target-hit-area",
        },
      }}
    />
  )
}
