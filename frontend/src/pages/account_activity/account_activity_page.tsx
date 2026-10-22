// `/activity` (PLAN.md sections 8.1 and 10): "Paid today" and the stream's state on one line, "Needs you"
// (needs_you_section.tsx), the account's invoices by day (`GET /api/v1/invoices`, reloaded after each new event, older
// pages on "Load older"), its quantity links and invoice schedules ("Links", payment_link_list.tsx and
// invoice_schedule_list.tsx), its customers' static deposit addresses
// ("Customers", static_deposit_address_list.tsx, PLAN.md section 17), the event history ("Updates",
// `GET /api/v1/events`, account_history_paging.ts) and its live events over SSE. After `caught_up`, each new event is a
// toast and, once allowed on the bell, a browser notification while the tab is open.
import { BellRing } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { useCallback, useEffect, useRef, useState } from "react"
import { useLocation } from "react-router"
import { toast } from "sonner"

import { PageColumn } from "@/components/page_column"
import { SignInGate } from "@/components/sign_in_gate"
import { SiteTopBar } from "@/components/site_top_bar"
import { Button } from "@/components/ui/button"
import { Empty, EmptyHeader, EmptyTitle } from "@/components/ui/empty"
import { Item, ItemActions, ItemContent, ItemDescription, ItemGroup, ItemTitle } from "@/components/ui/item"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { useAccountSession } from "@/hooks/use_account_session"
import { formatFiatAmount } from "@/lib/amount_formatting"
import { describeError, fetchApiJson } from "@/lib/api_client"
import type { AccountEvent, AccountInvoice, AccountInvoicePage, AccountPaymentLink } from "@/lib/api_types"
import { followAccountEventStream, type EventStreamConnectionState } from "@/lib/account_event_stream"
import { popSpring } from "@/lib/motion_presets"
import { formatClockTime, formatDayHeading, formatShortTime } from "@/lib/time_formatting"
import { cn } from "@/lib/utils"
import { InvoiceActionsOverlay } from "@/pages/account_activity/invoice_actions_overlay"
import {
  accountEventTitles,
  accountEventTones,
  describeAccountEventLine,
  describeInvoiceAmount,
  describeInvoiceLine,
  findAccountEventHref,
  findAccountEventInvoiceId,
} from "@/pages/account_activity/invoice_descriptions"
import { InvoiceStatusBadge } from "@/pages/account_activity/invoice_status_badge"
import { mergeAccountEventsNewestFirst, mergeAccountInvoicesNewestFirst, useOlderAccountEvents } from "@/pages/account_activity/account_history_paging"
import { LoadOlderButton } from "@/pages/account_activity/load_older_button"
import { NeedsYouSection } from "@/pages/account_activity/needs_you_section"
import { AccountInvoiceScheduleList } from "@/pages/account_activity/invoice_schedule_list"
import { AccountPaymentLinkList } from "@/pages/account_activity/payment_link_list"
import { useAccountInvoiceSchedules } from "@/pages/account_activity/use_account_invoice_schedules"
import { useAccountMoneroWallets } from "@/pages/account_activity/use_account_monero_wallets"
import { AccountStaticDepositAddressList } from "@/pages/account_activity/static_deposit_address_list"
import { useAccountPaymentLinks } from "@/pages/account_activity/use_account_payment_links"
import { useAccountStaticDepositAddresses } from "@/pages/account_activity/use_account_static_deposit_addresses"

const invoiceReloadDelayMilliseconds = 1000
const paidStatuses = new Set(["paid", "overpaid"])
const paidEventTypes = new Set(["invoice.paid", "invoice.overpaid"])
// An invoice the payer has not picked a coin for names its accepted coins up to this many, else how many.
const acceptedCoinsNamedAtMost = 3

// For screen readers only; on screen the stream's state is the dot (and "Reconnecting…" while it is).
const connectionLabels: Record<EventStreamConnectionState, string> = {
  connecting: "Connecting…",
  live: "Live",
  reconnecting: "Reconnecting…",
  signed_out: "Signed out",
  refused: "Not live in this tab",
}

// The invoices in the server's order (newest first), cut into runs of one local day each.
function groupInvoicesByDay(invoices: AccountInvoice[]): { dayKey: string; dayHeading: string; invoices: AccountInvoice[] }[] {
  const dayGroups: { dayKey: string; dayHeading: string; invoices: AccountInvoice[] }[] = []
  for (const invoice of invoices) {
    const dayKey = new Date(invoice.created_at).toDateString()
    const lastDayGroup = dayGroups.at(-1)
    if (lastDayGroup?.dayKey === dayKey) lastDayGroup.invoices.push(invoice)
    else dayGroups.push({ dayKey, dayHeading: formatDayHeading(invoice.created_at), invoices: [invoice] })
  }
  return dayGroups
}

// Under the note: the coin (or the coins on offer while the payer hasn't picked) and the time; for an invoice a payer
// made from a quantity link, that link's sales instead of the coin.
function describeInvoiceRowLine(invoice: AccountInvoice, paymentLinkById: Map<string, AccountPaymentLink>): string {
  const paymentLink = invoice.payment_link_id === null ? undefined : paymentLinkById.get(invoice.payment_link_id)
  let coinText = invoice.asset?.symbol ?? ""
  if (paymentLink) coinText = `Link · ${paymentLink.paid_count} of ${paymentLink.quantity} sold`
  else if (!coinText) coinText = invoice.assets.length <= acceptedCoinsNamedAtMost ? invoice.assets.join(", ") : `${invoice.assets.length} coins`
  return [coinText, formatClockTime(invoice.created_at)].filter(Boolean).join(" · ")
}

// When each invoice was paid: the Invoice resource has no paid time (PLAN.md section 7.1), so it is the time of its
// `invoice.paid` or `invoice.overpaid` event when the stream or a history page sent one.
function mapPaidTimeByInvoiceId(accountEvents: AccountEvent[]): Map<string, string> {
  const paidTimeByInvoiceId = new Map<string, string>()
  for (const accountEvent of accountEvents) {
    if (accountEvent.invoice_id === null || !paidEventTypes.has(accountEvent.type)) continue
    const knownPaidTime = paidTimeByInvoiceId.get(accountEvent.invoice_id)
    if (!knownPaidTime || accountEvent.created_at < knownPaidTime) paidTimeByInvoiceId.set(accountEvent.invoice_id, accountEvent.created_at)
  }
  return paidTimeByInvoiceId
}

// Sums what was paid today by the time it was paid, not when the invoice was made. An invoice paid before the
// streamed events reach back falls back to `updated_at`: its last change, the sweep right after the payment as a rule.
function sumPaidTodayUsd(invoices: AccountInvoice[], paidTimeByInvoiceId: Map<string, string>): string {
  const todayText = new Date().toDateString()
  const totalCents = invoices
    .filter((invoice) => {
      if (!paidStatuses.has(invoice.status) || !invoice.fiat.usd) return false
      const paidTime = paidTimeByInvoiceId.get(invoice.id) ?? invoice.updated_at
      return new Date(paidTime).toDateString() === todayText
    })
    .reduce((sumCents, invoice) => sumCents + Math.round(Number(invoice.fiat.usd) * 100), 0)
  return formatFiatAmount((totalCents / 100).toFixed(2), "USD")
}

// Whether this page can pop a notification itself. The constructor exists on Android Chrome and Samsung Internet but
// always throws a TypeError there, and on iOS it works only in a home-screen web app (MDN browser-compat-data,
// api.Notification.Notification, https://developer.mozilla.org/en-US/docs/Web/API/Notification/Notification#browser_compatibility),
// so the bell would ask for a permission that never pops anything. Probed only before permission is granted: then the
// constructor checks permission in parallel and just fires `error`, showing nothing (WHATWG Notifications, "new
// Notification(title, options)" steps 1 and 5, https://notifications.spec.whatwg.org/#dom-notification-notification).
function readNotificationPermission(): NotificationPermission | "unsupported" {
  if (!("Notification" in window)) return "unsupported"
  if (Notification.permission === "default") {
    try {
      new Notification("").close()
    } catch {
      return "unsupported"
    }
  }
  return Notification.permission
}

function showBrowserNotification(accountEvent: AccountEvent, openAccountEvent: (accountEvent: AccountEvent) => void) {
  if (!("Notification" in window) || Notification.permission !== "granted") return
  const title = accountEventTitles[accountEvent.type] ?? accountEvent.type
  let popup: Notification
  try {
    popup =
      accountEvent.invoice_id === null
        ? new Notification(title, { body: describeAccountEventLine(accountEvent), tag: `account_event_${accountEvent.id}` })
        : new Notification(`${title}: ${describeInvoiceAmount(accountEvent.data)}`, {
            body: describeInvoiceLine(accountEvent.data) || accountEvent.invoice_id,
            tag: `account_event_${accountEvent.id}`,
          })
  } catch {
    // Most mobile browsers throw a TypeError here (only a service worker may notify there:
    // https://developer.mozilla.org/en-US/docs/Web/API/Notification/Notification); the toast still shows.
    return
  }
  popup.addEventListener("click", () => {
    window.focus()
    openAccountEvent(accountEvent)
    popup.close()
  })
}

function showEventToast(accountEvent: AccountEvent, openAccountEvent: (accountEvent: AccountEvent) => void) {
  const title = accountEventTitles[accountEvent.type] ?? accountEvent.type
  const tone = accountEventTones[accountEvent.type] ?? "info"
  const toastOptions = {
    description: describeAccountEventLine(accountEvent),
    // "View", not "Open": "Open" is also an invoice status a list row could show beside the toast.
    action: { label: "View", onClick: () => openAccountEvent(accountEvent) },
  }
  if (tone === "success") toast.success(title, toastOptions)
  else if (tone === "warning") toast.warning(title, toastOptions)
  else if (tone === "danger") toast.error(title, toastOptions)
  else toast.info(title, toastOptions)
}

export function AccountActivityPage() {
  const { accountSession, sessionIsLoading } = useAccountSession()
  if (!accountSession) {
    return (
      <PageColumn topBar={<SiteTopBar />}>
        {sessionIsLoading ? (
          <Skeleton className="mt-12 h-10 w-56" />
        ) : (
          <SignInGate title="Your activity" />
        )}
      </PageColumn>
    )
  }
  // Keyed by account: another account's sign-in starts from an empty page and its own stream.
  return <SignedInAccountActivity key={accountSession.username} />
}

function SignedInAccountActivity() {
  const { refreshAccountSession } = useAccountSession()
  // `/activity#schedules` (where `/` goes after setting up a repeating invoice) opens the Links tab at its repeating
  // invoices.
  const opensAtInvoiceSchedules = useLocation().hash === "#schedules"
  const startingTabValue = opensAtInvoiceSchedules ? "payment_links" : "invoices"
  useEffect(() => {
    if (opensAtInvoiceSchedules) document.getElementById("invoice_schedules")?.scrollIntoView({ block: "start" })
  }, [opensAtInvoiceSchedules])
  const [invoices, setInvoices] = useState<AccountInvoice[] | null>(null)
  const [invoiceLoadError, setInvoiceLoadError] = useState("")
  const [olderInvoicesCursor, setOlderInvoicesCursor] = useState<string | null>(null)
  const [olderInvoicesAreLoading, setOlderInvoicesAreLoading] = useState(false)
  const [olderInvoicesLoadError, setOlderInvoicesLoadError] = useState("")
  // Once an older page is in, a reload of the first page merges into the list instead of replacing it, and the cursor
  // stays the one past the oldest invoice shown.
  const olderInvoicesWereLoadedRef = useRef(false)
  const { olderAccountEvents, hasOlderAccountEvents, olderAccountEventsAreLoading, olderAccountEventsLoadError, loadOlderAccountEvents } = useOlderAccountEvents()
  const [accountEvents, setAccountEvents] = useState<AccountEvent[]>([])
  const [eventsCaughtUp, setEventsCaughtUp] = useState(false)
  const [connectionState, setConnectionState] = useState<EventStreamConnectionState>("connecting")
  const [eventStreamRefusal, setEventStreamRefusal] = useState("")
  const [notificationPermission, setNotificationPermission] = useState(readNotificationPermission)
  const [openedInvoiceId, setOpenedInvoiceId] = useState<string | null>(null)
  // Counts the debounced reloads after live events; the quantity links reload with it.
  const [liveReloadCount, setLiveReloadCount] = useState(0)
  const invoiceReloadTimer = useRef<number | undefined>(undefined)
  const loadedInvoicesRef = useRef<AccountInvoice[] | null>(null)
  useEffect(() => {
    loadedInvoicesRef.current = invoices
  }, [invoices])

  // A toast's "View", a notification and an Updates row all open the same thing: the invoice's overlay here
  // when it is in the list, else (older than the list reaches) the invoice page in a new tab.
  // An invoice event opens that invoice's overlay (or a new tab when it is older than the loaded list); an account
  // event without an invoice (the payout address changed) opens its page; an operator alert opens the invoice it names.
  const openAccountEvent = useCallback((accountEvent: AccountEvent) => {
    const invoiceId = findAccountEventInvoiceId(accountEvent)
    if (invoiceId === null) {
      const eventHref = findAccountEventHref(accountEvent)
      if (eventHref !== window.location.pathname) window.location.assign(eventHref)
    } else if (loadedInvoicesRef.current?.some((invoice) => invoice.id === invoiceId)) setOpenedInvoiceId(invoiceId)
    else window.open(findAccountEventHref(accountEvent), "_blank", "noopener")
  }, [])

  // The state changes in the promise callbacks, once the API answers, never synchronously in the effect below.
  const loadAccountInvoices = useCallback(
    () =>
      fetchApiJson<AccountInvoicePage>("/api/v1/invoices").then(
        (invoicePage) => {
          const firstPageInvoices = invoicePage.invoices ?? []
          if (olderInvoicesWereLoadedRef.current) {
            setInvoices((currentInvoices) => mergeAccountInvoicesNewestFirst(currentInvoices ?? [], firstPageInvoices))
          } else {
            setInvoices(firstPageInvoices)
            setOlderInvoicesCursor(invoicePage.next_cursor)
          }
          setInvoiceLoadError("")
        },
        (loadError: unknown) => setInvoiceLoadError(describeError(loadError)),
      ),
    [],
  )

  useEffect(() => {
    void loadAccountInvoices()
    const abortController = new AbortController()
    const shownEventIds = new Set<string>()
    void followAccountEventStream(abortController.signal, {
      onAccountEvent(accountEvent, isCaughtUp) {
        if (shownEventIds.has(accountEvent.id)) return
        shownEventIds.add(accountEvent.id)
        // No cap: rows cut from here would fall between the history's first page and the live list, where nothing reads.
        setAccountEvents((currentEvents) => [accountEvent, ...currentEvents])
        // An invoice event carries the invoice as it is now, so one on an older page (which the reload below never
        // reads) is brought up to date too.
        if (accountEvent.invoice_id !== null) {
          const eventInvoice = accountEvent.data
          setInvoices(
            (currentInvoices) =>
              currentInvoices?.map((invoice) =>
                invoice.id === eventInvoice.id && eventInvoice.updated_at >= invoice.updated_at ? eventInvoice : invoice,
              ) ?? null,
          )
        }
        if (!isCaughtUp) return
        showEventToast(accountEvent, openAccountEvent)
        showBrowserNotification(accountEvent, openAccountEvent)
        window.clearTimeout(invoiceReloadTimer.current)
        invoiceReloadTimer.current = window.setTimeout(() => {
          void loadAccountInvoices()
          setLiveReloadCount((currentCount) => currentCount + 1)
        }, invoiceReloadDelayMilliseconds)
      },
      onCaughtUp() {
        setEventsCaughtUp(true)
      },
      onStreamRefused(refusal) {
        setEventStreamRefusal(describeError(refusal))
      },
      onConnectionStateChange(nextConnectionState) {
        setConnectionState(nextConnectionState)
        if (nextConnectionState === "signed_out") void refreshAccountSession()
      },
    })
    return () => {
      abortController.abort()
      window.clearTimeout(invoiceReloadTimer.current)
    }
  }, [loadAccountInvoices, refreshAccountSession, openAccountEvent])

  async function loadOlderInvoices() {
    if (olderInvoicesCursor === null || olderInvoicesAreLoading) return
    setOlderInvoicesAreLoading(true)
    // Before the await: a first-page reload answering meanwhile must merge, not replace the list and the cursor.
    olderInvoicesWereLoadedRef.current = true
    try {
      const invoicePage = await fetchApiJson<AccountInvoicePage>(`/api/v1/invoices?cursor=${encodeURIComponent(olderInvoicesCursor)}`)
      setInvoices((currentInvoices) => mergeAccountInvoicesNewestFirst(currentInvoices ?? [], invoicePage.invoices ?? []))
      setOlderInvoicesCursor(invoicePage.next_cursor)
      setOlderInvoicesLoadError("")
    } catch (loadError: unknown) {
      setOlderInvoicesLoadError(describeError(loadError))
    } finally {
      setOlderInvoicesAreLoading(false)
    }
  }

  // Only this open page pops them: there is no push to a closed page (2026-10-13).
  async function askForNotificationPermission() {
    await Notification.requestPermission()
    setNotificationPermission(Notification.permission)
  }

  const openedInvoice = invoices?.find((invoice) => invoice.id === openedInvoiceId) ?? null
  // Newest first: the server already lists invoices newest first; the streamed events (arriving oldest first) and the
  // older pages merge by id.
  const sortedEvents = mergeAccountEventsNewestFirst(accountEvents, olderAccountEvents)
  const paidTimeByInvoiceId = mapPaidTimeByInvoiceId(sortedEvents)
  const { paymentLinks, linkLoadError, replacePaymentLink } = useAccountPaymentLinks(liveReloadCount)
  const { invoiceSchedules, scheduleLoadError, replaceInvoiceSchedule } = useAccountInvoiceSchedules(liveReloadCount)
  const { moneroWalletsNeedingLookahead, confirmWidenedLookahead } = useAccountMoneroWallets(liveReloadCount)
  const accountStaticDepositAddresses = useAccountStaticDepositAddresses(liveReloadCount)
  const paymentLinkById = new Map((paymentLinks ?? []).map((paymentLink) => [paymentLink.id, paymentLink]))

  return (
    <PageColumn topBar={<SiteTopBar />}>
      <section className="mt-3 flex min-h-11 items-center justify-between gap-3">
        <p className="text-[15px] text-muted-foreground">
          Paid today{" "}
          <span data-testid="paid_today_total" className="font-semibold text-foreground tabular-nums">
            {invoices ? sumPaidTodayUsd(invoices, paidTimeByInvoiceId) : "…"}
          </span>
        </p>
        <div className="flex items-center gap-1">
          <span data-testid="event_stream_state" role="status" className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
            <span className="sr-only">{connectionLabels[connectionState]}</span>
            <AnimatePresence initial={false} mode="popLayout">
              {connectionState === "live" ? (
                <motion.span key="live" aria-hidden="true" className="inline-flex items-center gap-1.5" initial={{ opacity: 0, scale: 0.6 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0 }} transition={popSpring}>
                  <span className="size-2 rounded-full bg-success" />
                  live
                </motion.span>
              ) : connectionState === "reconnecting" ? (
                <motion.span key="reconnecting" aria-hidden="true" className="inline-flex items-center gap-1.5" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
                  <span className="size-2 animate-pulse rounded-full bg-warning" />
                  Reconnecting…
                </motion.span>
              ) : null}
            </AnimatePresence>
          </span>
          {notificationPermission === "default" ? (
            <Button
              id="enable_popups_button"
              type="button"
              variant="ghost"
              size="icon"
              aria-label="Notify me on this device when money arrives"
              title="Notify me on this device"
              className="size-11 rounded-full"
              onClick={() => void askForNotificationPermission()}
            >
              <BellRing className="size-5" />
            </Button>
          ) : null}
        </div>
      </section>
      {eventStreamRefusal ? (
        <p data-testid="event_stream_refusal" role="alert" className="text-sm text-muted-foreground">
          {eventStreamRefusal}
        </p>
      ) : null}

      {invoices ? (
        <NeedsYouSection
          invoices={invoices}
          onResolveInvoice={setOpenedInvoiceId}
          moneroWalletsNeedingLookahead={moneroWalletsNeedingLookahead}
          onConfirmMoneroLookahead={confirmWidenedLookahead}
        />
      ) : null}

      <Tabs defaultValue={startingTabValue} className="mt-4 flex-1">
        {/* 44 px tabs; "Updates", not "Live", which already names the stream state above. */}
        <TabsList className="grid w-full grid-cols-4 group-data-horizontal/tabs:h-[50px]">
          <TabsTrigger value="invoices" className="touch-target-hit-area h-11 text-[15px]">
            Invoices
          </TabsTrigger>
          <TabsTrigger id="payment_links_tab" value="payment_links" className="touch-target-hit-area h-11 text-[15px]">
            Links
          </TabsTrigger>
          <TabsTrigger id="static_deposit_addresses_tab" value="static_deposit_addresses" className="touch-target-hit-area h-11 text-[15px]">
            Customers
          </TabsTrigger>
          <TabsTrigger value="events" className="touch-target-hit-area h-11 text-[15px]">
            Updates
          </TabsTrigger>
        </TabsList>
        <TabsContent value="invoices" className="mt-1">
          {invoices === null && !invoiceLoadError ? (
            <div className="mt-2 flex flex-col gap-2">
              {[0, 1, 2].map((placeholderIndex) => (
                <Skeleton key={placeholderIndex} className="h-16 w-full rounded-2xl" />
              ))}
            </div>
          ) : invoiceLoadError ? (
            <p className="py-6 text-center text-sm text-destructive">Can't load invoices: {invoiceLoadError}</p>
          ) : invoices && invoices.length === 0 ? (
            <Empty>
              <EmptyHeader>
                <EmptyTitle>No invoices yet</EmptyTitle>
              </EmptyHeader>
            </Empty>
          ) : (
            <div id="invoice_list">
              {groupInvoicesByDay(invoices ?? []).map((dayGroup) => (
                <section key={dayGroup.dayKey}>
                  <h2 className="mt-3 mb-1.5 px-1 text-sm font-medium text-muted-foreground">
                    {dayGroup.dayHeading}
                  </h2>
                  <ItemGroup className="gap-0 rounded-2xl border">
                    {dayGroup.invoices.map((invoice) => (
                      <Item key={invoice.id} asChild size="sm" className="min-h-16 rounded-none border-b-border first:rounded-t-2xl last:rounded-b-2xl last:border-b-transparent">
                        <button type="button" data-testid="invoice_row" onClick={() => setOpenedInvoiceId(invoice.id)} className="w-full text-left">
                          <ItemContent className="min-w-0">
                            <ItemTitle className={cn("block w-full truncate", !invoice.note && "font-normal text-muted-foreground")}>{invoice.note || "No note"}</ItemTitle>
                            <ItemDescription className="truncate">{describeInvoiceRowLine(invoice, paymentLinkById)}</ItemDescription>
                          </ItemContent>
                          <ItemActions className="flex-col items-end gap-1">
                            <span className="font-medium tabular-nums">{describeInvoiceAmount(invoice)}</span>
                            <InvoiceStatusBadge invoiceStatus={invoice.status} paymentIsSeen={invoice.seen} />
                          </ItemActions>
                        </button>
                      </Item>
                    ))}
                  </ItemGroup>
                </section>
              ))}
            </div>
          )}
          {invoices && invoices.length > 0 && olderInvoicesCursor !== null ? (
            <LoadOlderButton id="load_older_invoices_button" isLoading={olderInvoicesAreLoading} loadError={olderInvoicesLoadError} onLoadOlder={() => void loadOlderInvoices()} />
          ) : null}
        </TabsContent>
        <TabsContent value="payment_links" className="mt-3">
          <AccountPaymentLinkList paymentLinks={paymentLinks} linkLoadError={linkLoadError} liveReloadCount={liveReloadCount} onPaymentLinkChange={replacePaymentLink} />
          {/* Repeating invoices sit under the links: five tabs don't fit a 390 px phone (PLAN.md section 18). */}
          <h2 id="invoice_schedules" className="mt-6 mb-2 scroll-mt-24 px-1 text-sm font-medium text-muted-foreground">
            Repeating invoices
          </h2>
          <AccountInvoiceScheduleList
            invoiceSchedules={invoiceSchedules}
            scheduleLoadError={scheduleLoadError}
            liveReloadCount={liveReloadCount}
            onInvoiceScheduleChange={replaceInvoiceSchedule}
          />
        </TabsContent>
        <TabsContent value="static_deposit_addresses" className="mt-3">
          <AccountStaticDepositAddressList
            staticDepositAddresses={accountStaticDepositAddresses.staticDepositAddresses}
            addressLoadError={accountStaticDepositAddresses.addressLoadError}
            hasOlderAddresses={accountStaticDepositAddresses.hasOlderAddresses}
            olderAddressesAreLoading={accountStaticDepositAddresses.olderAddressesAreLoading}
            olderAddressesLoadError={accountStaticDepositAddresses.olderAddressesLoadError}
            onLoadOlderAddresses={() => void accountStaticDepositAddresses.loadOlderAddresses()}
            liveReloadCount={liveReloadCount}
          />
        </TabsContent>
        <TabsContent value="events" className="mt-3">
          {sortedEvents.length === 0 ? (
            <Empty>
              <EmptyHeader>
                <EmptyTitle>{eventsCaughtUp ? "Quiet so far" : "Connecting…"}</EmptyTitle>
              </EmptyHeader>
            </Empty>
          ) : (
            <ItemGroup id="event_list" className="gap-0 rounded-2xl border">
              {sortedEvents.map((accountEvent) => (
                <Item key={accountEvent.id} asChild size="sm" className="min-h-14 rounded-none border-b-border last:border-b-transparent">
                  <button type="button" onClick={() => openAccountEvent(accountEvent)} className="w-full text-left">
                    <span
                      aria-hidden="true"
                      className={cn(
                        "size-2 shrink-0 rounded-full",
                        { success: "bg-success", warning: "bg-warning", danger: "bg-destructive", info: "bg-foreground/40" }[accountEventTones[accountEvent.type] ?? "info"],
                      )}
                    />
                    <ItemContent className="min-w-0">
                      <ItemTitle>{accountEventTitles[accountEvent.type] ?? accountEvent.type}</ItemTitle>
                      <ItemDescription className="truncate">
                        {describeAccountEventLine(accountEvent)}
                      </ItemDescription>
                    </ItemContent>
                    <span className="shrink-0 text-xs text-muted-foreground">{formatShortTime(accountEvent.created_at)}</span>
                  </button>
                </Item>
              ))}
            </ItemGroup>
          )}
          {hasOlderAccountEvents ? (
            <LoadOlderButton id="load_older_events_button" isLoading={olderAccountEventsAreLoading} loadError={olderAccountEventsLoadError} onLoadOlder={loadOlderAccountEvents} />
          ) : null}
        </TabsContent>
      </Tabs>

      <InvoiceActionsOverlay
        invoice={openedInvoice}
        onClose={() => setOpenedInvoiceId(null)}
        onInvoiceChange={(nextInvoice) =>
          setInvoices((currentInvoices) => currentInvoices?.map((invoice) => (invoice.id === nextInvoice.id ? nextInvoice : invoice)) ?? null)
        }
      />
    </PageColumn>
  )
}
