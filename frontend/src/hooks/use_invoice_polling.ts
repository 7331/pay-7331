// The payer's invoice, polled every 3 s only while it can still change and only while the tab is visible.
// Actions on the page hand their answer to `applyInvoiceAndResumePolling`, so the screen updates
// at once and polling picks up again if the invoice can still change.
import { useCallback, useEffect, useRef, useState } from "react"

import { ApiError, fetchApiJson } from "@/lib/api_client"
import type { PublicInvoice } from "@/lib/api_types"
import { describePayerError } from "@/lib/payer_errors"
import { readPayerStrings } from "@/lib/payer_language"

const invoicePollIntervalMilliseconds = 3000
const statusesThatCanChange = new Set(["awaiting_asset", "awaiting_payment", "confirming", "underpaid"])
const refundStatusesStillMoving = new Set(["pending", "broadcast", "mined", "waiting_for_float"])

function invoiceNeedsPolling(invoice: PublicInvoice): boolean {
  if (statusesThatCanChange.has(invoice.status)) return true
  if (invoice.stray_payments.some((strayPayment) => !strayPayment.final)) return true
  return invoice.refunds.some((refund) => refundStatusesStillMoving.has(refund.status))
}

export function publicInvoiceApiPath(invoiceId: string, suffix = ""): string {
  return `/api/v1/public/invoices/${encodeURIComponent(invoiceId)}${suffix}`
}

export function useInvoicePolling(invoiceId: string) {
  const [invoice, setInvoice] = useState<PublicInvoice | null>(null)
  const [loadErrorMessage, setLoadErrorMessage] = useState("")
  const [connectionProblemMessage, setConnectionProblemMessage] = useState("")
  const latestInvoice = useRef<PublicInvoice | null>(null)
  const latestInvoiceJsonText = useRef("")
  const scheduleNextPoll = useRef<() => void>(() => undefined)

  // A poll that changes nothing keeps the same object, so nothing re-renders.
  const applyInvoice = useCallback((nextInvoice: PublicInvoice) => {
    setConnectionProblemMessage("")
    const nextInvoiceJsonText = JSON.stringify(nextInvoice)
    if (nextInvoiceJsonText === latestInvoiceJsonText.current) return
    latestInvoiceJsonText.current = nextInvoiceJsonText
    latestInvoice.current = nextInvoice
    setInvoice(nextInvoice)
  }, [])

  useEffect(() => {
    let pollTimer: number | undefined
    let isCurrent = true

    async function loadInvoiceAndScheduleNextPoll() {
      window.clearTimeout(pollTimer)
      try {
        const loadedInvoice = await fetchApiJson<PublicInvoice>(publicInvoiceApiPath(invoiceId))
        if (!isCurrent) return
        applyInvoice(loadedInvoice)
      } catch (loadError) {
        if (!isCurrent) return
        if (!latestInvoice.current) {
          // A malformed id (400) or a missing invoice (404) won't fix itself: say so in plain words and stop; a
          // network error is worth retrying (2026-10-11).
          if (loadError instanceof ApiError && (loadError.httpStatus === 400 || loadError.httpStatus === 404)) {
            setLoadErrorMessage(readPayerStrings().checkYouCopiedTheWholeLink)
            return
          }
          setLoadErrorMessage(describePayerError(loadError))
        } else {
          setConnectionProblemMessage(describePayerError(loadError))
        }
      }
      schedulePoll()
    }

    function schedulePoll() {
      window.clearTimeout(pollTimer)
      if (!isCurrent || document.hidden) return
      if (latestInvoice.current && !invoiceNeedsPolling(latestInvoice.current)) return
      pollTimer = window.setTimeout(loadInvoiceAndScheduleNextPoll, invoicePollIntervalMilliseconds)
    }

    function handleVisibilityChange() {
      if (document.hidden) window.clearTimeout(pollTimer)
      else if (!latestInvoice.current || invoiceNeedsPolling(latestInvoice.current)) void loadInvoiceAndScheduleNextPoll()
    }

    scheduleNextPoll.current = schedulePoll
    void loadInvoiceAndScheduleNextPoll()
    document.addEventListener("visibilitychange", handleVisibilityChange)
    return () => {
      isCurrent = false
      window.clearTimeout(pollTimer)
      document.removeEventListener("visibilitychange", handleVisibilityChange)
    }
  }, [invoiceId, applyInvoice])

  const applyInvoiceAndResumePolling = useCallback(
    (nextInvoice: PublicInvoice) => {
      applyInvoice(nextInvoice)
      scheduleNextPoll.current()
    },
    [applyInvoice],
  )

  return { invoice, loadErrorMessage, connectionProblemMessage, applyInvoiceAndResumePolling }
}
