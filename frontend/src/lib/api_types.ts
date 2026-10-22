// The JSON the pages build against, named for the pages. Every wire type is an alias of the API's OpenAPI document,
// generated into ./generated/api_schema.ts by `pnpm generate:api-types`; never edit that file by hand. Amounts are
// decimal strings, times ISO 8601 UTC strings.

import type { components } from "@/lib/generated/api_schema"

type ApiSchemas = components["schemas"]

export type InvoiceStatus = ApiSchemas["InvoiceStatus"]
export type ChainAsset = ApiSchemas["ChainAsset"]
export type InvoiceChain = ApiSchemas["InvoiceChain"]
export type ListedChain = ApiSchemas["ListedChainResponse"]
export type ChainListResponse = ApiSchemas["ChainListResponse"]

// The networks and coins a new invoice, quantity link or schedule offers (PLAN.md section 15): an EVM one sends
// `chains` and the exact ticks per network in `assets_by_chain` (which wins), plus `assets`, the union, for older
// readers; a Monero one sends its one `chain`. The three create requests share these fields.
export type InvoiceNetworkRequestFields = Pick<ApiSchemas["CreateInvoiceRequest"], "chain" | "chains" | "assets" | "assets_by_chain">

export type PaymentCheckOutcome = ApiSchemas["PaymentCheckOutcome"]
export type PaymentCheckResponse = ApiSchemas["PaymentCheckResponse"]
export type PaymentOption = ApiSchemas["PaymentOption"]
export type PublicInvoice = ApiSchemas["InvoicePublicResponse"]
export type AccountInvoice = ApiSchemas["InvoiceAccountResponse"]
export type AccountInvoicePage = ApiSchemas["InvoicePageResponse"]

export type PaymentLinkStatus = ApiSchemas["PaymentLinkStatus"]
export type PublicPaymentLink = ApiSchemas["PaymentLinkPublicResponse"]
export type AccountPaymentLink = ApiSchemas["PaymentLinkAccountResponse"]
export type AccountPaymentLinkDetail = ApiSchemas["PaymentLinkDetailResponse"]
export type AccountPaymentLinkList = ApiSchemas["PaymentLinkListResponse"]

export type InvoiceScheduleStatus = ApiSchemas["InvoiceScheduleStatus"]
export type InvoiceScheduleInterval = ApiSchemas["InvoiceScheduleInterval"]
export type AccountInvoiceSchedule = ApiSchemas["InvoiceScheduleResponse"]
export type AccountInvoiceScheduleList = ApiSchemas["InvoiceScheduleListResponse"]
export type AccountInvoiceScheduleInvoiceList = ApiSchemas["InvoiceScheduleInvoiceListResponse"]

export type RefundOfferItem = ApiSchemas["RefundOfferItemResponse"]
export type AccountSession = ApiSchemas["AccountSessionResponse"]
export type AccountPasskey = ApiSchemas["AccountPasskeyResponse"]
export type AccountApiToken = ApiSchemas["AccountApiTokenResponse"]
export type CreatedAccountApiToken = ApiSchemas["CreatedAccountApiTokenResponse"]
export type AccountWebhookDelivery = ApiSchemas["AccountWebhookDeliveryResponse"]
export type AccountWebhook = ApiSchemas["AccountWebhookResponse"]
export type ChangedAccountWebhook = ApiSchemas["ChangedAccountWebhookResponse"]
export type AccountDiscordWebhook = ApiSchemas["AccountDiscordWebhookResponse"]

export type StaticDepositPayment = ApiSchemas["StaticDepositAddressPayment"]
export type StaticDepositAddress = ApiSchemas["StaticDepositAddressResponse"]
export type StaticDepositAddressList = ApiSchemas["StaticDepositAddressListResponse"]

export type MoneroChainSummary = ApiSchemas["MoneroChainSummary"]
export type AccountMoneroWallet = ApiSchemas["MoneroWallet"]
export type AccountMoneroWalletSettings = ApiSchemas["MoneroWalletSettingsResponse"]

// Account events. The OpenAPI document types every event's `data` as any JSON; these narrow it by `type`, as the
// backend's AccountEventType documents each payload, so a page can switch on `type` and read `data`.
type AccountEventType = ApiSchemas["AccountEventType"]

type AccountEventWithData<EventType extends AccountEventType, InvoiceId extends string | null, EventData> = Omit<
  ApiSchemas["AccountEventResponse"],
  "type" | "invoice_id" | "data"
> & { type: EventType; invoice_id: InvoiceId; data: EventData }

// An invoice's or refund's event: the account's Invoice resource at the change.
type InvoiceAccountEvent = AccountEventWithData<
  Extract<AccountEventType, `invoice.${string}` | `refund.${string}` | "schedule.invoice_created">,
  string,
  AccountInvoice
>

// A static deposit address's payment (`payment_received`, `stray_payment`) or sweep (`swept`, `sweep_failed`).
export type StaticDepositAddressAccountEvent = AccountEventWithData<
  Extract<AccountEventType, `static_address.${string}`>,
  null,
  {
    static_deposit_address: StaticDepositAddress
    payment?: StaticDepositPayment
    sweep?: { tx_hash: string | null; status: string }
  }
>

type PayoutAddressChangedAccountEvent = AccountEventWithData<
  "account.payout_address_changed",
  null,
  { previous_payout_address: string; payout_address: string }
>

// The account set (or changed) its Monero wallet behind a passkey check; ids only.
type MoneroWalletSetAccountEvent = AccountEventWithData<"account.monero_wallet_set", null, { chain_key: string }>

// A passkey added through an operator-minted enrollment link, added or deleted in /settings; no data.
type PasskeyAccountEvent = AccountEventWithData<
  "account.passkey_enrolled_by_link" | "account.passkey_added" | "account.passkey_deleted",
  null,
  Record<string, never>
>

// Something the operator must look at, only on the operator's own account; a reminder carries when the problem first
// opened (both missing on alerts before 2026-10-18); chain_key is null on the host's backup alerts.
export type OperatorAlertAccountEvent = AccountEventWithData<
  "operator.alert",
  null,
  {
    kind: string
    message: string
    problem_key: string
    chain_key: string | null
    invoice_id: string | null
    first_seen_at?: string
    is_reminder?: boolean
  }
>

export type AccountEvent =
  | InvoiceAccountEvent
  | StaticDepositAddressAccountEvent
  | PayoutAddressChangedAccountEvent
  | MoneroWalletSetAccountEvent
  | PasskeyAccountEvent
  | OperatorAlertAccountEvent

// `GET /api/v1/events`: the account's events newest first; `next_cursor` reads the older page (PLAN.md section 8.1).
export type AccountEventPage = Omit<ApiSchemas["AccountEventPageResponse"], "events"> & { events: AccountEvent[] }
