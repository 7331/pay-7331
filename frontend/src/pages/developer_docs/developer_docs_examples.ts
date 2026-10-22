import { deploymentIsSandbox } from "@/lib/deployment_mode"

const productionApiOrigin = "https://pay.7331.org"
export const sandboxApiOrigin = "https://pay-sandbox.7331.org"
// On the sandbox every example calls the sandbox, so a copied request never reaches production with a sandbox token,
// and names the sandbox's testnets, which are the only chain keys it accepts.
const apiOrigin = deploymentIsSandbox ? sandboxApiOrigin : productionApiOrigin
export const polygonChainKey = deploymentIsSandbox ? "polygon_amoy" : "polygon"
export const arbitrumChainKey = deploymentIsSandbox ? "arbitrum_sepolia" : "arbitrum"
const apiPathPrefix = "/api/v1"
export const apiBaseUrl = `${apiOrigin}${apiPathPrefix}`
// ruff format's default line length: every Python example is laid out as ruff format lays it out.
const pythonLineLength = 88

export const codeLanguages = ["requests", "aiohttp", "httpx", "curl"] as const
export type CodeLanguage = (typeof codeLanguages)[number]

type ApiRequestSpecification = {
  method: "GET" | "POST"
  // Under /api/v1; `{name}` is filled from the value `pathVariables` names for it (a Python constant, a shell variable).
  path: string
  pathVariables?: Record<string, PlaceholderName>
  query?: Record<string, string>
  body?: Record<string, RequestBodyValue>
  authenticated: boolean
  // What the Python examples call their result and their aiohttp coroutine.
  resultName: string
  aiohttpFunctionName: string
}

export type ApiRequestExample = Record<CodeLanguage, string>

// Amounts stay strings; a count (`quantity`) is a number, a switch (`refresh_expired`) a boolean and a coin list
// (`assets`) a list of strings.
type RequestBodyValue = string | number | boolean | string[]

// A flat object of strings, numbers, booleans and string lists: JSON writes `true`, Python `True`; a list is spaced
// as ruff format writes it.
function formatObjectLiteral(entries: Record<string, RequestBodyValue>, language: "json" | "python"): string {
  const formatValue = (value: RequestBodyValue) =>
    Array.isArray(value)
      ? `[${value.map((item) => JSON.stringify(item)).join(", ")}]`
      : typeof value === "boolean" && language === "python" ? (value ? "True" : "False") : JSON.stringify(value)
  return `{${Object.entries(entries).map(([key, value]) => `${JSON.stringify(key)}: ${formatValue(value)}`).join(", ")}}`
}

function buildCurlExample(specification: ApiRequestSpecification): string {
  let curlPath = specification.path
  for (const [variableName, shellVariableName] of Object.entries(specification.pathVariables ?? {})) {
    curlPath = curlPath.replace(`{${variableName}}`, `$${shellVariableName}`)
  }
  const url = curlPath.includes("$") ? `"${apiBaseUrl}${curlPath}"` : `${apiBaseUrl}${curlPath}`
  // Plain curl prints the API's error JSON as it is; --fail-with-body only adds an exit code for scripts.
  // A POST with no body says so: without -d, curl sends a GET.
  const lines = [specification.query ? `curl --get ${url}` : specification.method === "POST" && !specification.body ? `curl -X POST ${url}` : `curl ${url}`]
  if (specification.authenticated) lines.push(`  -H "Authorization: Bearer $PAY_API_TOKEN"`)
  if (specification.body) {
    lines.push("  -H 'Content-Type: application/json'", `  -d '${formatObjectLiteral(specification.body, "json")}'`)
  }
  for (const [name, value] of Object.entries(specification.query ?? {})) lines.push(`  --data-urlencode '${name}=${value}'`)
  return lines.join(" \\\n")
}

// What each value the examples read stands for: Python names it in a constant, curl reads the shell variable.
const pythonPlaceholderValues = {
  PAY_API_TOKEN: "pay_tok_...",
  PAY_INVOICE_ID: "inv_...",
  PAY_PAYMENT_LINK_ID: "lnk_...",
  PAY_STATIC_DEPOSIT_ADDRESS_ID: "sda_...",
  PAY_INVOICE_SCHEDULE_ID: "sch_...",
} as const
type PlaceholderName = keyof typeof pythonPlaceholderValues

function buildPythonPreamble(specification: ApiRequestSpecification, clientModule: string): string[] {
  const lines = clientModule === "aiohttp" ? ["import asyncio", ""] : []
  lines.push(`import ${clientModule}`, "")
  const constantNames: PlaceholderName[] = [
    ...(specification.authenticated ? ["PAY_API_TOKEN" as const] : []),
    ...Object.values(specification.pathVariables ?? {}),
  ]
  for (const constantName of constantNames) lines.push(`${constantName} = "${pythonPlaceholderValues[constantName]}"`)
  if (constantNames.length > 0) lines.push("")
  return lines
}

// A keyword argument whose value is a dict literal (`json=`, `params=`): written on one line when it fits, else one
// entry per line with a trailing comma, as ruff format splits it.
type PythonDictArgument = { keyword: string; entries: Record<string, RequestBodyValue> }
type PythonCallArgument = string | PythonDictArgument

function formatPythonArgumentOnOneLine(callArgument: PythonCallArgument): string {
  return typeof callArgument === "string" ? callArgument : `${callArgument.keyword}=${formatObjectLiteral(callArgument.entries, "python")}`
}

function formatPythonArgumentLines(indent: string, callArgument: PythonCallArgument): string[] {
  const oneLine = `${indent}${formatPythonArgumentOnOneLine(callArgument)},`
  if (typeof callArgument === "string" || oneLine.length <= pythonLineLength) return [oneLine]
  return [
    `${indent}${callArgument.keyword}={`,
    ...Object.keys(callArgument.entries).map(
      (key) => `${indent}    ${formatObjectLiteral({ [key]: callArgument.entries[key] }, "python").slice(1, -1)},`,
    ),
    `${indent}},`,
  ]
}

// One line if it fits, else the arguments on one line of their own, else one argument per line.
function formatPythonCall(indent: string, callee: string, callArguments: PythonCallArgument[], suffix: string): string[] {
  const argumentsText = callArguments.map(formatPythonArgumentOnOneLine).join(", ")
  const singleLine = `${indent}${callee}(${argumentsText})${suffix}`
  if (singleLine.length <= pythonLineLength) return [singleLine]
  const argumentsLine = `${indent}    ${argumentsText}`
  if (argumentsLine.length <= pythonLineLength) return [`${indent}${callee}(`, argumentsLine, `${indent})${suffix}`]
  return [
    `${indent}${callee}(`,
    ...callArguments.flatMap((callArgument) => formatPythonArgumentLines(`${indent}    `, callArgument)),
    `${indent})${suffix}`,
  ]
}

function buildPythonRequestArguments(specification: ApiRequestSpecification, urlPrefix: string): PythonCallArgument[] {
  let pythonUrl = `${urlPrefix}${specification.path}`
  for (const [variableName, constantName] of Object.entries(specification.pathVariables ?? {})) {
    pythonUrl = pythonUrl.replace(`{${variableName}}`, `{${constantName}}`)
  }
  const requestArguments: PythonCallArgument[] = [specification.pathVariables ? `f"${pythonUrl}"` : `"${pythonUrl}"`]
  if (specification.body) requestArguments.push({ keyword: "json", entries: specification.body })
  if (specification.query) requestArguments.push({ keyword: "params", entries: specification.query })
  return requestArguments
}

const authorizationHeaders = `{"Authorization": f"Bearer {PAY_API_TOKEN}"}`

function buildBlockingPythonExample(specification: ApiRequestSpecification, clientModule: "requests" | "httpx"): string {
  const callArguments = buildPythonRequestArguments(specification, apiBaseUrl)
  if (specification.authenticated) callArguments.splice(1, 0, `headers=${authorizationHeaders}`)
  // requests waits forever without a timeout and httpx gives up after 5 seconds; creating an invoice prices every
  // offered chain, which can take longer.
  callArguments.push("timeout=30")
  return [
    ...buildPythonPreamble(specification, clientModule),
    ...formatPythonCall("", `response = ${clientModule}.${specification.method.toLowerCase()}`, callArguments, ""),
    "response.raise_for_status()",
    `${specification.resultName} = response.json()`,
  ].join("\n")
}

function buildAiohttpExample(specification: ApiRequestSpecification): string {
  // base_url is the bare origin: aiohttp before 3.11 refuses a base_url with a path.
  const sessionArguments = [`base_url="${apiOrigin}"`]
  if (specification.authenticated) sessionArguments.push(`headers=${authorizationHeaders}`)
  sessionArguments.push("raise_for_status=True")
  const sessionMethod = specification.method.toLowerCase()
  return [
    ...buildPythonPreamble(specification, "aiohttp"),
    "",
    `async def ${specification.aiohttpFunctionName}():`,
    // One parenthesized `async with` for both, as ruff's SIM117 asks.
    "    async with (",
    ...formatPythonCall("        ", "aiohttp.ClientSession", sessionArguments, " as session,"),
    ...formatPythonCall("        ", `session.${sessionMethod}`, buildPythonRequestArguments(specification, apiPathPrefix), " as response,"),
    "    ):",
    "        return await response.json()",
    "",
    "",
    `${specification.resultName} = asyncio.run(${specification.aiohttpFunctionName}())`,
  ].join("\n")
}

function buildApiRequestExample(specification: ApiRequestSpecification): ApiRequestExample {
  return {
    requests: buildBlockingPythonExample(specification, "requests"),
    aiohttp: buildAiohttpExample(specification),
    httpx: buildBlockingPythonExample(specification, "httpx"),
    curl: buildCurlExample(specification),
  }
}

// A file the API streams (the CSV export): every example writes it to disk a chunk at a time, so a large account's
// export never sits in memory, on the client either.
function buildDownloadExample(specification: ApiRequestSpecification, fileName: string): ApiRequestExample {
  const requestsArguments = buildPythonRequestArguments(specification, apiBaseUrl)
  if (specification.authenticated) requestsArguments.splice(1, 0, `headers=${authorizationHeaders}`)
  const writeChunks = (indent: string, chunkLoop: string) => [
    `${indent}with open("${fileName}", "wb") as csv_file:`,
    `${indent}    ${chunkLoop}`,
    `${indent}        csv_file.write(chunk)`,
  ]
  const requestsExample = [
    ...buildPythonPreamble(specification, "requests"),
    ...formatPythonCall("", "with requests.get", [...requestsArguments, "stream=True", "timeout=30"], " as response:"),
    "    response.raise_for_status()",
    ...writeChunks("    ", "for chunk in response.iter_content(chunk_size=65536):"),
  ].join("\n")
  const httpxExample = [
    ...buildPythonPreamble(specification, "httpx"),
    ...formatPythonCall("", "with httpx.stream", [`"${specification.method}"`, ...requestsArguments, "timeout=30"], " as response:"),
    "    response.raise_for_status()",
    ...writeChunks("    ", "for chunk in response.iter_bytes():"),
  ].join("\n")
  const sessionArguments = [`base_url="${apiOrigin}"`]
  if (specification.authenticated) sessionArguments.push(`headers=${authorizationHeaders}`)
  sessionArguments.push("raise_for_status=True")
  const aiohttpExample = [
    ...buildPythonPreamble(specification, "aiohttp"),
    "",
    `async def ${specification.aiohttpFunctionName}():`,
    "    async with (",
    ...formatPythonCall("        ", "aiohttp.ClientSession", sessionArguments, " as session,"),
    ...formatPythonCall("        ", `session.${specification.method.toLowerCase()}`, buildPythonRequestArguments(specification, apiPathPrefix), " as response,"),
    "    ):",
    ...writeChunks("        ", "async for chunk in response.content.iter_chunked(65536):"),
    "",
    "",
    `asyncio.run(${specification.aiohttpFunctionName}())`,
  ].join("\n")
  return {
    requests: requestsExample,
    aiohttp: aiohttpExample,
    httpx: httpxExample,
    curl: `${buildCurlExample(specification)} \\\n  -o ${fileName}`,
  }
}

export const listRatesExample = buildApiRequestExample({
  method: "GET", path: "/rates", authenticated: false, resultName: "rates", aiohttpFunctionName: "list_rates",
})

export const exportInvoicesExample = buildDownloadExample({
  method: "GET", path: "/invoices/export.csv",
  query: { created_from: "2026-10-01T00:00:00Z", created_before: "2026-11-01T00:00:00Z" }, authenticated: true,
  resultName: "invoices_csv", aiohttpFunctionName: "export_invoices",
}, "invoices.csv")

export const discoverChainsExample = buildApiRequestExample({
  method: "GET", path: "/chains", authenticated: false, resultName: "chains", aiohttpFunctionName: "list_chains",
})

export const createInvoiceExample = buildApiRequestExample({
  method: "POST", path: "/invoices", body: { amount: "5", currency: "USD" }, authenticated: true,
  resultName: "invoice", aiohttpFunctionName: "create_invoice",
})

export const readInvoiceExample = buildApiRequestExample({
  method: "GET", path: "/invoices/{invoice_id}", pathVariables: { invoice_id: "PAY_INVOICE_ID" }, authenticated: true,
  resultName: "invoice", aiohttpFunctionName: "read_invoice",
})

export const findOrderExample = buildApiRequestExample({
  method: "GET", path: "/invoices", query: { order_id: "order-1042" }, authenticated: true,
  resultName: "invoice_page", aiohttpFunctionName: "find_order",
})

export const listCustomerInvoicesExample = buildApiRequestExample({
  method: "GET", path: "/invoices", query: { order_id_prefix: "customer-77:", status: "paid" }, authenticated: true,
  resultName: "invoice_page", aiohttpFunctionName: "list_customer_invoices",
})

export const refreshExpiredOrderExample = buildApiRequestExample({
  method: "POST", path: "/invoices",
  body: { amount: "5", currency: "USD", order_id: "order-1042", refresh_expired: true }, authenticated: true,
  resultName: "invoice", aiohttpFunctionName: "refresh_expired_order",
})

export const shopLinksAndCallbackExample = buildApiRequestExample({
  method: "POST", path: "/invoices",
  body: {
    amount: "5", currency: "USD", order_id: "order-1043", return_url: "https://shop.example/cart",
    success_url: "https://shop.example/thanks", callback_url: "https://shop.example/pay-events",
  },
  authenticated: true, resultName: "invoice", aiohttpFunctionName: "create_invoice",
})

export const cancelInvoiceExample = buildApiRequestExample({
  method: "POST", path: "/invoices/{invoice_id}/cancel", pathVariables: { invoice_id: "PAY_INVOICE_ID" },
  authenticated: true, resultName: "invoice", aiohttpFunctionName: "cancel_invoice",
})

export const listInvoiceEventsExample = buildApiRequestExample({
  method: "GET", path: "/events", query: { limit: "50" }, authenticated: true,
  resultName: "event_page", aiohttpFunctionName: "list_invoice_events",
})

export const createPaymentLinkExample = buildApiRequestExample({
  method: "POST", path: "/payment-links",
  body: { amount: "5", currency: "USD", chain: polygonChainKey, assets: ["USDC", "POL"], quantity: 10 }, authenticated: true,
  resultName: "payment_link", aiohttpFunctionName: "create_payment_link",
})

export const closePaymentLinkExample = buildApiRequestExample({
  method: "POST", path: "/payment-links/{payment_link_id}/close", pathVariables: { payment_link_id: "PAY_PAYMENT_LINK_ID" },
  authenticated: true, resultName: "payment_link", aiohttpFunctionName: "close_payment_link",
})

export const createInvoiceScheduleExample = buildApiRequestExample({
  method: "POST", path: "/invoice-schedules",
  body: {
    amount: "5", currency: "USD", chain: polygonChainKey, assets: ["USDC"], interval: "monthly",
    order_id_prefix: "customer-77-premium", ends_after_invoice_count: 12,
  },
  authenticated: true, resultName: "invoice_schedule", aiohttpFunctionName: "create_invoice_schedule",
})

export const readInvoiceScheduleExample = buildApiRequestExample({
  method: "GET", path: "/invoice-schedules/{invoice_schedule_id}", pathVariables: { invoice_schedule_id: "PAY_INVOICE_SCHEDULE_ID" },
  authenticated: true, resultName: "invoice_schedule", aiohttpFunctionName: "read_invoice_schedule",
})

export const listInvoiceScheduleInvoicesExample = buildApiRequestExample({
  method: "GET", path: "/invoice-schedules/{invoice_schedule_id}/invoices",
  pathVariables: { invoice_schedule_id: "PAY_INVOICE_SCHEDULE_ID" },
  authenticated: true, resultName: "invoice_list", aiohttpFunctionName: "list_invoice_schedule_invoices",
})

export const pauseInvoiceScheduleExample = buildApiRequestExample({
  method: "POST", path: "/invoice-schedules/{invoice_schedule_id}/pause", pathVariables: { invoice_schedule_id: "PAY_INVOICE_SCHEDULE_ID" },
  authenticated: true, resultName: "invoice_schedule", aiohttpFunctionName: "pause_invoice_schedule",
})

export const resumeInvoiceScheduleExample = buildApiRequestExample({
  method: "POST", path: "/invoice-schedules/{invoice_schedule_id}/resume", pathVariables: { invoice_schedule_id: "PAY_INVOICE_SCHEDULE_ID" },
  authenticated: true, resultName: "invoice_schedule", aiohttpFunctionName: "resume_invoice_schedule",
})

export const cancelInvoiceScheduleExample = buildApiRequestExample({
  method: "POST", path: "/invoice-schedules/{invoice_schedule_id}/cancel", pathVariables: { invoice_schedule_id: "PAY_INVOICE_SCHEDULE_ID" },
  authenticated: true, resultName: "invoice_schedule", aiohttpFunctionName: "cancel_invoice_schedule",
})

export const offeredNetworksExample = `"assets_by_chain": {
  "${polygonChainKey}": ["USDC", "POL"],
  "${arbitrumChainKey}": ["USDC"]
}`

export const choosePaymentPairExample = buildApiRequestExample({
  method: "POST", path: "/public/invoices/{invoice_id}/asset", pathVariables: { invoice_id: "PAY_INVOICE_ID" },
  body: { chain: polygonChainKey, asset: "USDC" }, authenticated: false, resultName: "invoice",
  aiohttpFunctionName: "choose_payment_pair",
})

export const createStaticDepositAddressExample = buildApiRequestExample({
  method: "POST", path: "/static-deposit-addresses", body: { customer_reference: "customer-77", chain: polygonChainKey, assets: ["USDC"] },
  authenticated: true, resultName: "static_deposit_address", aiohttpFunctionName: "create_static_deposit_address",
})

export const readStaticDepositAddressExample = buildApiRequestExample({
  method: "GET", path: "/static-deposit-addresses/{static_deposit_address_id}",
  pathVariables: { static_deposit_address_id: "PAY_STATIC_DEPOSIT_ADDRESS_ID" }, authenticated: true,
  resultName: "static_deposit_address", aiohttpFunctionName: "read_static_deposit_address",
})

export const listStaticDepositAddressesExample = buildApiRequestExample({
  method: "GET", path: "/static-deposit-addresses", query: { customer_reference_prefix: "customer-" }, authenticated: true,
  resultName: "static_deposit_address_page", aiohttpFunctionName: "list_static_deposit_addresses",
})

export const verifyWebhookExample = `import base64
import hashlib
import hmac
import json
import time

PAY_WEBHOOK_SECRET = "whsec_..."
TIMESTAMP_TOLERANCE_SECONDS = 300


def verify_pay_event(raw_body: bytes, headers: dict[str, str]):
    headers = {name.lower(): value for name, value in headers.items()}
    message_id = headers.get("webhook-id", "")
    timestamp = headers.get("webhook-timestamp", "")
    if not (message_id and timestamp.isascii() and timestamp.isdigit()):
        raise ValueError("webhook headers missing or malformed")
    if abs(int(time.time()) - int(timestamp)) > TIMESTAMP_TOLERANCE_SECONDS:
        raise ValueError("webhook timestamp too old or too new")
    key = base64.b64decode(PAY_WEBHOOK_SECRET.removeprefix("whsec_"))
    signed_content = f"{message_id}.{timestamp}.".encode() + raw_body
    digest = hmac.new(key, signed_content, hashlib.sha256).digest()
    expected_signature = base64.b64encode(digest)
    for signature in headers.get("webhook-signature", "").split():
        version, _, value = signature.partition(",")
        if version == "v1" and hmac.compare_digest(value.encode(), expected_signature):
            return message_id, json.loads(raw_body)
    raise ValueError("webhook signature does not match")`
