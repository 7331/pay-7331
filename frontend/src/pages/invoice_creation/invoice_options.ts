// The signed-in account's invoice options on `/` (PLAN.md sections 7 and 10), the fields of `POST /api/v1/invoices`
// beyond the amount, receiver, note, networks and coins: currency, how many payers, repeat, time to pay, return link,
// top-ups deadline, accepted shortfall, order id, metadata and the contract tick. Their defaults, their checks (each wrong one
// gets its own sentence, shown under its field in the options sheet) and the short summary `/` shows of the changed ones.
// Time to pay and the shortfall start empty, "Network default": nothing is sent, and each network takes the account's
// setting for it in `/settings`' "Coins and networks" sheet, else the server's (PLAN.md section 4); a typed value is for
// this invoice. Only an empty field means the default: minutes the browser could not read and a shortfall that is not a
// number are field errors, and a decimal comma ("0,5") is read as a point.

import { normalizeAccuracyPercentText } from "@/lib/accuracy_percent_text"
import type { InvoiceScheduleInterval } from "@/lib/api_types"

export const fiatCurrencyCodes = ["USD", "EUR"]

// "Never" makes one invoice; the others make an invoice schedule (`POST /api/v1/invoice-schedules`, PLAN.md section
// 18) that makes a new one each day, week or month from now, for the account to send its payer (2026-10-21).
export const repeatIntervalChoices: { value: "" | InvoiceScheduleInterval; label: string }[] = [
  { value: "", label: "Never" },
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" },
  { value: "monthly", label: "Monthly" },
]
export const maximumPayerQuantity = 1000

// Time-to-pay presets read faster than minutes; the minutes field under them takes any whole number from 5 to 720.
export const lifetimePresets = [
  { minutes: "15", label: "15 min" },
  { minutes: "30", label: "30 min" },
  { minutes: "60", label: "1 h" },
  { minutes: "180", label: "3 h" },
  { minutes: "720", label: "12 h" },
]

export interface InvoiceOptions {
  currencyCode: string
  lifetimeMinutesText: string
  // The minutes field holds text its browser could not read as a number (`validity.badInput`), so its value reads "".
  lifetimeMinutesTextIsUnreadable: boolean
  // 1 makes a one-off invoice; more makes a quantity link that many payers pay, each once (2026-10-11).
  payerQuantityText: string
  // "" for a one-off invoice.
  repeatInterval: "" | InvoiceScheduleInterval
  returnUrl: string
  // A `datetime-local` value, "" for no deadline.
  underpaidOpenUntilLocal: string
  accuracyPercentText: string
  orderId: string
  metadataText: string
  allowContractDestination: boolean
}

export const defaultInvoiceOptions: InvoiceOptions = {
  currencyCode: "USD",
  lifetimeMinutesText: "",
  lifetimeMinutesTextIsUnreadable: false,
  payerQuantityText: "1",
  repeatInterval: "",
  returnUrl: "",
  underpaidOpenUntilLocal: "",
  accuracyPercentText: "",
  orderId: "",
  metadataText: "",
  allowContractDestination: false,
}

// Each checked option and its input's id, in the sheet's order, so the first wrong one is the first in the sheet.
export const invoiceOptionInputIds = {
  payerQuantity: "quantity_input",
  repeatInterval: "repeat_interval_never",
  lifetimeMinutes: "lifetime_minutes_input",
  returnUrl: "return_url_input",
  accuracyPercent: "accuracy_percent_input",
  metadata: "metadata_input",
} as const
export type InvoiceOptionFieldName = keyof typeof invoiceOptionInputIds
export type InvoiceOptionFieldErrors = Partial<Record<InvoiceOptionFieldName, string>>

export interface CheckedInvoiceOptions {
  payerQuantity: number
  repeatInterval: InvoiceScheduleInterval | undefined
  // undefined for "Network default".
  lifetimeSeconds: number | undefined
  accuracyPercent: number | undefined
  // undefined for an empty field: JSON.stringify drops it, so it isn't sent.
  underpaidOpenUntil: string | undefined
  returnUrl: string | undefined
  orderId: string | undefined
  metadata: unknown
  allowContractDestination: true | undefined
}

export function currencyCodeIsCoin(currencyCode: string): boolean {
  return !fiatCurrencyCodes.includes(currencyCode)
}

// A whole number from 1 to 1000, else null.
export function readPayerQuantity(payerQuantityText: string): number | null {
  const trimmedQuantity = payerQuantityText.trim()
  if (!/^\d+$/.test(trimmedQuantity)) return null
  const payerQuantity = Number(trimmedQuantity)
  return payerQuantity >= 1 && payerQuantity <= maximumPayerQuantity ? payerQuantity : null
}

// What a quantity link can't carry; the quantity field says so.
function describePayerQuantityProblem(options: InvoiceOptions, payerQuantity: number | null): string {
  if (payerQuantity === null) return `A whole number from 1 to ${maximumPayerQuantity}.`
  if (payerQuantity === 1) return ""
  if (currencyCodeIsCoin(options.currencyCode)) return "A link for several payers is priced in USD or EUR."
  if (options.orderId.trim() || options.metadataText.trim() || options.underpaidOpenUntilLocal) {
    return "Top-ups open until, order id and metadata are for one-off invoices only."
  }
  return ""
}

// What a repeating invoice can't carry; the repeat field says so. Its order id is a prefix of each invoice's.
function describeRepeatProblem(options: InvoiceOptions, payerQuantity: number | null): string {
  if (!options.repeatInterval) return ""
  if (payerQuantity !== null && payerQuantity > 1) return "A repeating invoice has one payer. Set How many payers to 1."
  if (options.underpaidOpenUntilLocal) return "Top-ups open until is for one-off invoices. Clear it to repeat."
  if (options.orderId.trim().length > 100) return "With Repeat, the order id is at most 100 characters."
  return ""
}

// Checks every option at once, so each wrong one gets its own message.
export function checkInvoiceOptions(options: InvoiceOptions): {
  fieldErrors: InvoiceOptionFieldErrors
  checkedOptions: CheckedInvoiceOptions | null
} {
  const fieldErrors: InvoiceOptionFieldErrors = {}
  const payerQuantity = readPayerQuantity(options.payerQuantityText)
  const payerQuantityProblem = describePayerQuantityProblem(options, payerQuantity)
  if (payerQuantityProblem) fieldErrors.payerQuantity = payerQuantityProblem
  const repeatProblem = describeRepeatProblem(options, payerQuantity)
  if (repeatProblem) fieldErrors.repeatInterval = repeatProblem
  const lifetimeMinutesIsNetworkDefault = options.lifetimeMinutesText.trim() === "" && !options.lifetimeMinutesTextIsUnreadable
  const lifetimeMinutes = Number(options.lifetimeMinutesText)
  if (!lifetimeMinutesIsNetworkDefault && (!Number.isInteger(lifetimeMinutes) || lifetimeMinutes < 5 || lifetimeMinutes > 720)) {
    fieldErrors.lifetimeMinutes = "Pick a time, type whole minutes from 5 to 720 (12 hours), or leave it empty for the network default."
  }
  const trimmedReturnUrl = options.returnUrl.trim()
  if (trimmedReturnUrl && !URL.canParse(trimmedReturnUrl)) fieldErrors.returnUrl = "That isn't a full link: start it with https://"
  const accuracyPercentText = normalizeAccuracyPercentText(options.accuracyPercentText)
  const accuracyPercentIsNetworkDefault = accuracyPercentText === ""
  const accuracyPercent = Number(accuracyPercentText)
  if (!accuracyPercentIsNetworkDefault && !(/^\d+(\.\d+)?$/.test(accuracyPercentText) && accuracyPercent >= 0 && accuracyPercent <= 5)) {
    fieldErrors.accuracyPercent = "From 0 to 5 percent, or empty for the network default."
  }
  let metadata: unknown = undefined
  if (options.metadataText.trim()) {
    try {
      metadata = JSON.parse(options.metadataText)
      if (metadata === null || typeof metadata !== "object" || Array.isArray(metadata)) {
        fieldErrors.metadata = 'This must be a JSON object, like {"customer": "acme"}.'
      }
    } catch (parseError) {
      fieldErrors.metadata = `That isn't valid JSON: ${(parseError as Error).message}`
    }
  }
  if (Object.keys(fieldErrors).length > 0 || payerQuantity === null) return { fieldErrors, checkedOptions: null }
  return {
    fieldErrors,
    checkedOptions: {
      payerQuantity,
      repeatInterval: options.repeatInterval || undefined,
      lifetimeSeconds: lifetimeMinutesIsNetworkDefault ? undefined : lifetimeMinutes * 60,
      accuracyPercent: accuracyPercentIsNetworkDefault ? undefined : accuracyPercent,
      underpaidOpenUntil: options.underpaidOpenUntilLocal ? new Date(options.underpaidOpenUntilLocal).toISOString() : undefined,
      returnUrl: trimmedReturnUrl || undefined,
      orderId: options.orderId.trim() || undefined,
      metadata,
      allowContractDestination: options.allowContractDestination || undefined,
    },
  }
}

// The changed options in a few words ("EUR", "15 min", "2 payers"), for the Options button on `/`.
export function summarizeChangedInvoiceOptions(options: InvoiceOptions): string[] {
  const changedOptionWords: string[] = []
  if (options.currencyCode !== defaultInvoiceOptions.currencyCode) changedOptionWords.push(options.currencyCode)
  if (options.lifetimeMinutesText.trim() !== defaultInvoiceOptions.lifetimeMinutesText && !options.lifetimeMinutesTextIsUnreadable) {
    const lifetimePreset = lifetimePresets.find((preset) => preset.minutes === options.lifetimeMinutesText)
    changedOptionWords.push(lifetimePreset?.label ?? `${options.lifetimeMinutesText.trim()} min`)
  }
  const payerQuantity = readPayerQuantity(options.payerQuantityText)
  if (payerQuantity !== 1) changedOptionWords.push(`${options.payerQuantityText.trim()} payers`)
  const repeatChoice = repeatIntervalChoices.find((choice) => choice.value === options.repeatInterval)
  if (options.repeatInterval && repeatChoice) changedOptionWords.push(repeatChoice.label.toLowerCase())
  if (options.returnUrl.trim()) changedOptionWords.push("return link")
  if (options.underpaidOpenUntilLocal) changedOptionWords.push("top-ups deadline")
  if (options.accuracyPercentText.trim() !== defaultInvoiceOptions.accuracyPercentText) {
    changedOptionWords.push(`${options.accuracyPercentText.trim()}% shortfall`)
  }
  if (options.orderId.trim()) changedOptionWords.push("order id")
  if (options.metadataText.trim()) changedOptionWords.push("metadata")
  if (options.allowContractDestination) changedOptionWords.push("contract wallet")
  return changedOptionWords
}
