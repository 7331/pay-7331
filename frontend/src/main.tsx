// The one app behind every page route (PLAN.md section 10): the API answers `/`, `/new`, `/activity`, `/settings`, `/docs`,
// `/enroll-passkey`, `/i/{invoice_id}` and `/l/{payment_link_id}` with dist/index.html and the router picks the screen here. `/new`, the
// old account form, only redirects to `/` with its options sheet open, for old bookmarks.
import { MotionConfig } from "motion/react"
import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { createBrowserRouter, redirect, RouterProvider } from "react-router"

import { AccountSessionProvider } from "@/components/account_session_provider"
import { AppToaster } from "@/components/app_toaster"
import { PageChunkLoadingFallback } from "@/components/page_chunk_loading_fallback"
import "./index.css"

// The payer's words load only on the payer routes, so the account pages never carry them.
async function loadPayerLanguageForPayerRoute(): Promise<void> {
  await (await import("@/lib/payer_language")).loadPayerLanguage()
}

// A deploy replaces every hashed chunk, so a tab opened before it fails to load the next page's chunk: reload to pick
// up the new index.html (https://vite.dev/guide/build#load-error-handling, 2026-10-11). It reloads on every such
// failure, not just once. A payer language chunk is the exception: its failure reaches lib/payer_language.ts, which
// shows the page in English instead. Chromium and Firefox name the chunk's URL in the error; Safari's "Importing a
// module script failed." does not, so there it still reloads.
window.addEventListener("vite:preloadError", (preloadErrorEvent) => {
  if (String(preloadErrorEvent.payload?.message).includes("/payer_strings_")) return
  preloadErrorEvent.preventDefault()
  window.location.reload()
})

// Each page is its own chunk, so a payer on /i/... never downloads the account pages
// (https://reactrouter.com/start/data/route-object#lazy). The payer routes also load the payer's language (a chunk of
// its own, https://vite.dev/guide/features#dynamic-import) alongside the page, so the page renders in it at once.
// The pathless root route only carries the HydrateFallback: on the first load the matched page's lazy chunk is still
// on its way and the router renders the nearest ancestor's fallback (https://reactrouter.com/explanation/hydration);
// without one it renders nothing and warns "No `HydrateFallback` element provided" on every page. The root has no
// Component, so it renders its matched child through an Outlet.
const applicationRouter = createBrowserRouter([
  {
    HydrateFallback: PageChunkLoadingFallback,
    children: [
      { path: "/", lazy: async () => ({ Component: (await import("@/pages/invoice_creation/invoice_creation_page")).InvoiceCreationPage }) },
      { path: "/docs", lazy: async () => ({ Component: (await import("@/pages/developer_docs/developer_docs_page")).DeveloperDocsPage }) },
      // https://reactrouter.com/api/utils/redirect
      { path: "/new", loader: () => redirect("/?options=1") },
      {
        path: "/activity",
        lazy: async () => ({ Component: (await import("@/pages/account_activity/account_activity_page")).AccountActivityPage }),
      },
      {
        path: "/enroll-passkey",
        lazy: async () => ({ Component: (await import("@/pages/passkey_enrollment_page")).PasskeyEnrollmentPage }),
      },
      { path: "/settings", lazy: async () => ({ Component: (await import("@/pages/account_settings_page")).AccountSettingsPage }) },
      {
        path: "/i/:invoiceId",
        lazy: async () => {
          const [invoicePaymentPageModule] = await Promise.all([import("@/pages/invoice_payment/invoice_payment_page"), loadPayerLanguageForPayerRoute()])
          return { Component: invoicePaymentPageModule.InvoicePaymentPage }
        },
      },
      {
        path: "/l/:paymentLinkId",
        lazy: async () => {
          const [paymentLinkPageModule] = await Promise.all([import("@/pages/payment_link/payment_link_page"), loadPayerLanguageForPayerRoute()])
          return { Component: paymentLinkPageModule.PaymentLinkPage }
        },
      },
      { path: "*", lazy: async () => ({ Component: (await import("@/pages/page_not_found_page")).PageNotFoundPage }) },
    ],
  },
])

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {/* Light and dark need nothing here: public/theme_before_first_paint.js keeps the class on <html> in step with
        prefers-color-scheme. Motion follows the device's reduced-motion setting: transforms off, fades kept. */}
    <MotionConfig reducedMotion="user">
      <AccountSessionProvider>
        <RouterProvider router={applicationRouter} />
        <AppToaster />
      </AccountSessionProvider>
    </MotionConfig>
  </StrictMode>,
)
