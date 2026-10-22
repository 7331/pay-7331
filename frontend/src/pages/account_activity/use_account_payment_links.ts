// The account's quantity links for `/activity` (`GET /api/v1/payment-links`, loaded once and again with each debounced
// reload after live events, never per event of the catch-up):
// the Links tab lists them, and an invoice row a payer made from one names its sales.
import { useCallback, useEffect, useState } from "react"

import { describeError, fetchApiJson } from "@/lib/api_client"
import type { AccountPaymentLink, AccountPaymentLinkList } from "@/lib/api_types"

export function useAccountPaymentLinks(liveReloadCount: number) {
  const [paymentLinks, setPaymentLinks] = useState<AccountPaymentLink[] | null>(null)
  const [linkLoadError, setLinkLoadError] = useState("")

  // The state changes in the promise callbacks, once the API answers, never synchronously in the effect below.
  const loadPaymentLinks = useCallback(
    () =>
      fetchApiJson<AccountPaymentLinkList>("/api/v1/payment-links").then(
        (paymentLinkList) => {
          setPaymentLinks(paymentLinkList.payment_links ?? [])
          setLinkLoadError("")
        },
        (loadError: unknown) => setLinkLoadError(describeError(loadError)),
      ),
    [],
  )
  useEffect(() => {
    void loadPaymentLinks()
  }, [loadPaymentLinks, liveReloadCount])

  const replacePaymentLink = useCallback((nextPaymentLink: AccountPaymentLink) => {
    setPaymentLinks((currentLinks) => currentLinks?.map((paymentLink) => (paymentLink.id === nextPaymentLink.id ? nextPaymentLink : paymentLink)) ?? null)
  }, [])

  return { paymentLinks, linkLoadError, replacePaymentLink }
}
