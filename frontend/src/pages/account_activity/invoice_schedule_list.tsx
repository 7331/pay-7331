// "Repeating invoices" under `/activity`'s "Links" tab (2026-10-21, PLAN.md section 18): the account's invoice
// schedules (loaded by the page, use_account_invoice_schedules.ts), each row "Monthly · next Nov 20 · 3 made", and a drawer per schedule with its
// invoices, pause or resume, and cancel.
import { ChevronRight } from "lucide-react"
import { useState } from "react"

import { Item, ItemActions, ItemContent, ItemDescription, ItemGroup, ItemTitle } from "@/components/ui/item"
import { Skeleton } from "@/components/ui/skeleton"
import type { AccountInvoiceSchedule } from "@/lib/api_types"
import { describeInvoiceScheduleAmount, describeInvoiceScheduleTiming } from "@/pages/account_activity/invoice_schedule_descriptions"
import { InvoiceScheduleDrawer } from "@/pages/account_activity/invoice_schedule_drawer"
import { InvoiceScheduleStatusBadge } from "@/pages/account_activity/invoice_schedule_status_badge"

export function AccountInvoiceScheduleList({
  invoiceSchedules,
  scheduleLoadError,
  liveReloadCount,
  onInvoiceScheduleChange,
}: {
  invoiceSchedules: AccountInvoiceSchedule[] | null
  scheduleLoadError: string
  liveReloadCount: number
  onInvoiceScheduleChange(nextInvoiceSchedule: AccountInvoiceSchedule): void
}) {
  const [openedInvoiceScheduleId, setOpenedInvoiceScheduleId] = useState<string | null>(null)
  const openedInvoiceSchedule = invoiceSchedules?.find((invoiceSchedule) => invoiceSchedule.id === openedInvoiceScheduleId) ?? null

  return (
    <>
      {invoiceSchedules === null && !scheduleLoadError ? (
        <div className="flex flex-col gap-2">
          {[0, 1].map((placeholderIndex) => (
            <Skeleton key={placeholderIndex} className="h-16 w-full rounded-2xl" />
          ))}
        </div>
      ) : scheduleLoadError ? (
        <p className="py-3 text-sm text-destructive">Can't load schedules: {scheduleLoadError}</p>
      ) : invoiceSchedules && invoiceSchedules.length === 0 ? (
        <p className="px-1 py-2 text-sm text-muted-foreground">None yet. In New, Options, set "Repeat" to daily, weekly or monthly.</p>
      ) : (
        <ItemGroup id="invoice_schedule_list" className="gap-2">
          {invoiceSchedules?.map((invoiceSchedule) => (
            <Item key={invoiceSchedule.id} asChild variant="outline" className="min-h-16 rounded-2xl">
              <button type="button" data-testid="invoice_schedule_row" onClick={() => setOpenedInvoiceScheduleId(invoiceSchedule.id)} className="w-full text-left">
                <ItemContent className="min-w-0">
                  <ItemTitle className="tabular-nums">{describeInvoiceScheduleAmount(invoiceSchedule)}</ItemTitle>
                  <ItemDescription className="truncate">
                    {[describeInvoiceScheduleTiming(invoiceSchedule), invoiceSchedule.note].filter(Boolean).join(" · ")}
                  </ItemDescription>
                </ItemContent>
                <ItemActions>
                  <InvoiceScheduleStatusBadge invoiceScheduleStatus={invoiceSchedule.status} />
                  <ChevronRight className="size-4 text-muted-foreground" />
                </ItemActions>
              </button>
            </Item>
          ))}
        </ItemGroup>
      )}
      <InvoiceScheduleDrawer
        invoiceSchedule={openedInvoiceSchedule}
        reloadKey={liveReloadCount}
        onClose={() => setOpenedInvoiceScheduleId(null)}
        onInvoiceScheduleChange={onInvoiceScheduleChange}
      />
    </>
  )
}
