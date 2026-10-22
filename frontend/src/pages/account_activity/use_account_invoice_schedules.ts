// The account's invoice schedules for `/activity`'s "Links" tab (`GET /api/v1/invoice-schedules`, loaded once and
// again with each debounced reload after live events, as use_account_payment_links.ts does for the links).
import { useCallback, useEffect, useState } from "react"

import { describeError, fetchApiJson } from "@/lib/api_client"
import type { AccountInvoiceSchedule, AccountInvoiceScheduleList } from "@/lib/api_types"

export function useAccountInvoiceSchedules(liveReloadCount: number) {
  const [invoiceSchedules, setInvoiceSchedules] = useState<AccountInvoiceSchedule[] | null>(null)
  const [scheduleLoadError, setScheduleLoadError] = useState("")

  // The state changes in the promise callbacks, once the API answers, never synchronously in the effect below.
  const loadInvoiceSchedules = useCallback(
    () =>
      fetchApiJson<AccountInvoiceScheduleList>("/api/v1/invoice-schedules").then(
        (invoiceScheduleList) => {
          setInvoiceSchedules(invoiceScheduleList.invoice_schedules ?? [])
          setScheduleLoadError("")
        },
        (loadError: unknown) => setScheduleLoadError(describeError(loadError)),
      ),
    [],
  )
  useEffect(() => {
    void loadInvoiceSchedules()
  }, [loadInvoiceSchedules, liveReloadCount])

  const replaceInvoiceSchedule = useCallback((nextInvoiceSchedule: AccountInvoiceSchedule) => {
    setInvoiceSchedules(
      (currentSchedules) =>
        currentSchedules?.map((invoiceSchedule) => (invoiceSchedule.id === nextInvoiceSchedule.id ? nextInvoiceSchedule : invoiceSchedule)) ?? null,
    )
  }, [])

  return { invoiceSchedules, scheduleLoadError, replaceInvoiceSchedule }
}
