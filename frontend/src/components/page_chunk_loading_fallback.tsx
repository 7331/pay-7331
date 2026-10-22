// The router's HydrateFallback (main.tsx) while a page's lazy chunk loads on the first visit: the page background,
// blank, with no spinner to flash on a fast load.
export function PageChunkLoadingFallback() {
  return <div aria-busy="true" className="min-h-dvh bg-background" />
}
