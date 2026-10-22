// Refusals of "Create link" on `/` that need more than one sentence per code. The open-link cap
// (429 `too_many_open_invoices`, PLAN.md section 9) counts a network's unclosed links; a signed-out seller can't cancel
// one, so the copy says they close on their own and when, from the answer's Retry-After (2026-10-11).
// Every other refusal reads through `describeError`.
import { ApiError, describeError } from "@/lib/api_client"

function formatWaitInMinutes(waitSeconds: number): string {
  const waitMinutes = Math.max(1, Math.ceil(waitSeconds / 60))
  if (waitMinutes < 60) return waitMinutes === 1 ? "about a minute" : `about ${waitMinutes} minutes`
  const waitHours = Math.round(waitMinutes / 60)
  return waitHours === 1 ? "about an hour" : `about ${waitHours} hours`
}

function formatClockTimeAfter(waitSeconds: number): string {
  return new Date(Date.now() + waitSeconds * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
}

export function describeInvoiceCreationError(createError: unknown): string {
  if (createError instanceof ApiError && createError.code === "too_many_open_invoices") {
    const firstSentence = "Too many open links from your network right now."
    if (createError.retryAfterSeconds === null) {
      return `${firstSentence} A link closes once it's paid or its time to pay runs out; try again after that.`
    }
    const waitSeconds = createError.retryAfterSeconds
    return `${firstSentence} Unpaid links close on their own when their time to pay runs out: the next one in ${formatWaitInMinutes(waitSeconds)}, at ${formatClockTimeAfter(waitSeconds)}.`
  }
  return describeError(createError)
}
