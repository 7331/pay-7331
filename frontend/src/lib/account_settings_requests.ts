// `PATCH /api/v1/accounts/current` for `/settings`' sheets: each sends only the fields it changed. A per-chain field of
// `null` clears that chain's saved value back to the default.
import { ApiError, fetchApiJson } from "@/lib/api_client"
import type { AccountSession } from "@/lib/api_types"

export type AccountSettingsPatchBody = Partial<{
  accepted_assets_by_chain: Record<string, string[]>
  confirmations_final_by_chain: Record<string, number | null>
  invoice_lifetime_seconds_by_chain: Record<string, number | null>
  // Decimal strings, never floats.
  accuracy_percent_by_chain: Record<string, string | null>
  support_contact: string | null
}>

// The new session on success; a 401 refreshes the session (the page then shows the sign-in gate) and still throws, so
// the sheet stays as it was.
export async function saveAccountSettings(
  patchBody: AccountSettingsPatchBody,
  refreshAccountSession: () => Promise<unknown>,
): Promise<AccountSession> {
  try {
    return await fetchApiJson<AccountSession>("/api/v1/accounts/current", { method: "PATCH", body: patchBody })
  } catch (saveFailure) {
    if (saveFailure instanceof ApiError && saveFailure.httpStatus === 401) await refreshAccountSession()
    throw saveFailure
  }
}
