// One of the account's invoice schedules in a drawer (2026-10-21, PLAN.md section 18): how often and what's next,
// the invoices it made (`GET /api/v1/invoice-schedules/{id}/invoices`), pause or resume (`POST .../pause`,
// `.../resume`: resuming never bills a period that started while paused) and "Cancel schedule" after a confirm
// (`POST .../cancel`): no new invoice after that, those made run their course.
import { Ban, ChevronRight, Pause, Play } from "lucide-react"
import { useEffect, useState } from "react"
import { toast } from "sonner"

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { Drawer, DrawerContent, DrawerDescription, DrawerHeader, DrawerTitle } from "@/components/ui/drawer"
import { Item, ItemActions, ItemContent, ItemDescription, ItemGroup, ItemTitle } from "@/components/ui/item"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { describeError, fetchApiJson } from "@/lib/api_client"
import type { AccountInvoice, AccountInvoiceSchedule, AccountInvoiceScheduleInvoiceList } from "@/lib/api_types"
import { touchTargetButtonClassName } from "@/lib/button_size_class_names"
import { formatShortTime } from "@/lib/time_formatting"
import { cn } from "@/lib/utils"
import { describeInvoiceLine } from "@/pages/account_activity/invoice_descriptions"
import {
  describeInvoiceScheduleAmount,
  describeInvoiceScheduleFailure,
  describeInvoiceScheduleTiming,
} from "@/pages/account_activity/invoice_schedule_descriptions"
import { InvoiceScheduleStatusBadge } from "@/pages/account_activity/invoice_schedule_status_badge"
import { InvoiceStatusBadge } from "@/pages/account_activity/invoice_status_badge"

type InvoiceScheduleAction = "pause" | "resume" | "cancel"

const actionDoneTexts: Record<InvoiceScheduleAction, string> = {
  pause: "Schedule paused.",
  resume: "Schedule back on.",
  cancel: "Schedule cancelled.",
}

export function InvoiceScheduleDrawer({
  invoiceSchedule,
  reloadKey,
  onClose,
  onInvoiceScheduleChange,
}: {
  invoiceSchedule: AccountInvoiceSchedule | null
  // Changes when an account event arrives, so the open drawer's invoices follow.
  reloadKey: number
  onClose(): void
  onInvoiceScheduleChange(nextInvoiceSchedule: AccountInvoiceSchedule): void
}) {
  const [scheduleInvoices, setScheduleInvoices] = useState<{ invoiceScheduleId: string; invoices: AccountInvoice[] } | null>(null)
  const [invoiceLoadError, setInvoiceLoadError] = useState("")
  const [runningAction, setRunningAction] = useState<InvoiceScheduleAction | null>(null)
  const invoiceScheduleId = invoiceSchedule?.id ?? null

  // The state changes in the promise callbacks, never synchronously in the effect.
  useEffect(() => {
    if (!invoiceScheduleId) return
    let isCurrent = true
    fetchApiJson<AccountInvoiceScheduleInvoiceList>(`/api/v1/invoice-schedules/${encodeURIComponent(invoiceScheduleId)}/invoices`).then(
      (invoiceList) => {
        if (!isCurrent) return
        setScheduleInvoices({ invoiceScheduleId, invoices: invoiceList.invoices ?? [] })
        setInvoiceLoadError("")
      },
      (loadError: unknown) => isCurrent && setInvoiceLoadError(describeError(loadError)),
    )
    return () => {
      isCurrent = false
    }
  }, [invoiceScheduleId, reloadKey])

  async function runInvoiceScheduleAction(scheduleToChange: AccountInvoiceSchedule, action: InvoiceScheduleAction) {
    setRunningAction(action)
    try {
      const changedInvoiceSchedule = await fetchApiJson<AccountInvoiceSchedule>(
        `/api/v1/invoice-schedules/${encodeURIComponent(scheduleToChange.id)}/${action}`,
        { method: "POST" },
      )
      onInvoiceScheduleChange(changedInvoiceSchedule)
      toast.success(actionDoneTexts[action])
    } catch (actionError) {
      toast.error(describeError(actionError))
    } finally {
      setRunningAction(null)
    }
  }

  const shownInvoices = scheduleInvoices?.invoiceScheduleId === invoiceScheduleId ? scheduleInvoices.invoices : null
  const scheduleIsLive = invoiceSchedule?.status === "active" || invoiceSchedule?.status === "paused"

  return (
    <Drawer open={invoiceSchedule !== null} onOpenChange={(isOpen) => !isOpen && onClose()}>
      <DrawerContent className="sm:mx-auto sm:max-w-[440px]">
        {invoiceSchedule ? (
          <div className="mx-auto w-full max-w-[440px] overflow-y-auto px-5 pb-6">
            <DrawerHeader className="items-start px-0 pt-5 text-left md:text-left">
              <div className="flex w-full items-center justify-between gap-3">
                <DrawerTitle className="text-3xl font-semibold tracking-tight tabular-nums">{describeInvoiceScheduleAmount(invoiceSchedule)}</DrawerTitle>
                <InvoiceScheduleStatusBadge invoiceScheduleStatus={invoiceSchedule.status} />
              </div>
              <DrawerDescription className="text-[15px]">{invoiceSchedule.note || "No note"}</DrawerDescription>
            </DrawerHeader>
            <p id="invoice_schedule_drawer_timing" className="text-[15px] font-medium tabular-nums">
              {describeInvoiceScheduleTiming(invoiceSchedule)}
            </p>
            {invoiceSchedule.status === "active" && invoiceSchedule.last_failure_code ? (
              <p className="mt-2 text-sm text-warning">{describeInvoiceScheduleFailure(invoiceSchedule)}</p>
            ) : null}
            <p className="mt-2 text-sm text-muted-foreground">
              Each invoice goes to your webhook and API as <span className="font-mono">schedule.invoice_created</span>; send its link to your payer.
            </p>
            <Separator className="my-4" />
            <h2 className="mb-2 px-1 text-sm font-medium text-muted-foreground">Invoices</h2>
            {shownInvoices ? (
              shownInvoices.length === 0 ? (
                <p className="px-1 py-3 text-sm text-muted-foreground">None yet.</p>
              ) : (
                <ItemGroup id="invoice_schedule_invoice_list" className="gap-0 rounded-2xl border">
                  {shownInvoices.map((scheduledInvoice) => (
                    <Item key={scheduledInvoice.id} asChild size="sm" className="min-h-14 rounded-none border-b-border last:border-b-transparent">
                      <a href={`/i/${encodeURIComponent(scheduledInvoice.id)}`} target="_blank" rel="noopener noreferrer">
                        <ItemContent className="min-w-0">
                          <ItemTitle className="tabular-nums">{formatShortTime(scheduledInvoice.invoice_schedule_period_start ?? scheduledInvoice.created_at)}</ItemTitle>
                          <ItemDescription className="truncate">{describeInvoiceLine({ ...scheduledInvoice, note: "" }) || scheduledInvoice.order_id}</ItemDescription>
                        </ItemContent>
                        <ItemActions>
                          <InvoiceStatusBadge invoiceStatus={scheduledInvoice.status} paymentIsSeen={scheduledInvoice.seen} />
                          <ChevronRight className="size-4 text-muted-foreground" />
                        </ItemActions>
                      </a>
                    </Item>
                  ))}
                </ItemGroup>
              )
            ) : invoiceLoadError ? (
              <p className="py-3 text-sm text-destructive">Can't load its invoices: {invoiceLoadError}</p>
            ) : (
              <Skeleton className="h-14 w-full rounded-2xl" />
            )}
            {scheduleIsLive ? (
              <div className="mt-4 flex flex-col gap-2">
                {invoiceSchedule.status === "active" ? (
                  <Button
                    id="pause_invoice_schedule_button"
                    variant="secondary"
                    className={cn(touchTargetButtonClassName, "w-full justify-start")}
                    disabled={runningAction !== null}
                    onClick={() => void runInvoiceScheduleAction(invoiceSchedule, "pause")}
                  >
                    {runningAction === "pause" ? <Spinner /> : <Pause />} Pause
                  </Button>
                ) : (
                  <Button
                    id="resume_invoice_schedule_button"
                    variant="secondary"
                    className={cn(touchTargetButtonClassName, "w-full justify-start")}
                    disabled={runningAction !== null}
                    onClick={() => void runInvoiceScheduleAction(invoiceSchedule, "resume")}
                  >
                    {runningAction === "resume" ? <Spinner /> : <Play />} Resume
                  </Button>
                )}
                <AlertDialog>
                  <AlertDialogTrigger asChild>
                    <Button
                      id="cancel_invoice_schedule_button"
                      variant="destructive"
                      className={cn(touchTargetButtonClassName, "w-full justify-start")}
                      disabled={runningAction !== null}
                    >
                      {runningAction === "cancel" ? <Spinner /> : <Ban />} Cancel schedule
                    </Button>
                  </AlertDialogTrigger>
                  <AlertDialogContent>
                    <AlertDialogHeader>
                      <AlertDialogTitle>Cancel this {describeInvoiceScheduleAmount(invoiceSchedule)} schedule?</AlertDialogTitle>
                      <AlertDialogDescription>
                        No new invoice after this. Invoices already made can still be paid. This can't be undone.
                      </AlertDialogDescription>
                    </AlertDialogHeader>
                    <AlertDialogFooter>
                      <AlertDialogCancel className={touchTargetButtonClassName}>Keep it</AlertDialogCancel>
                      <AlertDialogAction
                        id="confirm_cancel_invoice_schedule_button"
                        variant="destructive"
                        className={touchTargetButtonClassName}
                        onClick={() => void runInvoiceScheduleAction(invoiceSchedule, "cancel")}
                      >
                        Cancel schedule
                      </AlertDialogAction>
                    </AlertDialogFooter>
                  </AlertDialogContent>
                </AlertDialog>
              </div>
            ) : null}
          </div>
        ) : null}
      </DrawerContent>
    </Drawer>
  )
}
