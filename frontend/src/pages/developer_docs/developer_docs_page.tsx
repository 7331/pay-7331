import { ArrowUpRight, ChevronRight } from "lucide-react"
import { useEffect, useState, type ReactNode } from "react"
import { Link } from "react-router"

import { BrandMark } from "@/components/brand_mark"
import { SandboxBanner } from "@/components/sandbox_banner"
import { TapToCopyButton } from "@/components/tap_to_copy_button"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { deploymentIsSandbox } from "@/lib/deployment_mode"
import {
  type ApiRequestExample,
  apiBaseUrl,
  arbitrumChainKey,
  type CodeLanguage,
  cancelInvoiceExample,
  choosePaymentPairExample,
  cancelInvoiceScheduleExample,
  closePaymentLinkExample,
  createInvoiceScheduleExample,
  listInvoiceScheduleInvoicesExample,
  pauseInvoiceScheduleExample,
  readInvoiceScheduleExample,
  resumeInvoiceScheduleExample,
  codeLanguages,
  createInvoiceExample,
  createPaymentLinkExample,
  createStaticDepositAddressExample,
  discoverChainsExample,
  exportInvoicesExample,
  findOrderExample,
  listCustomerInvoicesExample,
  listInvoiceEventsExample,
  listRatesExample,
  listStaticDepositAddressesExample,
  offeredNetworksExample,
  polygonChainKey,
  readInvoiceExample,
  readStaticDepositAddressExample,
  refreshExpiredOrderExample,
  sandboxApiOrigin,
  shopLinksAndCallbackExample,
  verifyWebhookExample,
} from "@/pages/developer_docs/developer_docs_examples"
import {
  cancelInvoiceResponse,
  choosePaymentPairResponse,
  cancelInvoiceScheduleResponse,
  closePaymentLinkResponse,
  createInvoiceScheduleResponse,
  listInvoiceScheduleInvoicesResponse,
  pauseInvoiceScheduleResponse,
  readInvoiceScheduleResponse,
  resumeInvoiceScheduleResponse,
  createInvoiceResponse,
  createPaymentLinkResponse,
  createStaticDepositAddressResponse,
  discoverChainsResponse,
  exportInvoicesResponse,
  findOrderResponse,
  listCustomerInvoicesResponse,
  listInvoiceEventsResponse,
  listRatesResponse,
  listStaticDepositAddressesResponse,
  readInvoiceResponse,
  readStaticDepositAddressResponse,
  refreshExpiredOrderResponse,
  shopLinksAndCallbackResponse,
  type DocumentationExampleResponse,
  webhookEventExamples,
} from "@/pages/developer_docs/developer_docs_responses"
import "./developer_docs.css"

const documentationSections = [
  { id: "quickstart", title: "Quickstart" },
  { id: "invoices", title: "Invoices" },
  { id: "networks", title: "Networks & coins" },
  { id: "status", title: "Payment status" },
  { id: "webhooks", title: "Webhooks" },
  { id: "export", title: "CSV export" },
  { id: "rates", title: "Exchange rates" },
  { id: "quantity-links", title: "Quantity links" },
  { id: "scheduled-invoices", title: "Scheduled invoices" },
  // Production's docs leave the sandbox out until it makes invoices (its refund key needs testnet gas and factories,
  // PLAN.md section 16); the sandbox's own docs show it.
  ...(deploymentIsSandbox ? [{ id: "sandbox", title: "Sandbox" }] : []),
  { id: "static-deposit-addresses", title: "Customer addresses" },
]

function DocumentationNavigation() {
  return (
    <nav aria-label="Developer docs" className="grid grid-cols-2 gap-x-3 lg:grid-cols-1">
      {documentationSections.map((section) => (
        <a key={section.id} href={`#${section.id}`} className="flex min-h-11 items-center rounded-lg px-3 text-sm text-muted-foreground hover:bg-muted hover:text-foreground">
          {section.title}
        </a>
      ))}
    </nav>
  )
}

function DocumentationSection({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section id={id} aria-labelledby={`${id}-heading`} className="scroll-mt-24 space-y-5 border-t pt-9">
      <h2 id={`${id}-heading`} className="text-2xl font-semibold tracking-tight">{title}</h2>
      {children}
    </section>
  )
}

// Scrolls inside its box rather than wrapping: a wrapped Python line loses its indentation. Focusable, so a keyboard
// can scroll it; the outline sits inside, where the box's overflow-hidden cannot clip it.
function DocumentationCodePre({ label, code }: { label: string; code: string }) {
  return (
    <pre tabIndex={0} role="region" aria-label={label} className="overflow-x-auto p-4 text-[13px] leading-relaxed whitespace-pre focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-foreground"><code>{code}</code></pre>
  )
}

function DocumentationCodeBlock({ title, code }: { title: string; code: string }) {
  return (
    <div className="min-w-0 overflow-hidden rounded-xl border bg-muted/40">
      <div className="flex min-h-11 items-center justify-between gap-3 border-b px-4">
        <span className="text-xs font-medium text-muted-foreground">{title}</span>
        <TapToCopyButton textToCopy={code} accessibleLabel={`Copy ${title}`} feedbackShownBy="trailing_icon" className="flex min-h-11 items-center gap-2 px-2 text-xs text-foreground">
          Copy
        </TapToCopyButton>
      </div>
      <DocumentationCodePre label={title} code={code} />
    </div>
  )
}

// A request's real answer under its code, closed until asked for: a whole invoice is long on a phone. It scrolls inside
// its own box both ways, so the page never does.
function DocumentationResponseDisclosure({ title, exampleResponse }: { title: string; exampleResponse: DocumentationExampleResponse }) {
  return (
    <details className="documentation-response group border-t">
      <summary className="flex min-h-11 list-none items-center gap-2 px-4 text-xs font-medium text-muted-foreground [&::-webkit-details-marker]:hidden">
        <ChevronRight aria-hidden="true" className="size-4 shrink-0 transition-transform group-open:rotate-90" />
        Response
        <span className="min-w-0 truncate font-mono font-normal">{exampleResponse.statusText}</span>
      </summary>
      <div className="max-h-[28rem] overflow-y-auto border-t">
        <DocumentationCodePre label={`${title}: example response`} code={exampleResponse.body} />
      </div>
    </details>
  )
}

// One language choice for every request on the page: picking aiohttp in one block shows aiohttp in all of them.
function DocumentationRequestExampleBlock({
  title,
  example,
  exampleResponse,
  codeLanguage,
  onCodeLanguageChange,
}: {
  title: string
  example: ApiRequestExample
  exampleResponse: DocumentationExampleResponse
  codeLanguage: CodeLanguage
  onCodeLanguageChange: (codeLanguage: CodeLanguage) => void
}) {
  return (
    <div className="min-w-0 overflow-hidden rounded-xl border bg-muted/40">
      <div className="flex min-h-11 items-center justify-between gap-3 border-b px-4">
        <span className="text-xs font-medium text-muted-foreground">{title}</span>
        <TapToCopyButton textToCopy={example[codeLanguage]} accessibleLabel={`Copy ${title} (${codeLanguage})`} feedbackShownBy="trailing_icon" className="flex min-h-11 items-center gap-2 px-2 text-xs text-foreground">
          Copy
        </TapToCopyButton>
      </div>
      <Tabs value={codeLanguage} onValueChange={(value) => onCodeLanguageChange(value as CodeLanguage)} className="gap-0">
        <TabsList variant="line" aria-label={`${title}: language`} className="w-full justify-start gap-0 border-b px-2 group-data-horizontal/tabs:h-11">
          {codeLanguages.map((language) => (
            <TabsTrigger key={language} value={language} className="h-11 flex-none px-3 text-xs group-data-horizontal/tabs:after:bottom-0">
              {language}
            </TabsTrigger>
          ))}
        </TabsList>
        {codeLanguages.map((language) => (
          // The code is focusable itself, so the panel takes no tab stop of its own (WAI-ARIA APG tabs pattern).
          <TabsContent key={language} value={language} tabIndex={-1}>
            <DocumentationCodePre label={`${title} in ${language}`} code={example[language]} />
          </TabsContent>
        ))}
      </Tabs>
      <DocumentationResponseDisclosure title={title} exampleResponse={exampleResponse} />
    </div>
  )
}

// One webhook body per event type, picked from a list: fourteen tabs would not fit a phone.
function DocumentationWebhookEventExamples() {
  const [shownEventType, setShownEventType] = useState("invoice.paid")
  const shownEvent = webhookEventExamples.find((eventExample) => eventExample.eventType === shownEventType) ?? webhookEventExamples[0]
  if (!shownEvent) return null
  return (
    <div className="min-w-0 overflow-hidden rounded-xl border bg-muted/40">
      <div className="flex min-h-11 items-center justify-between gap-3 border-b px-4">
        <span className="text-xs font-medium text-muted-foreground">Example body</span>
        <TapToCopyButton textToCopy={shownEvent.body} accessibleLabel={`Copy the ${shownEvent.eventType} example`} feedbackShownBy="trailing_icon" className="flex min-h-11 items-center gap-2 px-2 text-xs text-foreground">
          Copy
        </TapToCopyButton>
      </div>
      <div className="border-b p-2">
        <Select value={shownEvent.eventType} onValueChange={setShownEventType}>
          <SelectTrigger id="developer_docs_webhook_event_type_select" aria-label="Event type" className="h-11! w-full rounded-lg font-mono text-sm">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {webhookEventExamples.map((eventExample) => (
              <SelectItem key={eventExample.eventType} value={eventExample.eventType} className="min-h-11 font-mono text-sm">
                {eventExample.eventType}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="max-h-[28rem] overflow-y-auto">
        <DocumentationCodePre label={`${shownEvent.eventType} example body`} code={shownEvent.body} />
      </div>
    </div>
  )
}

export function DeveloperDocsPage() {
  const [codeLanguage, setCodeLanguage] = useState<CodeLanguage>("requests")

  // The page is a lazy chunk: an initial /docs#section needs its anchor after the chunk mounts.
  useEffect(() => {
    document.getElementById(window.location.hash.slice(1))?.scrollIntoView()
  }, [])

  return (
    <div className="developer-docs min-h-dvh bg-background">
      <title>Developer docs · pay-7331</title>
      <meta name="description" content="Create a crypto invoice from Python or curl, read its status, and receive signed payment webhooks." />
      <a href="#quickstart" className="sr-only focus:not-sr-only focus:fixed focus:top-3 focus:left-3 focus:z-30 focus:rounded-lg focus:bg-background focus:p-3 focus:ring-2">Skip to quickstart</a>
      <SandboxBanner className="mx-5 mt-3 lg:mx-8" />
      <header className="sticky top-0 z-20 border-b bg-background/95 backdrop-blur">
        <div className="mx-auto flex min-h-16 max-w-6xl items-center justify-between gap-4 px-5 lg:px-8">
          <div className="flex items-center gap-4">
            <BrandMark />
            <span className="border-l pl-4 text-sm text-muted-foreground">Docs</span>
          </div>
          <Button variant="ghost" className="min-h-11" asChild>
            <Link to="/">Open pay <ArrowUpRight className="size-4" /></Link>
          </Button>
        </div>
      </header>
      <div className="mx-auto max-w-6xl px-5 lg:grid lg:grid-cols-[200px_minmax(0,1fr)] lg:gap-12 lg:px-8">
        <aside className="hidden lg:block">
          <div className="sticky top-24 py-8">
            <p className="mb-3 px-3 text-sm font-semibold">Developer docs</p>
            <DocumentationNavigation />
          </div>
        </aside>
        <main className="min-w-0 max-w-[720px] py-10 lg:py-12">
          <div className="mb-9 space-y-4">
            <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">Accept crypto payments</h1>
            <p className="text-lg leading-relaxed text-muted-foreground">Create an invoice, send its link, and fulfil the order once the payment is final.</p>
            <div className="rounded-xl border p-2 lg:hidden"><DocumentationNavigation /></div>
          </div>
          <div className="documentation-prose space-y-12">
            <DocumentationSection id="quickstart" title="Your first invoice">
              <p>Sign up with a passkey, set your payout address, then create an API token in <Link to="/settings">Settings</Link>. Approve with your passkey. The token is shown once; save it on your server as <code>PAY_API_TOKEN</code>. The examples paste it into a constant to stay short: keep the real one in your secret store or environment, never in git.{deploymentIsSandbox && <> To try it without real money first, use the <a href="#sandbox">sandbox</a>.</>}</p>
              <DocumentationRequestExampleBlock title="1. Create a $5 invoice" example={createInvoiceExample} exampleResponse={createInvoiceResponse} codeLanguage={codeLanguage} onCodeLanguageChange={setCodeLanguage} />
              <p>Send the customer the returned <code>url</code>; they pick the network and coin there, from the coins ticked in your <Link to="/settings">Settings</Link>. Save <code>id</code> as <code>PAY_INVOICE_ID</code> to read its status.</p>
              <DocumentationRequestExampleBlock title="2. Read the invoice" example={readInvoiceExample} exampleResponse={readInvoiceResponse} codeLanguage={codeLanguage} onCodeLanguageChange={setCodeLanguage} />
              <p>Fulfil on <code>paid</code> or <code>overpaid</code>, checking <a href="#status"><code>partial</code></a> if you require full payment. Use <a href="#webhooks">webhooks</a> to get updates.</p>
            </DocumentationSection>

            <DocumentationSection id="invoices" title="Invoices">
              <p>Base URL: <code>{apiBaseUrl}</code>. Send <code>Authorization: Bearer …</code> from your server only. API tokens cover these routes, the <a href="#export">CSV export</a>, <a href="#quantity-links">quantity links</a>, <a href="#static-deposit-addresses">customer addresses</a>, <a href="#scheduled-invoices">scheduled invoices</a> and <a href="#webhooks">events</a>. A token can’t touch your payout address, webhook or tokens: those live in <Link to="/settings">Settings</Link>. Changing the payout address, setting the webhook and making a token ask for your passkey; removing the webhook or a token needs only a signed-in session.</p>
              <dl className="documentation-reference">
                <div><dt><code>POST /invoices</code></dt><dd>Create an invoice. Returns HTTP 201 with the account view.</dd></div>
                <div><dt><code>GET /invoices/&#123;id&#125;</code></dt><dd>Read your invoice. Another account’s invoice is 404.</dd></div>
                <div><dt><code>GET /invoices</code></dt><dd>Filter by <code>status</code> and either <code>order_id</code> (0 or 1 invoice) or <code>order_id_prefix</code> (every order ID starting with it, matched literally; both at once is 400). Newest first, 50 per page. Returns <code>invoices</code> and <code>next_cursor</code>; pass it as <code>cursor</code> until null. Each invoice has <code>paid_at</code>, null until paid.</dd></div>
                <div><dt><code>POST /invoices/&#123;id&#125;/cancel</code></dt><dd>Cancel an invoice with nothing received; otherwise 409.</dd></div>
                <div><dt><code>POST /invoices/&#123;id&#125;/accept-partial</code></dt><dd>Accept an <code>underpaid</code> invoice as paid (<code>partial: true</code>). The money goes to the invoice’s own destination.</dd></div>
              </dl>
              <DocumentationRequestExampleBlock title="Cancel an unpaid invoice" example={cancelInvoiceExample} exampleResponse={cancelInvoiceResponse} codeLanguage={codeLanguage} onCodeLanguageChange={setCodeLanguage} />
              <p>pay makes each invoice’s <code>id</code>. Optionally send your own <code>order_id</code> too (e.g. <code>"order_id": "order-1042"</code>), one per purchase, with no spaces at either end and no control characters (either is 400), so a retry is safe: reusing it returns the same invoice unchanged, even if expired. Use a new ID for a new checkout.</p>
              <DocumentationRequestExampleBlock title="Find an order after a lost response" example={findOrderExample} exampleResponse={findOrderResponse} codeLanguage={codeLanguage} onCodeLanguageChange={setCodeLanguage} />
              <p>To give an expired order a fresh payment window, send the same <code>order_id</code> with <code>"refresh_expired": true</code>. If its invoice is <code>expired</code> with nothing received, you get a new invoice: new <code>id</code>, <code>url</code> and payment address, made from this request’s fields, with <code>refreshed_from_invoice_id</code>. The old one gets <code>refreshed_into_invoice_id</code>; <code>?order_id=</code> now finds the new one. Money sent late to the old address still counts as a <code>late_payment</code> there, never on the new one. Money already on the expired invoice, or already mined to its address, gets 409 <code>invoice_not_refreshable</code>. A payment still waiting to be mined can’t be seen yet: if it lands after the refresh, it shows up as a <code>late_payment</code> on the old invoice, which means that payer paid twice; settle the second payment with them. Any other invoice comes back unchanged.</p>
              <DocumentationRequestExampleBlock title="Refresh an expired order" example={refreshExpiredOrderExample} exampleResponse={refreshExpiredOrderResponse} codeLanguage={codeLanguage} onCodeLanguageChange={setCodeLanguage} />
              <p>Prefix order IDs with your customer, e.g. <code>customer-77:order-1042</code>, to list one customer’s invoices:</p>
              <DocumentationRequestExampleBlock title="List one customer’s paid invoices" example={listCustomerInvoicesExample} exampleResponse={listCustomerInvoicesResponse} codeLanguage={codeLanguage} onCodeLanguageChange={setCodeLanguage} />
              <details>
                <summary>Creation fields</summary>
                <dl className="documentation-reference mt-4">
                  <div><dt><code>amount</code>, <code>currency</code></dt><dd>Amount: decimal string, e.g. <code>"5"</code> or <code>"4.99"</code>, so no JSON float rounds it. USD and EUR: at most two decimals, minimum 1 in that currency. Coin amounts: exactly one network and one coin matching <code>currency</code>.</dd></div>
                  <div><dt><code>destination_address</code></dt><dd>Defaults to your saved payout address; fixed at creation. Contracts require <code>allow_contract_destination: true</code>.</dd></div>
                  <div><dt><code>note</code>, <code>metadata</code></dt><dd>Note: public, up to 500 characters; spaces at either end are trimmed and control characters other than line breaks and tabs are refused. Metadata: JSON object, included only in account reads and webhooks; at most 50 keys of up to 40 characters, strings up to 500 characters, 8 KB in all.</dd></div>
                  <div><dt><code>return_url</code></dt><dd>Back to your shop: an http(s) page. The payer page’s back button before payment, and its link after payment unless you set <code>success_url</code>.</dd></div>
                  <div><dt><code>success_url</code></dt><dd>An http(s) page linked once the invoice is paid.</dd></div>
                  <div><dt><code>callback_url</code></dt><dd>An HTTPS endpoint for this invoice’s events, in addition to your account <a href="#webhooks">webhook</a>.</dd></div>
                  <div><dt><code>refresh_expired</code></dt><dd>With <code>order_id</code>: replace that order’s expired, unpaid invoice with a new one.</dd></div>
                  <div><dt><code>lifetime_seconds</code></dt><dd>Range 300–43200, for every network the invoice offers. Left out, each network takes your account’s setting for it (Settings, Coins and networks), else 1800. Read <code>expires_at</code> for the deadline.</dd></div>
                  <div><dt><code>accuracy_percent</code></dt><dd>Shortfall tolerance, range 0–5%. Left out, each network takes your account’s setting for it, else 1%. Set 0 to require the full amount. The invoice’s own <code>accuracy_percent</code> is the one in force.</dd></div>
                  <div><dt>Several networks</dt><dd>Until the payer picks a network, <code>expires_at</code> and <code>accuracy_percent</code> are provisional (the longest window offered, the first network’s shortfall); the pick sets the picked network’s and restarts the window.</dd></div>
                  <div><dt><code>underpaid_open_until</code></dt><dd>UTC deadline for top-ups, within 5 years. Omit to leave underpaid invoices open.</dd></div>
                </dl>
              </details>
              <DocumentationRequestExampleBlock title="Shop links and a per-invoice callback" example={shopLinksAndCallbackExample} exampleResponse={shopLinksAndCallbackResponse} codeLanguage={codeLanguage} onCodeLanguageChange={setCodeLanguage} />
              <p>Errors contain <code>code</code> and <code>message</code>: 400 invalid input, 401 invalid/revoked token, 409 state conflict. On 429, respect <code>Retry-After</code> if present. API creation caps open invoices per account; public writes also have rate limits.</p>
            </DocumentationSection>

            <DocumentationSection id="networks" title="Networks & coins">
              <p>{deploymentIsSandbox ? "Polygon Amoy" : "Polygon PoS"} (<code>{polygonChainKey}</code>) and {deploymentIsSandbox ? "Arbitrum Sepolia" : "Arbitrum One"} (<code>{arbitrumChainKey}</code>) are live; Monero is off. Build your picker from public <code>GET /chains</code>: coins, token addresses, decimals, <code>chain_id</code> and <code>expected_finality_seconds</code>.</p>
              <DocumentationRequestExampleBlock title="List networks and coins" example={discoverChainsExample} exampleResponse={discoverChainsResponse} codeLanguage={codeLanguage} onCodeLanguageChange={setCodeLanguage} />
              <p>With no network field an invoice offers every live network. With no coin field a USD or EUR invoice offers your saved coins on each network, and a coin amount (e.g. <code>"currency": "POL"</code>) the network that has that coin. Use <code>chain</code> for one network or <code>chains</code> for several; <code>assets</code> is filtered to each. For exact coins per network, replace both with <code>assets_by_chain</code>:</p>
              <DocumentationCodeBlock title="Creation fields for two networks" code={offeredNetworksExample} />
              <p>Invoices offering several pairs start in <code>awaiting_asset</code>. <code>payment_options</code> lists estimates, without payment addresses; top-level <code>chain</code> is the first network until selection.</p>
              <DocumentationRequestExampleBlock title="Select a pair on an awaiting_asset invoice" example={choosePaymentPairExample} exampleResponse={choosePaymentPairResponse} codeLanguage={codeLanguage} onCodeLanguageChange={setCodeLanguage} />
              <p>This public call locks the pair, starts the payment window and returns a quote. Another pick gets 409. Wait for selection even if <code>deposit_address</code> is present. Send the exact <code>amount_due</code> on the selected network, to <code>deposit_address</code>, using the returned token address.</p>
              <p>pay uses Chainlink prices, quoting USD stablecoins at $1 within 0.5% of the peg; otherwise at the feed price, capped at $1. Stale feeds or an <a href="https://docs.chain.link/data-feeds/l2-sequencer-feeds">Arbitrum sequencer outage or recovery grace period</a> can block quotes. Estimates with <code>amount: null</code> aren’t payable.</p>
              <p>Keep amounts as decimal strings, never floats. For an open underpaid fiat invoice with an expired quote, <code>POST /public/invoices/&#123;id&#125;/quote</code> without a body refreshes the price. Send <code>amount_remaining</code> for a top-up; the pair stays fixed.</p>
            </DocumentationSection>

            <DocumentationSection id="status" title="Payment status">
              <dl className="documentation-reference">
                <div><dt><code>awaiting_asset</code></dt><dd>Waiting for the network/coin pick.</dd></div>
                <div><dt><code>awaiting_payment</code></dt><dd>The pair and amount are ready; nothing counted yet.</dd></div>
                <div><dt><code>confirming</code></dt><dd>Payment detected; wait for finality.</dd></div>
                <div><dt><code>underpaid</code></dt><dd>Final payment is short. Top-ups remain possible while open.</dd></div>
                <div><dt><code>paid</code>, <code>overpaid</code></dt><dd>Accepted and final. <code>partial: true</code> means the seller accepted a short payment.</dd></div>
                <div><dt><code>expired</code>, <code>cancelled</code></dt><dd>Closed. A <code>late_payment</code> doesn’t make it paid.</dd></div>
              </dl>
              <p>Read status on your server before fulfilment; a redirect or transaction hash isn’t proof. pay waits for RPC <code>finalized</code>: seconds on <a href="https://docs.polygon.technology/pos/concepts/finality/finality">Polygon</a>; batch inclusion on Ethereum and Ethereum finality on <a href="https://docs.arbitrum.io/how-arbitrum-works/reference/finality-and-reorgs">Arbitrum</a>. API timings are estimates.</p>
              <p>Payout follows acceptance: <code>sweeps</code> or <code>invoice.swept</code> tracks delivery; <code>sweep_blocked</code> or <code>invoice.sweep_failed</code> needs attention.</p>
              <details>
                <summary>Excess, wrong coins and refunds</summary>
                <p className="mt-4">Float refunds apply only to the operator’s own invoices paying its address. Other receivers get swept excess and wrong coins and settle with the payer. Wrong coins appear in <code>stray_payments</code>; they never count as payment.</p>
                <p>Final excess and wrong coins worth at least the refund minimum return automatically to the original sender, capped at what they sent. Contract or unknown senders need the operator; EIP-7702 wallets count as plain wallets. Refunds can wait for float or prices. Payers request underpaid refunds with their signature after the payment window ends. See <code>refunds</code>, <code>refund_offer</code> and stray <code>refund_handling</code>.</p>
              </details>
            </DocumentationSection>

            <DocumentationSection id="webhooks" title="Webhooks">
              <p>Set a public HTTPS endpoint in <Link to="/settings">Settings</Link> and approve with your passkey. Save the <code>whsec_…</code> secret as <code>PAY_WEBHOOK_SECRET</code>, like the token out of git; it’s shown once. Settings has tests, delivery history and resends.</p>
              <p>Each POST contains <code>type</code>, <code>timestamp</code> and <code>data</code>: the account invoice at the event, including <code>order_id</code> and <code>metadata</code>. Types include <code>invoice.payment_detected</code>, <code>invoice.underpaid</code>, <code>invoice.paid</code>, <code>invoice.overpaid</code>, <code>invoice.swept</code> and <code>invoice.refunded</code>. <code>webhook.test</code> carries a test message instead. Every type’s body:</p>
              <DocumentationWebhookEventExamples />
              <p>Every delivery is signed the <a href="https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md">Standard Webhooks</a> way: an HMAC-SHA256 over <code>webhook-id</code>, <code>webhook-timestamp</code> and the body, keyed with your webhook secret, in the <code>webhook-signature</code> header. Check it on the unchanged body bytes with Python’s own <code>hmac</code>, no package needed, and refuse anything that fails, is malformed or is more than 5 minutes off. Any Standard Webhooks library checks it the same way.</p>
              <DocumentationCodeBlock title="Python verification helper" code={verifyWebhookExample} />
              <p>To try your handler, pick a type next to <strong>Send test</strong> in <Link to="/settings">Settings</Link>. An invoice type sends a made-up invoice in that state, signed with your secret like a real event, and marked three ways: <code>"test": true</code> at the top of the body (real events have no <code>test</code> key), a <code>webhook-id</code> starting with <code>test_</code>, and <code>"test": true</code> in its <code>metadata</code>. Its <code>id</code> reads 404, so a handler that rereads the invoice won’t fulfil it. Refund events have no sample: only invoices paid to pay’s own address get refunds, so your webhook never receives them.</p>
              <p>Atomically save <code>webhook-id</code> with queued work, then return 2xx promptly. Delivery is at least once; retries and resends keep the ID and body. A real event’s <code>webhook-id</code> is its event id (<code>evt_</code> plus 22 characters), the same <code>id</code> <code>GET /events</code> lists; a delivery queued before event ids took this form keeps its numeric <code>webhook-id</code> on retries and resends. Deduplicate that ID and fulfil each invoice once. Events can arrive out of order; reread the invoice with your API token before fulfilling its order.</p>
              <p>A <code>callback_url</code> on an invoice gets that invoice’s events in addition to your account webhook. They use the same body, <code>webhook-id</code>, secret and retries. It follows the webhook URL rules: public HTTPS, no user info or fragment. Without an account webhook, creating it returns 409 <code>webhook_not_set</code>, because the callback needs that webhook’s secret. A 410 from the callback stops that delivery only. Removing the webhook fails pending callbacks.</p>
              <p>Missed something? Read your events with your token, newest first. A token sees exactly the events your webhook gets.</p>
              <DocumentationRequestExampleBlock title="List invoice events" example={listInvoiceEventsExample} exampleResponse={listInvoiceEventsResponse} codeLanguage={codeLanguage} onCodeLanguageChange={setCodeLanguage} />
              <details>
                <summary>Retries and secret rotation</summary>
                <p className="mt-4">Any 2xx succeeds. Failures retry after 5s, 5m, 30m, 2h, 5h, 10h, 14h, 20h and 24h, plus up to 10% jitter: ten attempts total. No redirects. <code>Retry-After</code> sets a minimum wait, capped at one hour.</p>
                <p>410 Gone disables the endpoint and fails pending deliveries; set its URL again to resume. Secret rotation takes effect immediately: update your verifier, then resend missed deliveries in Settings.</p>
              </details>
            </DocumentationSection>

            <DocumentationSection id="export" title="CSV export">
              <p><code>GET /invoices/export.csv</code> streams your invoices for accounting, by API token or from <strong>Invoice export</strong> in <Link to="/settings">Settings</Link>. Oldest first. Each invoice has an <code>invoice</code> row, then one row per <code>payment</code>, <code>stray_payment</code> (a coin it didn’t ask for), <code>sweep</code> (the payout to you) and <code>refund</code>, each repeating the invoice’s columns, so a filtered sheet still reads on its own.</p>
              <DocumentationRequestExampleBlock title="Export October’s invoices" example={exportInvoicesExample} exampleResponse={exportInvoicesResponse} codeLanguage={codeLanguage} onCodeLanguageChange={setCodeLanguage} />
              <p>Optional <code>created_from</code> (inclusive), <code>created_before</code> (exclusive) and <code>status</code>; times without a zone are UTC. Amounts are exact decimal strings in <code>row_asset</code>, times ISO 8601 UTC. A payment’s <code>asset_price_usd</code> is the price of the quote live at its block’s time, blank when none was. A note or order ID that starts (after any spaces) with <code>=</code>, <code>+</code>, <code>-</code>, <code>@</code>, a tab, a line break or a full-width <code>＝＋－＠</code> gets a leading <code>'</code>, and a <code>'</code> goes after any <code>;</code> (and the spaces after it) followed by <code>=</code>, <code>+</code>, <code>-</code>, <code>@</code> or their full-width forms, so a spreadsheet shows the text instead of running it. At most 6 exports a minute.</p>
              <details>
                <summary>Columns</summary>
                <dl className="documentation-reference mt-4">
                  <div><dt><code>row_kind</code>, <code>occurred_at</code></dt><dd><code>invoice</code> (its creation), <code>payment</code> and <code>stray_payment</code> (their block’s time), <code>sweep</code> and <code>refund</code> (when pay recorded them).</dd></div>
                  <div><dt>Invoice columns</dt><dd><code>invoice_id</code>, <code>invoice_created_at</code>, <code>order_id</code>, <code>invoice_status</code>, <code>partial</code>, <code>late_payment</code>, <code>note</code>, <code>fiat_currency</code>, <code>fiat_amount</code>, <code>chain</code>, <code>asset</code>, <code>amount_due</code>, <code>amount_received</code>, <code>destination_address</code>, <code>deposit_address</code>, <code>paid_at</code>, <code>payment_link_id</code>.</dd></div>
                  <div><dt>Row columns</dt><dd><code>row_status</code> (<code>final</code>, <code>confirming</code>, or a sweep’s or refund’s status), <code>row_asset</code>, <code>row_amount</code>, <code>asset_price_usd</code>, <code>transaction_hash</code>, <code>block_number</code>, <code>from_address</code>, <code>to_address</code>. A sweep still under way names no coin or amount: it moves whatever the deposit holds.</dd></div>
                </dl>
              </details>
            </DocumentationSection>

            <DocumentationSection id="rates" title="Exchange rates">
              <p>Public <code>GET /rates</code> lists each coin’s price per network in USD and EUR: the Chainlink price a new quote would use now, USD stablecoins at exactly 1 within 0.5% of the peg. A price can be up to 30 seconds old, and the response may be cached for 10 seconds more (<code>Cache-Control: public, max-age=10</code>);<code>usd</code> and <code>eur</code> are null while a network’s feeds are stale. It’s a guide: the invoice’s own quote is what the payer pays.</p>
              <DocumentationRequestExampleBlock title="Read the rates" example={listRatesExample} exampleResponse={listRatesResponse} codeLanguage={codeLanguage} onCodeLanguageChange={setCodeLanguage} />
            </DocumentationSection>

            <DocumentationSection id="quantity-links" title="Quantity links">
              <p>Make a link for up to N payers, from the pay page’s Options or with your API token:</p>
              <dl className="documentation-reference">
                <div><dt><code>POST /payment-links</code></dt><dd><code>amount</code>, <code>currency</code> (USD/EUR), network/coin fields and <code>quantity</code> (1–1000), optionally <code>lifetime_seconds</code> and <code>accuracy_percent</code> (left out, they read null and each payer’s invoice takes your setting for the network picked). Returns the link’s <code>url</code>.</dd></div>
                <div><dt><code>GET /payment-links</code></dt><dd>Your newest 100 links.</dd></div>
                <div><dt><code>GET /payment-links/&#123;id&#125;</code></dt><dd>Your link with <code>slots_left</code>, <code>slots_held</code>, <code>paid_count</code> and its newest invoices.</dd></div>
                <div><dt><code>POST /payment-links/&#123;id&#125;/close</code></dt><dd>Stops new payers. Existing invoices run their course.</dd></div>
              </dl>
              <DocumentationRequestExampleBlock title="Create a link for 10 payers" example={createPaymentLinkExample} exampleResponse={createPaymentLinkResponse} codeLanguage={codeLanguage} onCodeLanguageChange={setCodeLanguage} />
              <DocumentationRequestExampleBlock title="Close a link" example={closePaymentLinkExample} exampleResponse={closePaymentLinkResponse} codeLanguage={codeLanguage} onCodeLanguageChange={setCodeLanguage} />
              <p>Viewing <code>/l/…</code> holds no slot. <code>POST /public/payment-links/&#123;id&#125;/invoices</code> with <code>chain</code> and <code>asset</code> reserves one with a separate invoice and deposit. If full, it returns 409 <code>sold_out</code>.</p>
              <p>Invoices hold slots unless expired or cancelled without a late payment. Each sends invoice webhooks with <code>payment_link_id</code>.</p>
            </DocumentationSection>

            <DocumentationSection id="scheduled-invoices" title="Scheduled invoices">
              <p>A schedule makes a new invoice every day, week or month for a customer you bill again and again, from the pay page’s Options (<strong>Repeat</strong>) or with your API token. It only makes invoices: you send each one’s <code>url</code> to the customer and they pay it like any other. Nothing is ever taken from a wallet.</p>
              <dl className="documentation-reference">
                <div><dt><code>POST /invoice-schedules</code></dt><dd>The <code>POST /invoices</code> fields (except <code>order_id</code>, <code>refresh_expired</code> and <code>underpaid_open_until</code>) plus <code>interval</code> (<code>daily</code>, <code>weekly</code> or <code>monthly</code>), optional <code>starts_at</code> (default now) and at most one end: <code>ends_after_invoice_count</code> (1–10000) or <code>ends_at</code>; both times within 5 years. Returns HTTP 201 with the schedule.</dd></div>
                <div><dt><code>GET /invoice-schedules</code></dt><dd>Up to 100 of your schedules, active and paused ones first, then newest first.</dd></div>
                <div><dt><code>GET /invoice-schedules/&#123;id&#125;</code></dt><dd>Your schedule with <code>next_period_start</code> (the next period still to come), <code>created_invoice_count</code> and <code>last_failure_code</code>.</dd></div>
                <div><dt><code>GET /invoice-schedules/&#123;id&#125;/invoices</code></dt><dd>Its invoices, newest period first, at most 200.</dd></div>
                <div><dt><code>POST /invoice-schedules/&#123;id&#125;/pause</code>, <code>/resume</code>, <code>/cancel</code></dt><dd>Pause stops new invoices; resume starts again from the next period that hasn’t started yet, never billing one missed while paused. Cancel is for good. Invoices already made run their course. Pausing or resuming a cancelled or completed schedule is 409 <code>invoice_schedule_state_conflict</code>.</dd></div>
              </dl>
              <DocumentationRequestExampleBlock title="Bill a customer $5 a month for a year" example={createInvoiceScheduleExample} exampleResponse={createInvoiceScheduleResponse} codeLanguage={codeLanguage} onCodeLanguageChange={setCodeLanguage} />
              <p>Each period gets exactly one invoice, made within a minute of the period’s start, with <code>order_id</code> <code>&lt;order_id_prefix&gt;-&lt;YYYYMMDD&gt;</code> (the period’s start day in UTC; the schedule’s <code>id</code> stands in for a missing prefix), so <code>GET /invoices?order_id_prefix=customer-77-premium-</code> pages through them all. Periods are counted in UTC: a monthly schedule keeps the start’s UTC day of the month and time, or the month’s last day when it is shorter: one started on 31 January bills on 28 February, 31 March and 30 April. Its invoices wait 12 hours for payment unless you set <code>lifetime_seconds</code>; your account’s time to pay for a network does not apply to them, its shortfall does when you leave <code>accuracy_percent</code> out (a schedule made without them returns <code>lifetime_seconds</code> and <code>accuracy_percent</code> as null); an expired one can be refreshed with <code>refresh_expired</code> on <code>POST /invoices</code> like any order.</p>
              <p>Each new invoice sends <code>schedule.invoice_created</code> to your webhook, the invoice’s <code>callback_url</code> and <code>GET /events</code>, then the usual invoice events. Its <code>data</code> is the invoice with <code>invoice_schedule_id</code> and <code>invoice_schedule_period_start</code>: send its <code>url</code> to your customer from there.</p>
              <DocumentationRequestExampleBlock title="Read a schedule" example={readInvoiceScheduleExample} exampleResponse={readInvoiceScheduleResponse} codeLanguage={codeLanguage} onCodeLanguageChange={setCodeLanguage} />
              <DocumentationRequestExampleBlock title="List a schedule’s invoices" example={listInvoiceScheduleInvoicesExample} exampleResponse={listInvoiceScheduleInvoicesResponse} codeLanguage={codeLanguage} onCodeLanguageChange={setCodeLanguage} />
              <DocumentationRequestExampleBlock title="Pause a schedule" example={pauseInvoiceScheduleExample} exampleResponse={pauseInvoiceScheduleResponse} codeLanguage={codeLanguage} onCodeLanguageChange={setCodeLanguage} />
              <DocumentationRequestExampleBlock title="Resume it" example={resumeInvoiceScheduleExample} exampleResponse={resumeInvoiceScheduleResponse} codeLanguage={codeLanguage} onCodeLanguageChange={setCodeLanguage} />
              <DocumentationRequestExampleBlock title="Cancel it" example={cancelInvoiceScheduleExample} exampleResponse={cancelInvoiceScheduleResponse} codeLanguage={codeLanguage} onCodeLanguageChange={setCodeLanguage} />
              <p>The destination and coins are fixed when the schedule is made; to change them, cancel it and make a new one. If an invoice can’t be made (prices down, say), <code>last_failure_code</code> says why and pay retries, backing off up to an hour. A period that passed before its invoice could be made (pay down for days, say) is skipped, never billed late. Schedules work on EVM networks only, at most 100 active or paused per account (409 <code>too_many_invoice_schedules</code>), and two live schedules can’t share an <code>order_id_prefix</code> (409 <code>order_id_prefix_taken</code>), nor can a new one whose first invoice’s order id one of your invoices already has (a cancelled schedule’s, say).</p>
            </DocumentationSection>

            {deploymentIsSandbox && <DocumentationSection id="sandbox" title="Sandbox">
              <p>The sandbox at <a href={sandboxApiOrigin}><code>{sandboxApiOrigin.replace("https://", "")}</code></a> is the same app and API on testnets, so you can build and test an integration with free test coins. Base URL: <code>{sandboxApiOrigin}/api/v1</code>. Its networks are Polygon Amoy (<code>polygon_amoy</code>: POL and USDC) and Arbitrum Sepolia (<code>arbitrum_sepolia</code>: ETH and USDC); <code>GET /chains</code> there lists them.</p>
              <p>It is a separate deployment: accounts, passkeys, API tokens, webhook secrets and invoices made there exist only there, and a production token answers 401 there (and the other way round). Sign up on the sandbox, set a payout address you hold the key to and make a sandbox token. Prices are the same live Chainlink mainnet prices production quotes with, so a $5 invoice asks for 5 test USDC or $5 worth of test POL or ETH. Test coins have no value and the sandbox keeps no backups.</p>
              <p>Get test coins from the official faucets:</p>
              <dl className="documentation-reference">
                <div><dt>Test USDC (both networks)</dt><dd><a href="https://faucet.circle.com/">Circle’s faucet</a>: 20 USDC per request, one request every 2 hours per address and network. Circle lists the token addresses on <a href="https://developers.circle.com/stablecoins/usdc-contract-addresses">its USDC contract page</a>.</dd></div>
                <div><dt>Amoy POL</dt><dd>The <a href="https://faucet.polygon.technology/">Polygon faucet</a>; pick Amoy and POL.</dd></div>
                <div><dt>Arbitrum Sepolia ETH</dt><dd>The faucets Arbitrum lists on its <a href="https://docs.arbitrum.io/for-devs/dev-tools-and-resources/chain-info">chain info page</a>.</dd></div>
              </dl>
              <p>Payments count on the testnet’s <code>finalized</code> block as on mainnet: seconds on Amoy, about 20 minutes on Arbitrum Sepolia.</p>
            </DocumentationSection>}

            <DocumentationSection id="static-deposit-addresses" title="Customer addresses">
              <p>A permanent deposit address per customer, for top-ups or a balance: any amount, any time, in the coins it takes, and it never expires. Every final payment goes to the payout address it was made with and is counted at its USD value when it arrived.</p>
              <dl className="documentation-reference">
                <div><dt><code>POST /static-deposit-addresses</code></dt><dd><code>customer_reference</code> (1–128 of <code>A–Z a–z 0–9 _ . : @ + -</code>) and <code>chain</code>. Optional <code>assets</code> (defaults to your saved coins) and <code>allow_contract_destination</code>. Returns 201 with the address; the same reference on the same chain returns the same address unchanged, so a retry is safe. 409 <code>static_deposit_address_limit_reached</code> past the account’s limit.</dd></div>
                <div><dt><code>GET /static-deposit-addresses/&#123;id&#125;</code></dt><dd>The address with its newest 100 payments.</dd></div>
                <div><dt><code>GET /static-deposit-addresses</code></dt><dd>Filter by <code>customer_reference</code>, <code>customer_reference_prefix</code> (matched literally) or <code>chain</code>. Newest first, 50 per page, <code>next_cursor</code> as for invoices.</dd></div>
              </dl>
              <DocumentationRequestExampleBlock title="Get a customer’s address" example={createStaticDepositAddressExample} exampleResponse={createStaticDepositAddressResponse} codeLanguage={codeLanguage} onCodeLanguageChange={setCodeLanguage} />
              <p>Show the customer <code>deposit_address</code> and the coins in <code>assets</code>. Save <code>id</code> as <code>PAY_STATIC_DEPOSIT_ADDRESS_ID</code>.</p>
              <DocumentationRequestExampleBlock title="Read its payments" example={readStaticDepositAddressExample} exampleResponse={readStaticDepositAddressResponse} codeLanguage={codeLanguage} onCodeLanguageChange={setCodeLanguage} />
              <DocumentationRequestExampleBlock title="List your customers’ addresses" example={listStaticDepositAddressesExample} exampleResponse={listStaticDepositAddressesResponse} codeLanguage={codeLanguage} onCodeLanguageChange={setCodeLanguage} />
              <p>Credit your customer with <code>amount_usd</code> of each payment that has <code>credited: true</code>; <code>received_usd</code> is their sum. Payments are found within about a minute of finality on an address made or paid in the last day, within about 20 minutes on an older one; one payment can show up as several rows, or one row for several transfers, so credit by payment <code>id</code>, not by <code>tx_hash</code>.</p>
              <p>Webhooks: <code>static_address.payment_received</code> for a counted payment, <code>static_address.stray_payment</code> for a coin the address doesn’t take (<code>uncredited_reason: "wrong_coin"</code>, swept to you anyway; settle it with the payer, there are no refunds here), <code>static_address.swept</code> once the money reached your payout address, and <code>static_address.sweep_failed</code> when the operator must move it by hand. Every body is in the <a href="#webhooks">webhook examples</a>.</p>
            </DocumentationSection>
          </div>
        </main>
      </div>
    </div>
  )
}
