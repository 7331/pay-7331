// Whether a CSS media query matches now, kept current: the account sheet is a Dialog on a wide screen and a Drawer on a
// phone (shadcn's responsive pattern, https://ui.shadcn.com/docs/components/drawer).
import { useCallback, useSyncExternalStore } from "react"

export function useMediaQuery(mediaQueryText: string): boolean {
  const subscribeToMediaQuery = useCallback(
    (onMediaQueryChange: () => void) => {
      const mediaQueryList = window.matchMedia(mediaQueryText)
      mediaQueryList.addEventListener("change", onMediaQueryChange)
      return () => mediaQueryList.removeEventListener("change", onMediaQueryChange)
    },
    [mediaQueryText],
  )
  return useSyncExternalStore(subscribeToMediaQuery, () => window.matchMedia(mediaQueryText).matches)
}
