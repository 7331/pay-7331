// Paging back through `/activity`'s history. Events (`GET /api/v1/events`, PLAN.md section 8.1): the first page loads
// with the page, so "Load older" shows only when there is more, and each tap reads the next page by its
// `next_cursor`; the rows stay apart from the live stream's list and the page merges
// the two by id. Invoices (`GET /api/v1/invoices`) page the same way in the page itself.
import { useCallback, useEffect, useRef, useState } from "react"

import { describeError, fetchApiJson } from "@/lib/api_client"
import type { AccountEvent, AccountEventPage, AccountInvoice } from "@/lib/api_types"

// Every list once, newest first: the stream and a history page can both hold an event, the first page and an older
// one an invoice.
export function mergeAccountEventsNewestFirst(...accountEventLists: AccountEvent[][]): AccountEvent[] {
  const accountEventById = new Map<string, AccountEvent>()
  for (const accountEvents of accountEventLists) {
    for (const accountEvent of accountEvents) accountEventById.set(accountEvent.id, accountEvent)
  }
  // Ids are random (`evt_...`), so the order is the time each event was recorded, ties broken by id.
  return [...accountEventById.values()].sort((first, second) => {
    const createdAtOrder = new Date(second.created_at).getTime() - new Date(first.created_at).getTime()
    if (createdAtOrder !== 0) return createdAtOrder
    return first.id < second.id ? 1 : first.id > second.id ? -1 : 0
  })
}

// The server's order (`created_at`, then `id`, both descending); `fresherInvoices` win over a stale copy of the same
// invoice.
export function mergeAccountInvoicesNewestFirst(currentInvoices: AccountInvoice[], fresherInvoices: AccountInvoice[]): AccountInvoice[] {
  const invoiceById = new Map<string, AccountInvoice>()
  for (const invoice of [...currentInvoices, ...fresherInvoices]) invoiceById.set(invoice.id, invoice)
  return [...invoiceById.values()].sort((first, second) => {
    const createdAtOrder = new Date(second.created_at).getTime() - new Date(first.created_at).getTime()
    if (createdAtOrder !== 0) return createdAtOrder
    return first.id < second.id ? 1 : first.id > second.id ? -1 : 0
  })
}

export function useOlderAccountEvents() {
  const [olderAccountEvents, setOlderAccountEvents] = useState<AccountEvent[]>([])
  // undefined until the first page answers; null once there is no older page.
  const [olderAccountEventsCursor, setOlderAccountEventsCursor] = useState<string | null | undefined>(undefined)
  const [olderAccountEventsAreLoading, setOlderAccountEventsAreLoading] = useState(false)
  const [olderAccountEventsLoadError, setOlderAccountEventsLoadError] = useState("")
  const isLoadingRef = useRef(false)

  // The state changes in the promise callbacks, once the API answers, never synchronously in the effect below.
  const loadAccountEventPage = useCallback(
    (cursor: string | null) =>
      fetchApiJson<AccountEventPage>(cursor === null ? "/api/v1/events" : `/api/v1/events?cursor=${encodeURIComponent(cursor)}`).then(
        (accountEventPage) => {
          setOlderAccountEvents((currentEvents) => mergeAccountEventsNewestFirst(currentEvents, accountEventPage.events))
          setOlderAccountEventsCursor(accountEventPage.next_cursor)
          setOlderAccountEventsLoadError("")
        },
        (loadError: unknown) => setOlderAccountEventsLoadError(describeError(loadError)),
      ),
    [],
  )

  useEffect(() => {
    void loadAccountEventPage(null)
  }, [loadAccountEventPage])

  // A failed first page is retried by the same tap.
  const loadOlderAccountEvents = useCallback(() => {
    if (olderAccountEventsCursor === null || isLoadingRef.current) return
    isLoadingRef.current = true
    setOlderAccountEventsAreLoading(true)
    void loadAccountEventPage(olderAccountEventsCursor ?? null).finally(() => {
      isLoadingRef.current = false
      setOlderAccountEventsAreLoading(false)
    })
  }, [loadAccountEventPage, olderAccountEventsCursor])

  return {
    olderAccountEvents,
    hasOlderAccountEvents: typeof olderAccountEventsCursor === "string" || (olderAccountEventsCursor === undefined && olderAccountEventsLoadError !== ""),
    olderAccountEventsAreLoading,
    olderAccountEventsLoadError,
    loadOlderAccountEvents,
  }
}
