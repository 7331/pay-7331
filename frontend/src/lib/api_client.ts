// Calls the JSON API on this origin. Account routes ride on the HttpOnly session cookie the passkey sign-in set; no
// Authorization header anywhere: API tokens are for an account's own server, never the pages (PLAN.md section 7).

export class ApiError extends Error {
  readonly code: string | null
  readonly httpStatus: number
  // The answer's Retry-After in seconds, when it sent one (a 429: the rate limit, or when the open-link cap frees up).
  readonly retryAfterSeconds: number | null

  constructor(message: string, code: string | null, httpStatus: number, retryAfterSeconds: number | null = null) {
    super(message)
    this.code = code
    this.httpStatus = httpStatus
    this.retryAfterSeconds = retryAfterSeconds
  }
}

export async function fetchApiJson<ResponseJson>(
  path: string,
  { method = "GET", body }: { method?: string; body?: unknown } = {},
): Promise<ResponseJson> {
  const headers: Record<string, string> = { Accept: "application/json" }
  if (body !== undefined) headers["Content-Type"] = "application/json"
  let response: Response
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
    })
  } catch {
    throw new ApiError("Can't reach the server. Check your connection.", "network_error", 0)
  }
  const responseText = await response.text()
  let responseJson: unknown = null
  try {
    responseJson = responseText ? JSON.parse(responseText) : null
  } catch {
    responseJson = null
  }
  if (!response.ok) {
    const errorBody = (responseJson ?? {}) as { message?: string; code?: string }
    const retryAfterHeader = response.headers.get("Retry-After")
    const retryAfterSeconds = retryAfterHeader && /^\d+$/.test(retryAfterHeader) ? Number(retryAfterHeader) : null
    throw new ApiError(
      errorBody.message ?? `The server answered ${response.status}.`,
      errorBody.code ?? null,
      response.status,
      retryAfterSeconds,
    )
  }
  return responseJson as ResponseJson
}

// Field names the API puts before its `validation_error` messages ("amount: ..."), in the pages' words.
const validationFieldLabels: Record<string, string> = {
  amount: "Amount",
  amount_usd: "Amount",
  currency: "Currency",
  chain: "Network",
  chains: "Networks",
  asset: "Coin",
  assets: "Coins",
  assets_by_chain: "Coins",
  destination_address: "Receiver address",
  payout_address: "Payout address",
  support_contact: "Support contact",
  username: "Username",
  label: "Passkey name",
  note: "Note",
  order_id: "Order id",
  lifetime_minutes: "Time to pay",
  lifetime_seconds: "Time to pay",
  quantity: "How many",
  accuracy_percent: "Accuracy",
  metadata: "Metadata",
  accepted_assets_by_chain: "Coins",
  sender: "Sender address",
  nonce: "Refund offer",
  signature: "Signature",
  name: "Token name",
  url: "Webhook URL",
}

function capitalizeSentence(text: string): string {
  const trimmedText = text.trim()
  if (!trimmedText) return trimmedText
  const capitalizedText = trimmedText[0].toUpperCase() + trimmedText.slice(1)
  return /[.!?]$/.test(capitalizedText) ? capitalizedText : `${capitalizedText}.`
}

// "amount: at most 2 decimals for a fiat currency" -> "Amount: at most 2 decimals for a fiat currency."
function describeValidationMessage(serverMessage: string): string {
  const separatorIndex = serverMessage.indexOf(": ")
  if (separatorIndex < 0) return serverMessage === "invalid request" ? "Something in the form isn't valid. Check it and try again." : capitalizeSentence(serverMessage)
  const fieldPath = serverMessage.slice(0, separatorIndex)
  const fieldName = fieldPath.split(".")[0]
  const fieldLabel = validationFieldLabels[fieldName] ?? fieldName.replaceAll("_", " ")
  const problemText = serverMessage.slice(separatorIndex + 2)
  if (problemText === "invalid value") return capitalizeSentence(`${fieldLabel} isn't valid`)
  return capitalizeSentence(`${fieldLabel}: ${problemText}`)
}

// A Retry-After as whole seconds under a minute, else whole minutes rounded up; none at all reads as a minute. The
// payer's pages say the same wait in the payer's language (lib/payer_errors.ts).
export function measureWaitTime(retryAfterSeconds: number | null): { unit: "seconds" | "minutes"; count: number } {
  if (!retryAfterSeconds) return { unit: "minutes", count: 1 }
  if (retryAfterSeconds < 60) return { unit: "seconds", count: retryAfterSeconds }
  return { unit: "minutes", count: Math.ceil(retryAfterSeconds / 60) }
}

function describeWaitTime(retryAfterSeconds: number | null): string {
  const waitTime = measureWaitTime(retryAfterSeconds)
  if (waitTime.count === 1) return waitTime.unit === "seconds" ? "a second" : "a minute"
  return `${waitTime.count} ${waitTime.unit}`
}

// Plain sentences for every `code` the API answers the pages with (backend/app/shared/errors.py and each domain's
// errors.py). A few codes cover several cases, told apart by the server's message; a code or case not listed shows
// the server's own message as a sentence, which the API writes to be safe to show. Operator-only codes (the
// settlement CLI's refund_payment_not_proven and the like) never reach a page and are left out. The payer's pages show
// these in the payer's language through lib/payer_errors.ts: a code a payer can meet gets its sentence there too.
function describeApiError(apiError: ApiError): string {
  const serverMessage = apiError.message
  const serverMessageIncludes = (fragment: string) => serverMessage.includes(fragment)
  switch (apiError.code) {
    case "network_error":
      return serverMessage
    case "rate_limited":
      if (serverMessageIncludes("Discord webhook changes"))
        return `Too many Discord webhook changes today. Wait ${describeWaitTime(apiError.retryAfterSeconds)} and try again.`
      return `Too many tries from your network. Wait ${describeWaitTime(apiError.retryAfterSeconds)} and try again.`
    case "too_many_open_invoices":
      // `/` says when the next one closes (lib/invoice_creation_errors.ts); this is the sentence elsewhere.
      return "Too many open links from your network right now. Unpaid links close on their own when their time to pay runs out; try again after that."
    case "public_invoice_capacity_reached":
      return `This site has too many open payment links right now. Wait ${describeWaitTime(apiError.retryAfterSeconds)} and try again.`
    case "too_many_event_streams":
      return "Activity is open in too many tabs. Close one and reload this page."
    case "validation_error":
      return describeValidationMessage(serverMessage)
    case "destination_required":
      return "Paste the receiver's 0x address."
    case "destination_refused":
      if (serverMessageIncludes("deposit address")) return "That's one of this gateway's own deposit addresses. Use the receiver's own wallet address."
      if (serverMessageIncludes("is required")) return "Paste the receiver's 0x address."
      return "Money can't be sent to that address. Use a regular wallet address."
    case "invoice_not_found":
      return "This invoice doesn't exist. Check you copied the whole link, or ask the seller to send it again."
    case "invoice_state_conflict":
      if (serverMessageIncludes("already picked")) return "A coin was already picked for this invoice. Reload the page."
      if (serverMessageIncludes("expired before a coin")) return "This invoice expired before a coin was picked. Ask the seller for a new link."
      if (serverMessageIncludes("was cancelled")) return "This invoice was cancelled. Ask the seller for a new link."
      if (serverMessageIncludes("refund for this invoice is under way")) return "A refund is on its way back to you, so this invoice takes no more payments."
      if (serverMessageIncludes("fresh quote")) return "This invoice can't get a new price right now. Reload the page."
      if (serverMessageIncludes("nothing received can be cancelled")) return "Money already came in on this invoice, so it can't be cancelled."
      if (serverMessageIncludes("refund was made")) return "A refund went out on this invoice, so it can't be accepted as partly paid."
      if (serverMessageIncludes("partially paid")) return "Only an underpaid invoice can be accepted as partly paid."
      if (serverMessageIncludes("no payment address")) return "Pick a coin first."
      return "This invoice changed in the meantime. Reload the page and try again."
    case "static_deposit_address_not_found":
      return "This customer address doesn't exist on your account. Reload the page."
    case "static_deposit_address_limit_reached":
      return "This account has as many customer addresses as it can have."
    case "payment_link_not_found":
      return "This link doesn't exist. Check you copied the whole link, or ask the seller to send it again."
    case "sold_out":
      return "Sold out."
    case "payment_link_closed":
      return "This link is closed."
    case "refund_not_allowed":
      if (serverMessageIncludes("pays its receiver directly")) {
        return "This invoice paid the receiver directly, so this site can't send a refund. Ask the receiver to send the money back."
      }
      if (serverMessageIncludes("payment window")) return "You can ask for an underpaid amount back once the time to pay is up."
      if (serverMessageIncludes("already under way")) return "A refund for this invoice is already on its way."
      if (serverMessageIncludes("at most")) return capitalizeSentence(serverMessage)
      return "Nothing on this invoice can be refunded from here."
    case "refund_offer_not_found":
      return "That refund offer is gone. Reload the page and try again."
    case "refund_offer_used":
      return "That refund offer was already used or has run out. Reload the page and try again."
    case "refund_signature_invalid":
      return "The signature isn't from the wallet that paid. Switch to the account that sent the money and try again."
    case "sign_up_refused":
      if (serverMessageIncludes("VPN")) return "Sign-ups through a VPN, proxy or Tor aren't accepted here. Turn it off and try again."
      if (serverMessageIncludes("data-centre")) return "Sign-ups from a data-centre network aren't accepted here. Try from your home or phone connection."
      if (serverMessageIncludes("country")) return "Sign-ups from your country aren't accepted here."
      return "Sign-ups from your network aren't accepted here."
    case "sign_up_screening_unavailable":
      return "We couldn't check your network just now. Try again in a minute."
    case "price_unavailable":
      return "Prices aren't available right now. Try again in a minute."
    case "service_unavailable":
      return "The server can't finish that right now. Try again in a minute."
    case "factory_not_deployed":
      return "Payments aren't set up on this network yet."
    case "chain_not_enabled":
      return "That network isn't available here."
    case "asset_not_supported":
      return "That coin isn't available here."
    case "unauthorized":
      return "Sign in with your passkey first."
    case "account_session_expired":
      if (serverMessageIncludes("account is gone")) return "This account no longer exists. Sign up again."
      return "Your sign-in ended. Sign in with your passkey again."
    case "passkey_unknown":
      if (serverMessageIncludes("account is gone")) return "This passkey's account no longer exists. Sign up again."
      return "This passkey isn't registered here, or it was deleted. Pick another passkey, or sign up."
    case "passkey_response_invalid":
      return "The passkey's answer didn't check out. Try again."
    case "passkey_challenge_expired":
      return "The passkey check timed out or was already used. Try again."
    case "passkey_not_for_this_account":
      return "That passkey belongs to another account. Use one of this account's passkeys."
    case "passkey_enrollment_link_unknown":
      return "This passkey link isn't valid. Check it was copied whole, or ask for a new one."
    case "passkey_enrollment_link_used":
      return "This passkey link was already used. Ask for a new one."
    case "passkey_enrollment_link_expired":
      return "This passkey link has expired. Ask for a new one."
    case "passkey_already_registered":
      return "This passkey is already registered here."
    case "username_taken":
      return "That username is taken. Pick another."
    case "last_passkey":
      return "This is your only passkey. Add another before deleting it."
    case "account_passkey_not_found":
      return "That passkey is already gone. Reload the page."
    case "api_token_invalid":
      return "That API token is wrong or was revoked."
    case "api_token_not_found":
      return "That API token is already gone. Reload the page."
    case "too_many_api_tokens":
      return "You have the most API tokens an account can have. Revoke one you don't use, then make a new one."
    case "webhook_not_set":
      if (serverMessageIncludes("callback_url")) return "Set a webhook URL in Settings first: its secret signs the invoice's callbacks."
      return "Set a webhook URL first."
    case "invoice_schedule_not_found":
      return "That schedule is gone. Reload the page."
    case "invoice_schedule_state_conflict":
      if (serverMessageIncludes("cancelled")) return "This schedule was cancelled, so it can't change anymore."
      if (serverMessageIncludes("completed")) return "This schedule reached its end, so it can't change anymore."
      return "This schedule changed in the meantime. Reload the page and try again."
    case "too_many_invoice_schedules":
      return "You have the most repeating invoices an account can have. Cancel one you don't need, then try again."
    case "order_id_prefix_taken":
      if (serverMessageIncludes("is already used")) return "An invoice already has the order id this one's first invoice would get. Pick another, or leave it empty."
      return "Another repeating invoice already uses that order id. Pick another, or leave it empty."
    case "invoice_not_refreshable":
      return "Money already reached this order's expired invoice, so it can't be refreshed. Settle that invoice first."
    case "webhook_disabled":
      return "Your server answered 410 Gone, so the webhook is off. Set the URL again to turn it back on."
    case "webhook_delivery_not_found":
      return "That delivery is gone. Reload the page."
    case "discord_webhook_not_set":
      return "Set a Discord webhook URL first."
    case "not_operator_account":
      return "Only the operator's account gets operator alerts."
    case "discord_webhook_in_use":
      return "Another account already uses this Discord webhook. Make a new webhook in the channel's settings and paste its URL."
    case "monero_address_invalid":
      return "That's not a Monero primary address for this network. Copy the address that starts with 4 (or 5 on stagenet) from your wallet."
    case "monero_view_key_mismatch":
      return "That view key belongs to another wallet. Copy the secret view key of this address's wallet."
    case "monero_wallet_unchanged":
      return "That's already your Monero wallet."
    case "monero_address_taken":
      return "Another account uses this Monero wallet. Use a wallet of your own made for this."
    case "monero_wallet_change_conflict":
      return "Your Monero wallet changed at the same moment somewhere else. Reload the page and try again."
    case "monero_wallet_missing":
      return "Set a Monero wallet in Settings first."
    case "monero_lookahead_exhausted":
      return "Your Monero wallet can't see more unpaid links yet. Widen its lookahead, then tap “I set it” in Settings."
    case "monero_sidecar_unavailable":
      return "Monero isn't available right now. Try again in a minute."
    case "chain_kind_not_supported":
      if (serverMessageIncludes("invoice schedules")) return "Repeating invoices aren't made on Monero. Pick another network."
      return "That network doesn't do this."
    case "origin_mismatch":
      return "Account changes only work on this site's own address. Open it there and try again."
    case "not_found":
      return "That isn't here anymore. Reload the page."
    case "bad_request":
      return "That request couldn't be read. Reload the page and try again."
    case "method_not_allowed":
      return "This page is out of date. Reload it and try again."
    case "forbidden":
      return "That isn't allowed."
    case "conflict":
      return "That changed in the meantime. Reload the page and try again."
    case "internal_error":
      return "Something broke on our side. Try again in a minute."
    default:
      return capitalizeSentence(serverMessage)
  }
}

export function describeError(error: unknown): string {
  if (error instanceof ApiError) return describeApiError(error)
  if (error instanceof Error && error.message) return error.message
  return "Something went wrong."
}
