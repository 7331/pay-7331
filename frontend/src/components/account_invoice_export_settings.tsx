// `/settings`' "Invoice export" row and its sheet (2026-10-21): the account's invoices with their payments, sweeps and
// refunds as one CSV for accounting, from `GET /api/v1/invoices/export.csv`. The download is a plain link: a GET the
// session cookie rides along with, streamed by the API straight to a file, so nothing large passes through this page.
// The two dates are optional and read as whole UTC days, as the file's times are UTC.
import { Download } from "lucide-react"
import { useState } from "react"

import { AccountSettingsRow } from "@/components/account_settings_row"
import { ResponsiveSheet } from "@/components/responsive_sheet"
import { TemporalInput } from "@/components/temporal_input"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field"
import { primaryActionButtonClassName } from "@/lib/button_size_class_names"
import { cn } from "@/lib/utils"

const invoiceExportPath = "/api/v1/invoices/export.csv"
const millisecondsPerDay = 86_400_000

// `created_from` is the first day's 00:00 UTC; `created_before` the day after the last one, so "to" includes its day.
function buildInvoiceExportUrl(fromDateText: string, toDateText: string): string {
  const exportQuery = new URLSearchParams()
  if (fromDateText) exportQuery.set("created_from", `${fromDateText}T00:00:00Z`)
  if (toDateText) {
    const dayAfterToDate = new Date(Date.parse(`${toDateText}T00:00:00Z`) + millisecondsPerDay)
    exportQuery.set("created_before", dayAfterToDate.toISOString())
  }
  const queryText = exportQuery.toString()
  return queryText ? `${invoiceExportPath}?${queryText}` : invoiceExportPath
}

export function AccountInvoiceExportSettings() {
  const [sheetIsOpen, setSheetIsOpen] = useState(false)
  const [fromDateText, setFromDateText] = useState("")
  const [toDateText, setToDateText] = useState("")
  const datesAreReversed = Boolean(fromDateText && toDateText && toDateText < fromDateText)

  return (
    <>
      <AccountSettingsRow id="settings_invoice_export_row" title="Invoice export" valueText="CSV" onOpen={() => setSheetIsOpen(true)} />
      <ResponsiveSheet
        isOpen={sheetIsOpen}
        onOpenChange={setSheetIsOpen}
        title="Export invoices"
        description="Every invoice with its payments, payouts and refunds, one row each."
        testId="settings_invoice_export_sheet"
      >
        <div className="flex flex-col gap-4">
          {/* minmax(0, 1fr): neither column grows past half the sheet, whatever the date field asks for. */}
          <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-3">
            <Field className="min-w-0">
              <FieldLabel htmlFor="settings_invoice_export_from_input">From</FieldLabel>
              <TemporalInput
                id="settings_invoice_export_from_input"
                type="date"
                value={fromDateText}
                onChange={(changeEvent) => setFromDateText(changeEvent.target.value)}
              />
            </Field>
            <Field data-invalid={datesAreReversed || undefined} className="min-w-0">
              <FieldLabel htmlFor="settings_invoice_export_to_input">To</FieldLabel>
              <TemporalInput
                id="settings_invoice_export_to_input"
                type="date"
                value={toDateText}
                aria-invalid={datesAreReversed}
                onChange={(changeEvent) => setToDateText(changeEvent.target.value)}
              />
            </Field>
          </div>
          {datesAreReversed ? <FieldError id="settings_invoice_export_dates_error">Pick a "to" date on or after "from".</FieldError> : null}
          <FieldDescription>Leave both empty for all time. Days and times are UTC.</FieldDescription>
          {datesAreReversed ? (
            <Button type="button" className={cn(primaryActionButtonClassName, "w-full")} disabled>
              <Download /> Download CSV
            </Button>
          ) : (
            <Button asChild className={cn(primaryActionButtonClassName, "w-full")}>
              <a id="settings_invoice_export_download_link" href={buildInvoiceExportUrl(fromDateText, toDateText)} download>
                <Download /> Download CSV
              </a>
            </Button>
          )}
        </div>
      </ResponsiveSheet>
    </>
  )
}
