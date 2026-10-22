// The docs' example responses: real answers of the API, captured by the fork e2e's merchant operations and invoice
// schedule scenarios and anonymised (e2e/merchant_operations_scenarios.py and e2e/invoice_schedule_scenarios.py write
// developer_docs_responses.json, and fail when the API's shapes drift from it). The webhook events are the API's own samples, the ones "Send test" in Settings sends.
import capturedResponses from "./developer_docs_responses.json"

export type DocumentationExampleResponse = {
  // The status line the docs show, e.g. "201 Created".
  statusText: string
  body: string
}

function describeJsonResponse(statusText: string, responseJson: unknown): DocumentationExampleResponse {
  return { statusText, body: JSON.stringify(responseJson, null, 2) }
}

export const createInvoiceResponse = describeJsonResponse("201 Created", capturedResponses.create_invoice)
export const readInvoiceResponse = describeJsonResponse("200 OK", capturedResponses.read_invoice)
export const findOrderResponse = describeJsonResponse("200 OK", capturedResponses.find_order)
export const listCustomerInvoicesResponse = describeJsonResponse("200 OK", capturedResponses.list_customer_invoices)
export const discoverChainsResponse = describeJsonResponse("200 OK", capturedResponses.list_chains)
export const choosePaymentPairResponse = describeJsonResponse("200 OK", capturedResponses.choose_payment_pair)
export const listRatesResponse = describeJsonResponse("200 OK", capturedResponses.list_rates)
export const cancelInvoiceResponse = describeJsonResponse("200 OK", capturedResponses.cancel_invoice)
export const refreshExpiredOrderResponse = describeJsonResponse("201 Created", capturedResponses.refresh_expired_order)
export const shopLinksAndCallbackResponse = describeJsonResponse("201 Created", capturedResponses.shop_links_and_callback)
export const listInvoiceEventsResponse = describeJsonResponse(
  "200 OK (first events)",
  capturedResponses.list_invoice_events,
)
export const createPaymentLinkResponse = describeJsonResponse("201 Created", capturedResponses.create_payment_link)
export const closePaymentLinkResponse = describeJsonResponse("200 OK", capturedResponses.close_payment_link)
export const createStaticDepositAddressResponse = describeJsonResponse(
  "201 Created",
  capturedResponses.create_static_deposit_address,
)
export const readStaticDepositAddressResponse = describeJsonResponse("200 OK", capturedResponses.read_static_deposit_address)
export const listStaticDepositAddressesResponse = describeJsonResponse(
  "200 OK",
  capturedResponses.list_static_deposit_addresses,
)
export const createInvoiceScheduleResponse = describeJsonResponse("201 Created", capturedResponses.create_invoice_schedule)
export const readInvoiceScheduleResponse = describeJsonResponse("200 OK", capturedResponses.read_invoice_schedule)
export const listInvoiceScheduleInvoicesResponse = describeJsonResponse("200 OK", capturedResponses.list_invoice_schedule_invoices)
export const pauseInvoiceScheduleResponse = describeJsonResponse("200 OK", capturedResponses.pause_invoice_schedule)
export const resumeInvoiceScheduleResponse = describeJsonResponse("200 OK", capturedResponses.resume_invoice_schedule)
export const cancelInvoiceScheduleResponse = describeJsonResponse("200 OK", capturedResponses.cancel_invoice_schedule)
export const exportInvoicesResponse: DocumentationExampleResponse = {
  statusText: "200 OK, text/csv (first rows)",
  body: capturedResponses.invoice_export_csv.replaceAll("\r\n", "\n"),
}

// Every event type "Send test" has a sample for (no refund events), in the order an invoice's life meets them, then
// the customer address events.
export const webhookEventExamples = Object.entries(capturedResponses.webhook_events).map(([eventType, eventBody]) => ({
  eventType,
  body: JSON.stringify(eventBody, null, 2),
}))
