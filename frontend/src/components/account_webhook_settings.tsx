// `/settings`' "Webhook" row ("Off" or the URL's host) and its sheet (2026-10-12, rows 2026-10-14): one URL the
// account's own server gets every invoice event at, signed per Standard Webhooks. Setting or changing the URL and
// rotating the secret ask for a fresh passkey check; the `whsec_` secret shows once, as the sheet's next step.
// "Send test event" queues the picked type (2026-10-21): `webhook.test` (a message, the default) or a sample invoice in
// the state that invoice event leaves one in, signed like a real one and marked `"test": true`. The newest deliveries
// list with their status and attempts, each with Resend. Everything acts at once.
import { ArrowLeft, Fingerprint, RefreshCw, RotateCw, Send, Webhook } from "lucide-react"
import { useCallback, useEffect, useState, type FormEvent } from "react"
import { toast } from "sonner"

import { AccountSettingsRow } from "@/components/account_settings_row"
import { ResponsiveSheet } from "@/components/responsive_sheet"
import { TapToCopyButton } from "@/components/tap_to_copy_button"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Field, FieldError, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Item, ItemActions, ItemContent, ItemDescription, ItemGroup, ItemMedia, ItemTitle } from "@/components/ui/item"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { useAccountSession } from "@/hooks/use_account_session"
import { ApiError, describeError, fetchApiJson } from "@/lib/api_client"
import type { AccountWebhook, AccountWebhookDelivery, ChangedAccountWebhook } from "@/lib/api_types"
import { primaryActionButtonClassName, touchTargetButtonClassName } from "@/lib/button_size_class_names"
import {
  describePasskeyError,
  passkeysAreSupported,
  rotateAccountWebhookSecretWithPasskey,
  setAccountWebhookWithPasskey,
} from "@/lib/passkey_access"
import { formatShortTime } from "@/lib/time_formatting"
import { cn } from "@/lib/utils"

// While a delivery is queued or retrying soon, the list rereads itself this often.
const pendingDeliveryRefreshMilliseconds = 4000

const failureReasonDescriptions: Record<string, string> = {
  timeout: "no answer in 15 s",
  connection_failed: "couldn't connect",
  unresolvable_host: "the host name doesn't resolve",
  refused_address: "the host isn't on the public internet",
  no_webhook: "the webhook was removed",
  receiver_gone: "your server answered 410 Gone",
}

// The types "Send test event" offers, as `POST /api/v1/account-webhook/test-events` takes them: the plain test first,
// then every invoice event an account's EVM invoice can send (PLAN.md section 8), a schedule's new invoice (section
// 18) and every customer address event (PLAN.md section 17); no refund events, which only invoices paying the
// operator's own address get. Kept in step with the API's WebhookTestEventType (a 400 otherwise).
const testWebhookEventTypes = [
  "webhook.test",
  "invoice.payment_detected",
  "invoice.underpaid",
  "invoice.paid",
  "invoice.overpaid",
  "invoice.expired",
  "invoice.cancelled",
  "invoice.swept",
  "invoice.sweep_failed",
  "invoice.late_payment",
  "invoice.stray_payment",
  "schedule.invoice_created",
  "static_address.payment_received",
  "static_address.stray_payment",
  "static_address.swept",
  "static_address.sweep_failed",
]

// What the sheet shows: the webhook, the URL field, or a secret the API just answered.
type WebhookSheetStep = "overview" | "url" | "secret"

function describeDeliveryStatus(delivery: AccountWebhookDelivery): { label: string; variant: "secondary" | "destructive" | "outline" } {
  if (delivery.status === "delivered") return { label: "Delivered", variant: "secondary" }
  if (delivery.status === "failed") return { label: "Failed", variant: "destructive" }
  return { label: delivery.attempt_count ? "Retrying" : "Queued", variant: "outline" }
}

function describeDeliveryAttempt(delivery: AccountWebhookDelivery): string {
  const attemptsText = `${delivery.attempt_count} ${delivery.attempt_count === 1 ? "attempt" : "attempts"}`
  const lastAnswerText = delivery.last_failure_reason
    ? delivery.last_failure_reason === "http_status"
      ? `answered ${delivery.last_response_status}`
      : (failureReasonDescriptions[delivery.last_failure_reason] ?? delivery.last_failure_reason)
    : delivery.last_response_status
      ? `answered ${delivery.last_response_status}`
      : ""
  const nextText = delivery.status === "pending" && delivery.next_attempt_at && delivery.attempt_count ? `, next ${formatShortTime(delivery.next_attempt_at)}` : ""
  return [attemptsText, lastAnswerText].filter(Boolean).join(", ") + nextText
}

// The row's value: the host alone fits a phone row; the sheet shows the whole URL.
function describeWebhookHost(webhookUrl: string): string {
  try {
    return new URL(webhookUrl).host
  } catch {
    return webhookUrl
  }
}

export function AccountWebhookSettings() {
  const { refreshAccountSession } = useAccountSession()
  const [accountWebhook, setAccountWebhook] = useState<AccountWebhook | null>(null)
  const [webhookLoadError, setWebhookLoadError] = useState("")
  const [sheetIsOpen, setSheetIsOpen] = useState(false)
  const [sheetStep, setSheetStep] = useState<WebhookSheetStep>("overview")
  // A secret the API just answered (first set or rotation): shown once, as the sheet's "secret" step.
  const [shownSecret, setShownSecret] = useState("")
  const [passkeyPromptIsOpen, setPasskeyPromptIsOpen] = useState(false)
  const [isSendingTest, setIsSendingTest] = useState(false)
  const [testEventType, setTestEventType] = useState(testWebhookEventTypes[0])

  // The state changes in the promise callbacks, once the API answers, never synchronously in the effects below.
  const reloadAccountWebhook = useCallback(
    () =>
      fetchApiJson<AccountWebhook>("/api/v1/account-webhook").then(
        (loadedWebhook) => {
          setAccountWebhook(loadedWebhook)
          setWebhookLoadError("")
        },
        (loadFailure: unknown) => setWebhookLoadError(describeError(loadFailure)),
      ),
    [],
  )

  useEffect(() => {
    void reloadAccountWebhook()
  }, [reloadAccountWebhook])

  const hasPendingDelivery = accountWebhook?.deliveries.some((delivery) => delivery.status === "pending") ?? false
  useEffect(() => {
    if (!hasPendingDelivery) return
    const refreshTimer = window.setInterval(() => {
      if (document.visibilityState === "visible") void reloadAccountWebhook()
    }, pendingDeliveryRefreshMilliseconds)
    return () => window.clearInterval(refreshTimer)
  }, [hasPendingDelivery, reloadAccountWebhook])

  function changeSheetOpenState(nextIsOpen: boolean) {
    if (passkeyPromptIsOpen) return
    setSheetIsOpen(nextIsOpen)
    if (!nextIsOpen) {
      // The secret shows once: closing the sheet drops it.
      setSheetStep("overview")
      setShownSecret("")
    }
  }

  async function handleActionFailure(actionFailure: unknown) {
    if (actionFailure instanceof ApiError && actionFailure.httpStatus === 401) await refreshAccountSession()
    else toast.error(describePasskeyError(actionFailure))
  }

  async function rotateSecret() {
    setPasskeyPromptIsOpen(true)
    try {
      const updatedWebhook = await rotateAccountWebhookSecretWithPasskey()
      setAccountWebhook(updatedWebhook)
      setShownSecret(updatedWebhook.secret ?? "")
      setSheetStep("secret")
    } catch (rotateFailure) {
      await handleActionFailure(rotateFailure)
    } finally {
      setPasskeyPromptIsOpen(false)
    }
  }

  async function removeWebhook() {
    try {
      await fetchApiJson<unknown>("/api/v1/account-webhook", { method: "DELETE" })
      toast.success("Webhook removed.")
      await reloadAccountWebhook()
    } catch (removeFailure) {
      await handleActionFailure(removeFailure)
    }
  }

  async function sendTestEvent() {
    setIsSendingTest(true)
    try {
      await fetchApiJson<AccountWebhookDelivery>("/api/v1/account-webhook/test-events", {
        method: "POST",
        body: { event_type: testEventType },
      })
      toast.success(`Test ${testEventType} queued. It shows below once sent.`)
      await reloadAccountWebhook()
    } catch (testFailure) {
      await handleActionFailure(testFailure)
    } finally {
      setIsSendingTest(false)
    }
  }

  async function resendDelivery(delivery: AccountWebhookDelivery) {
    try {
      await fetchApiJson<AccountWebhookDelivery>(`/api/v1/account-webhook/deliveries/${delivery.id}/resend`, { method: "POST" })
      toast.success(`${delivery.event_type} queued again.`)
      await reloadAccountWebhook()
    } catch (resendFailure) {
      await handleActionFailure(resendFailure)
    }
  }

  const webhookUrl = accountWebhook?.url ?? null
  // Standard Webhooks: a 410 Gone means the receiver wants no more, so the API stopped sending until the URL is set again.
  const webhookDisabledAt = accountWebhook?.disabled_at ?? null
  const sheetTitles: Record<WebhookSheetStep, string> = {
    overview: "Webhook",
    url: webhookUrl ? "Change webhook URL" : "Set a webhook",
    secret: "Copy your signing secret",
  }
  const sheetDescriptions: Record<WebhookSheetStep, string | undefined> = {
    overview: undefined,
    url: webhookUrl ? "Events go to the new URL, signed with the same secret." : "The signing secret shows once, right after.",
    secret: "This is the only time it shows. Your server checks each webhook's signature with it.",
  }
  return (
    <>
      <AccountSettingsRow
        id="settings_webhook_row"
        title="Webhook"
        valueText={webhookLoadError ? "Can't load" : accountWebhook === null ? "…" : webhookDisabledAt ? "Stopped" : webhookUrl ? describeWebhookHost(webhookUrl) : "Off"}
        valueIsMonospace={Boolean(webhookUrl) && !webhookDisabledAt && !webhookLoadError}
        valueTone={webhookLoadError || webhookDisabledAt ? "problem" : "default"}
        onOpen={() => setSheetIsOpen(true)}
      />
      <ResponsiveSheet
        isOpen={sheetIsOpen}
        onOpenChange={changeSheetOpenState}
        title={sheetTitles[sheetStep]}
        description={sheetDescriptions[sheetStep]}
        testId="settings_webhook_sheet"
      >
        {sheetStep === "secret" ? (
          <div className="flex flex-col">
            <TapToCopyButton
              id="settings_webhook_secret"
              textToCopy={shownSecret}
              accessibleLabel="Copy the webhook signing secret"
              className="rounded-xl border px-3.5 py-3 text-left font-mono text-[15px] break-all"
            >
              {shownSecret}
            </TapToCopyButton>
            <Button
              id="settings_webhook_secret_done_button"
              type="button"
              className={cn(primaryActionButtonClassName, "mt-8 w-full")}
              onClick={() => {
                setShownSecret("")
                setSheetStep("overview")
              }}
            >
              Done
            </Button>
          </div>
        ) : sheetStep === "url" ? (
          <WebhookUrlForm
            currentWebhookUrl={webhookUrl}
            onPasskeyPromptChange={setPasskeyPromptIsOpen}
            onBack={() => setSheetStep("overview")}
            onSaved={(updatedWebhook) => {
              setAccountWebhook(updatedWebhook)
              if (updatedWebhook.secret) {
                setShownSecret(updatedWebhook.secret)
                setSheetStep("secret")
              } else {
                toast.success("Webhook URL changed. The secret stays the same.")
                setSheetStep("overview")
              }
            }}
          />
        ) : webhookLoadError ? (
          <Alert id="settings_webhook_message" variant="destructive">
            <AlertDescription>Can't load your webhook: {webhookLoadError}</AlertDescription>
          </Alert>
        ) : accountWebhook === null ? (
          <Skeleton className="h-16 w-full rounded-2xl" />
        ) : (
          <div className="flex flex-col gap-3">
            <p id="settings_webhook_url" className={cn("rounded-xl border px-3.5 py-3 font-mono text-[15px] break-all", !webhookUrl && "text-muted-foreground")}>
              {webhookUrl ?? "No webhook yet"}
            </p>
            {webhookDisabledAt ? (
              <Alert id="settings_webhook_disabled" variant="destructive">
                <AlertDescription>
                  Your server answered 410 Gone, so events stopped ({formatShortTime(webhookDisabledAt)}). Set the URL again
                  to turn it back on.
                </AlertDescription>
              </Alert>
            ) : null}
            <div className="flex flex-wrap gap-2">
              {passkeysAreSupported() ? (
                <Button id="settings_set_webhook_button" type="button" variant="secondary" className={touchTargetButtonClassName} onClick={() => setSheetStep("url")}>
                  {webhookUrl ? "Change URL" : "Set URL"}
                </Button>
              ) : null}
              {webhookUrl ? (
                <>
                  <AlertDialog>
                    <AlertDialogTrigger asChild>
                      <Button
                        id="settings_rotate_webhook_secret_button"
                        type="button"
                        variant="secondary"
                        className={touchTargetButtonClassName}
                        disabled={passkeyPromptIsOpen || !passkeysAreSupported()}
                      >
                        {passkeyPromptIsOpen ? <Spinner /> : <RotateCw />}
                        New secret
                      </Button>
                    </AlertDialogTrigger>
                    <AlertDialogContent>
                      <AlertDialogHeader>
                        <AlertDialogTitle>Make a new signing secret?</AlertDialogTitle>
                        <AlertDialogDescription>
                          The old one stops signing at once, so update your server right after. It asks for your passkey.
                        </AlertDialogDescription>
                      </AlertDialogHeader>
                      <AlertDialogFooter>
                        <AlertDialogCancel className={touchTargetButtonClassName}>Keep it</AlertDialogCancel>
                        <AlertDialogAction id="settings_confirm_rotate_webhook_secret_button" className={touchTargetButtonClassName} onClick={() => void rotateSecret()}>
                          Make a new one
                        </AlertDialogAction>
                      </AlertDialogFooter>
                    </AlertDialogContent>
                  </AlertDialog>
                  <AlertDialog>
                    <AlertDialogTrigger asChild>
                      <Button id="settings_remove_webhook_button" type="button" variant="ghost" className={touchTargetButtonClassName}>
                        Remove
                      </Button>
                    </AlertDialogTrigger>
                    <AlertDialogContent>
                      <AlertDialogHeader>
                        <AlertDialogTitle>Remove the webhook?</AlertDialogTitle>
                        <AlertDialogDescription>
                          Your server gets no more events, and deliveries not sent yet stop. Setting one again makes a new
                          secret.
                        </AlertDialogDescription>
                      </AlertDialogHeader>
                      <AlertDialogFooter>
                        <AlertDialogCancel className={touchTargetButtonClassName}>Keep it</AlertDialogCancel>
                        <AlertDialogAction
                          id="settings_confirm_remove_webhook_button"
                          variant="destructive"
                          className={touchTargetButtonClassName}
                          onClick={() => void removeWebhook()}
                        >
                          Remove
                        </AlertDialogAction>
                      </AlertDialogFooter>
                    </AlertDialogContent>
                  </AlertDialog>
                </>
              ) : null}
            </div>
            {webhookUrl ? (
              <div className="flex min-w-0 gap-2">
                <Select value={testEventType} onValueChange={setTestEventType}>
                  <SelectTrigger
                    id="settings_test_webhook_event_type_select"
                    aria-label="Test event type"
                    className="h-11! min-w-0 flex-1 rounded-xl font-mono text-sm"
                    disabled={Boolean(webhookDisabledAt)}
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {testWebhookEventTypes.map((eventType) => (
                      <SelectItem key={eventType} value={eventType} className="min-h-11 font-mono text-sm">
                        {eventType}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Button
                  id="settings_send_test_webhook_button"
                  type="button"
                  variant="secondary"
                  className={cn(touchTargetButtonClassName, "shrink-0")}
                  disabled={isSendingTest || Boolean(webhookDisabledAt)}
                  onClick={() => void sendTestEvent()}
                >
                  {isSendingTest ? <Spinner /> : <Send />}
                  Send test
                </Button>
              </div>
            ) : null}
            {accountWebhook.deliveries.length ? (
              <ItemGroup className="gap-2" id="settings_webhook_delivery_list">
                <div className="flex items-center justify-between gap-3">
                  <FieldLabel>Last deliveries</FieldLabel>
                  <Button type="button" variant="ghost" className={touchTargetButtonClassName} aria-label="Reload the deliveries" onClick={() => void reloadAccountWebhook()}>
                    <RefreshCw />
                  </Button>
                </div>
                {accountWebhook.deliveries.map((delivery) => {
                  const deliveryStatus = describeDeliveryStatus(delivery)
                  return (
                    <Item key={delivery.id} variant="outline" className="min-h-14 rounded-2xl" data-testid="settings_webhook_delivery_row">
                      <ItemMedia variant="icon">
                        <Webhook />
                      </ItemMedia>
                      <ItemContent className="min-w-0">
                        <ItemTitle className="flex-wrap">
                          <span className="font-mono text-sm break-all">{delivery.event_type}</span>
                          <Badge variant={deliveryStatus.variant} data-testid="settings_webhook_delivery_status">
                            {deliveryStatus.label}
                          </Badge>
                        </ItemTitle>
                        <ItemDescription>
                          {formatShortTime(delivery.created_at)}, {describeDeliveryAttempt(delivery)}
                        </ItemDescription>
                      </ItemContent>
                      <ItemActions className="gap-0">
                        <Button
                          type="button"
                          variant="ghost"
                          className={touchTargetButtonClassName}
                          data-testid="settings_resend_webhook_delivery_button"
                          disabled={!webhookUrl || Boolean(webhookDisabledAt)}
                          onClick={() => void resendDelivery(delivery)}
                        >
                          Resend
                        </Button>
                      </ItemActions>
                    </Item>
                  )
                })}
              </ItemGroup>
            ) : null}
          </div>
        )}
      </ResponsiveSheet>
    </>
  )
}

function WebhookUrlForm({
  currentWebhookUrl,
  onPasskeyPromptChange,
  onBack,
  onSaved,
}: {
  currentWebhookUrl: string | null
  onPasskeyPromptChange(passkeyPromptIsOpen: boolean): void
  onBack(): void
  onSaved(updatedWebhook: ChangedAccountWebhook): void
}) {
  const { refreshAccountSession } = useAccountSession()
  const [webhookUrlText, setWebhookUrlText] = useState(currentWebhookUrl ?? "")
  const [webhookUrlError, setWebhookUrlError] = useState("")
  const [saveError, setSaveError] = useState("")
  const [passkeyPromptIsOpen, setPasskeyPromptIsOpen] = useState(false)

  function changePasskeyPromptState(nextPasskeyPromptIsOpen: boolean) {
    setPasskeyPromptIsOpen(nextPasskeyPromptIsOpen)
    onPasskeyPromptChange(nextPasskeyPromptIsOpen)
  }

  async function confirmWithPasskey(submitEvent: FormEvent) {
    submitEvent.preventDefault()
    setSaveError("")
    const trimmedUrl = webhookUrlText.trim()
    let nextUrlError = ""
    try {
      if (!trimmedUrl) nextUrlError = "Paste your server's webhook URL."
      else if (!/^https?:$/.test(new URL(trimmedUrl).protocol)) nextUrlError = "Use an https:// URL."
    } catch {
      nextUrlError = "That's not a URL. It starts with https://."
    }
    setWebhookUrlError(nextUrlError)
    if (nextUrlError) return
    changePasskeyPromptState(true)
    try {
      const updatedWebhook = await setAccountWebhookWithPasskey(trimmedUrl)
      changePasskeyPromptState(false)
      onSaved(updatedWebhook)
    } catch (saveFailure) {
      changePasskeyPromptState(false)
      if (saveFailure instanceof ApiError && saveFailure.httpStatus === 401) {
        await refreshAccountSession()
        return
      }
      if (saveFailure instanceof ApiError && saveFailure.message.startsWith("url:")) setWebhookUrlError(describeError(saveFailure))
      else setSaveError(describePasskeyError(saveFailure))
    }
  }

  return (
    <form onSubmit={(submitEvent) => void confirmWithPasskey(submitEvent)} noValidate className="flex flex-col">
      <Field data-invalid={Boolean(webhookUrlError) || undefined}>
        <FieldLabel htmlFor="settings_webhook_url_input">URL</FieldLabel>
        <Input
          id="settings_webhook_url_input"
          type="url"
          inputMode="url"
          maxLength={2048}
          autoComplete="off"
          spellCheck={false}
          autoCapitalize="off"
          placeholder="e.g. https://yourshop.com/webhooks/pay"
          value={webhookUrlText}
          aria-invalid={Boolean(webhookUrlError)}
          onChange={(changeEvent) => {
            setWebhookUrlText(changeEvent.target.value)
            setWebhookUrlError("")
            setSaveError("")
          }}
          className="h-12 rounded-xl px-3.5 font-mono text-base"
        />
        <FieldError id="settings_webhook_url_error">{webhookUrlError}</FieldError>
      </Field>
      {saveError ? (
        <Alert id="settings_webhook_change_message" variant="destructive" className="mt-4">
          <AlertDescription>{saveError}</AlertDescription>
        </Alert>
      ) : null}
      <Button id="settings_confirm_webhook_button" type="submit" className={cn(primaryActionButtonClassName, "mt-6 w-full")} disabled={passkeyPromptIsOpen}>
        {passkeyPromptIsOpen ? <Spinner /> : <Fingerprint />}
        {passkeyPromptIsOpen ? "Waiting for your passkey…" : "Confirm with passkey"}
      </Button>
      <Button type="button" variant="ghost" className={cn(touchTargetButtonClassName, "mt-2 w-full")} disabled={passkeyPromptIsOpen} onClick={onBack}>
        <ArrowLeft /> Back
      </Button>
    </form>
  )
}
