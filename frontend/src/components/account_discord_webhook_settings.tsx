// `/settings`' "Discord" row ("Off" or the webhook's id) and its sheet (2026-10-13, rows 2026-10-14): a Discord channel
// webhook that gets a short line for every invoice event, in /activity's words. Setting it asks for a fresh passkey
// check, like the webhook URL; the page never gets the URL back (its token posts to the channel), only the webhook's
// id. "Send test message" queues one; the last message's status shows under it. Once Discord says the webhook was
// deleted, sending stops and this says so. Acts at once. On the operator's account, "Alert reminders" sets how often an
// operator alert that lasts is sent again (2026-10-18); off sends each problem once.
import { ArrowLeft, Fingerprint, Send } from "lucide-react"
import { useCallback, useEffect, useState, type FormEvent } from "react"
import { toast } from "sonner"

import { AccountSettingsRow } from "@/components/account_settings_row"
import { ResponsiveSheet } from "@/components/responsive_sheet"
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
import { Button } from "@/components/ui/button"
import { Field, FieldError, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { useAccountSession } from "@/hooks/use_account_session"
import { ApiError, describeError, fetchApiJson } from "@/lib/api_client"
import type { AccountDiscordWebhook, AccountWebhookDelivery } from "@/lib/api_types"
import { primaryActionButtonClassName, touchTargetButtonClassName } from "@/lib/button_size_class_names"
import { describePasskeyError, passkeysAreSupported, setAccountDiscordWebhookWithPasskey } from "@/lib/passkey_access"
import { formatShortTime } from "@/lib/time_formatting"
import { cn } from "@/lib/utils"

// The reminder choices; the API takes any whole hour from 0 to 168, and one set there shows too.
const operatorAlertRepeatHourChoices = [0, 1, 2, 4, 8, 12, 24]

function describeOperatorAlertRepeatHours(repeatHours: number): string {
  if (repeatHours === 0) return "Off, alert once"
  return repeatHours === 1 ? "Every hour" : `Every ${repeatHours} hours`
}

// While the last message is queued or retrying soon, the state rereads itself this often.
const pendingMessageRefreshMilliseconds = 4000

// The URLs the API takes (backend discord_webhook_urls.py): Discord's "Copy Webhook URL", and for a forum or media
// channel, whose webhook posts only into a thread, the same with `?thread_id=<thread id>`.
const discordWebhookHosts = new Set(["discord.com", "ptb.discord.com", "canary.discord.com", "discordapp.com"])
const discordWebhookPathPattern = /^\/api(\/v\d{1,2})?\/webhooks\/\d{15,20}\/[A-Za-z0-9_-]{20,200}\/?$/
const discordThreadQueryPattern = /^\?thread_id=\d{15,20}$/

const messageFailureDescriptions: Record<string, string> = {
  timeout: "Discord didn't answer in 15 s",
  connection_failed: "couldn't reach Discord",
  unresolvable_host: "couldn't reach Discord",
  refused_address: "couldn't reach Discord",
  no_webhook: "the webhook was removed",
  rate_limited: "Discord asked to wait",
  discord_webhook_deleted: "the webhook is gone in Discord",
  discord_refused_message: "Discord refused the message",
  discord_thread_required: "this is a forum channel, so set the URL with ?thread_id= of a post",
}

function checkDiscordWebhookUrl(typedUrl: string): string {
  if (!typedUrl) return "Paste the webhook URL from Discord."
  let parsedUrl: URL
  try {
    parsedUrl = new URL(typedUrl)
  } catch {
    return "That's not a URL. It starts with https://discord.com/api/webhooks/."
  }
  const isDiscordWebhookUrl =
    parsedUrl.protocol === "https:" &&
    discordWebhookHosts.has(parsedUrl.hostname) &&
    !parsedUrl.port &&
    (!parsedUrl.search || discordThreadQueryPattern.test(parsedUrl.search)) &&
    !parsedUrl.hash &&
    discordWebhookPathPattern.test(parsedUrl.pathname)
  return isDiscordWebhookUrl ? "" : "Use the URL from Discord's Copy Webhook URL: https://discord.com/api/webhooks/…"
}

function describeLatestMessage(latestMessage: AccountWebhookDelivery): string {
  const timeText = formatShortTime(latestMessage.created_at)
  if (latestMessage.status === "delivered") return `Last message sent ${timeText}.`
  const reasonText = latestMessage.last_failure_reason
    ? latestMessage.last_failure_reason === "http_status"
      ? `Discord answered ${latestMessage.last_response_status}`
      : (messageFailureDescriptions[latestMessage.last_failure_reason] ?? latestMessage.last_failure_reason)
    : ""
  if (latestMessage.status === "failed") return `Last message (${timeText}) didn't go out: ${reasonText || "it gave up"}.`
  if (!latestMessage.attempt_count) return `Last message (${timeText}) is on its way.`
  const nextText = latestMessage.next_attempt_at ? `, trying again ${formatShortTime(latestMessage.next_attempt_at)}` : ""
  return `Last message (${timeText}) not sent yet: ${reasonText || "retrying"}${nextText}.`
}

export function AccountDiscordWebhookSettings() {
  const { refreshAccountSession } = useAccountSession()
  const [discordWebhook, setDiscordWebhook] = useState<AccountDiscordWebhook | null>(null)
  const [loadError, setLoadError] = useState("")
  const [sheetIsOpen, setSheetIsOpen] = useState(false)
  // The sheet shows the URL field instead of the webhook.
  const [showsUrlForm, setShowsUrlForm] = useState(false)
  const [passkeyPromptIsOpen, setPasskeyPromptIsOpen] = useState(false)
  const [isSendingTest, setIsSendingTest] = useState(false)

  // The state changes in the promise callbacks, once the API answers, never synchronously in the effects below.
  const reloadDiscordWebhook = useCallback(
    () =>
      fetchApiJson<AccountDiscordWebhook>("/api/v1/account-discord-webhook").then(
        (loadedDiscordWebhook) => {
          setDiscordWebhook(loadedDiscordWebhook)
          setLoadError("")
        },
        (loadFailure: unknown) => setLoadError(describeError(loadFailure)),
      ),
    [],
  )

  useEffect(() => {
    void reloadDiscordWebhook()
  }, [reloadDiscordWebhook])

  const latestMessageIsPending = discordWebhook?.latest_message?.status === "pending"
  useEffect(() => {
    if (!latestMessageIsPending) return
    const refreshTimer = window.setInterval(() => {
      if (document.visibilityState === "visible") void reloadDiscordWebhook()
    }, pendingMessageRefreshMilliseconds)
    return () => window.clearInterval(refreshTimer)
  }, [latestMessageIsPending, reloadDiscordWebhook])

  async function handleActionFailure(actionFailure: unknown) {
    if (actionFailure instanceof ApiError && actionFailure.httpStatus === 401) await refreshAccountSession()
    else toast.error(describePasskeyError(actionFailure))
  }

  async function sendTestMessage() {
    setIsSendingTest(true)
    try {
      await fetchApiJson<AccountWebhookDelivery>("/api/v1/account-discord-webhook/test-messages", { method: "POST" })
      toast.success("Test message queued. Check your Discord channel.")
      await reloadDiscordWebhook()
    } catch (testFailure) {
      await handleActionFailure(testFailure)
    } finally {
      setIsSendingTest(false)
    }
  }

  async function removeDiscordWebhook() {
    try {
      await fetchApiJson<unknown>("/api/v1/account-discord-webhook", { method: "DELETE" })
      toast.success("Discord webhook removed.")
      await reloadDiscordWebhook()
    } catch (removeFailure) {
      await handleActionFailure(removeFailure)
    }
  }

  const discordWebhookId = discordWebhook?.discord_webhook_id ?? null
  const isDeletedInDiscord = discordWebhook?.status === "deleted_in_discord"
  return (
    <>
      <AccountSettingsRow
        id="settings_discord_row"
        title="Discord"
        valueText={loadError ? "Can't load" : discordWebhook === null ? "…" : isDeletedInDiscord ? "Gone in Discord" : (discordWebhookId ?? "Off")}
        valueIsMonospace={Boolean(discordWebhookId) && !isDeletedInDiscord && !loadError}
        valueTone={loadError || isDeletedInDiscord ? "problem" : "default"}
        onOpen={() => setSheetIsOpen(true)}
      />
      <ResponsiveSheet
        isOpen={sheetIsOpen}
        onOpenChange={(nextIsOpen) => {
          if (passkeyPromptIsOpen) return
          setSheetIsOpen(nextIsOpen)
          if (!nextIsOpen) setShowsUrlForm(false)
        }}
        title={showsUrlForm ? (discordWebhookId ? "Change Discord webhook" : "Set a Discord webhook") : "Discord"}
        description={showsUrlForm ? "In Discord: channel settings, Integrations, Webhooks, Copy Webhook URL. A forum channel's URL needs ?thread_id= and a post's id." : undefined}
        testId="settings_discord_sheet"
      >
        {showsUrlForm ? (
          <DiscordWebhookUrlForm
            onPasskeyPromptChange={setPasskeyPromptIsOpen}
            onBack={() => setShowsUrlForm(false)}
            onSaved={(updatedDiscordWebhook) => {
              setDiscordWebhook(updatedDiscordWebhook)
              setShowsUrlForm(false)
              toast.success("Discord webhook set. Send a test message to check it.")
            }}
          />
        ) : loadError ? (
          <Alert id="settings_discord_webhook_message" variant="destructive">
            <AlertDescription>Can't load your Discord webhook: {loadError}</AlertDescription>
          </Alert>
        ) : discordWebhook === null ? (
          <Skeleton className="h-16 w-full rounded-2xl" />
        ) : (
          <div className="flex flex-col gap-3">
            <div>
              <p
                id="settings_discord_webhook_state"
                className={cn("rounded-xl border px-3.5 py-3 text-[15px] break-all", !discordWebhookId && "text-muted-foreground")}
              >
                {discordWebhookId
                  ? `Webhook ${discordWebhookId}${discordWebhook.discord_thread_id ? `, thread ${discordWebhook.discord_thread_id}` : ""}`
                  : "No Discord channel yet"}
              </p>
              {discordWebhook.latest_message && !isDeletedInDiscord ? (
                <p id="settings_discord_latest_message" className="mt-2 px-1 text-sm text-muted-foreground">
                  {describeLatestMessage(discordWebhook.latest_message)}
                </p>
              ) : null}
            </div>
            {discordWebhook.operator_alert_repeat_hours !== null && !isDeletedInDiscord ? (
              <OperatorAlertRepeatField
                repeatHours={discordWebhook.operator_alert_repeat_hours}
                onSaved={setDiscordWebhook}
                onActionFailure={handleActionFailure}
              />
            ) : null}
            {isDeletedInDiscord ? (
              <Alert id="settings_discord_webhook_deleted" variant="destructive">
                <AlertDescription>
                  Discord says this webhook (or its thread) is gone, so messages stopped
                  {discordWebhook.deleted_in_discord_at ? ` (${formatShortTime(discordWebhook.deleted_in_discord_at)})` : ""}. Make a
                  new one in Discord and set it here.
                </AlertDescription>
              </Alert>
            ) : null}
            <div className="flex flex-wrap gap-2">
              {passkeysAreSupported() ? (
                <Button
                  id="settings_set_discord_webhook_button"
                  type="button"
                  variant="secondary"
                  className={touchTargetButtonClassName}
                  onClick={() => setShowsUrlForm(true)}
                >
                  {discordWebhookId ? "Change" : "Set"}
                </Button>
              ) : null}
              {discordWebhookId && !isDeletedInDiscord ? (
                <Button
                  id="settings_send_test_discord_message_button"
                  type="button"
                  variant="secondary"
                  className={touchTargetButtonClassName}
                  disabled={isSendingTest}
                  onClick={() => void sendTestMessage()}
                >
                  {isSendingTest ? <Spinner /> : <Send />}
                  Send test message
                </Button>
              ) : null}
              {discordWebhookId ? (
                <AlertDialog>
                  <AlertDialogTrigger asChild>
                    <Button id="settings_remove_discord_webhook_button" type="button" variant="ghost" className={touchTargetButtonClassName}>
                      Remove
                    </Button>
                  </AlertDialogTrigger>
                  <AlertDialogContent>
                    <AlertDialogHeader>
                      <AlertDialogTitle>Remove the Discord webhook?</AlertDialogTitle>
                      <AlertDialogDescription>
                        The channel gets no more messages, and ones not sent yet stop. The webhook stays in Discord until
                        you delete it there.
                      </AlertDialogDescription>
                    </AlertDialogHeader>
                    <AlertDialogFooter>
                      <AlertDialogCancel className={touchTargetButtonClassName}>Keep it</AlertDialogCancel>
                      <AlertDialogAction
                        id="settings_confirm_remove_discord_webhook_button"
                        variant="destructive"
                        className={touchTargetButtonClassName}
                        onClick={() => void removeDiscordWebhook()}
                      >
                        Remove
                      </AlertDialogAction>
                    </AlertDialogFooter>
                  </AlertDialogContent>
                </AlertDialog>
              ) : null}
            </div>
          </div>
        )}
      </ResponsiveSheet>
    </>
  )
}

function OperatorAlertRepeatField({
  repeatHours,
  onSaved,
  onActionFailure,
}: {
  repeatHours: number
  onSaved(updatedDiscordWebhook: AccountDiscordWebhook): void
  onActionFailure(actionFailure: unknown): Promise<void>
}) {
  const [isSaving, setIsSaving] = useState(false)
  const repeatHourChoices = operatorAlertRepeatHourChoices.includes(repeatHours)
    ? operatorAlertRepeatHourChoices
    : [...operatorAlertRepeatHourChoices, repeatHours].sort((left, right) => left - right)

  async function saveRepeatHours(nextRepeatHours: number) {
    setIsSaving(true)
    try {
      const updatedDiscordWebhook = await fetchApiJson<AccountDiscordWebhook>("/api/v1/account-discord-webhook/operator-alert-repeat", {
        method: "PUT",
        body: { repeat_hours: nextRepeatHours },
      })
      onSaved(updatedDiscordWebhook)
      toast.success(nextRepeatHours === 0 ? "Alert reminders off." : `Alert reminders ${describeOperatorAlertRepeatHours(nextRepeatHours).toLowerCase()}.`)
    } catch (saveFailure) {
      await onActionFailure(saveFailure)
    } finally {
      setIsSaving(false)
    }
  }

  return (
    <Field>
      <FieldLabel htmlFor="settings_operator_alert_repeat_select">Alert reminders</FieldLabel>
      <Select value={String(repeatHours)} disabled={isSaving} onValueChange={(nextValue) => void saveRepeatHours(Number(nextValue))}>
        <SelectTrigger id="settings_operator_alert_repeat_select" className="h-12! w-full rounded-xl text-base">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {repeatHourChoices.map((repeatHourChoice) => (
            <SelectItem key={repeatHourChoice} value={String(repeatHourChoice)} className="min-h-11">
              {describeOperatorAlertRepeatHours(repeatHourChoice)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <p className="px-1 text-sm text-muted-foreground">
        How often a problem that needs you is sent here again while it lasts. Activity shows each one either way.
      </p>
    </Field>
  )
}

function DiscordWebhookUrlForm({
  onPasskeyPromptChange,
  onBack,
  onSaved,
}: {
  onPasskeyPromptChange(passkeyPromptIsOpen: boolean): void
  onBack(): void
  onSaved(updatedDiscordWebhook: AccountDiscordWebhook): void
}) {
  const { refreshAccountSession } = useAccountSession()
  // The URL holds the webhook's token: it lives only in this form, gone once the form unmounts.
  const [typedUrl, setTypedUrl] = useState("")
  const [urlError, setUrlError] = useState("")
  const [saveError, setSaveError] = useState("")
  const [passkeyPromptIsOpen, setPasskeyPromptIsOpen] = useState(false)

  function changePasskeyPromptState(nextPasskeyPromptIsOpen: boolean) {
    setPasskeyPromptIsOpen(nextPasskeyPromptIsOpen)
    onPasskeyPromptChange(nextPasskeyPromptIsOpen)
  }

  async function confirmWithPasskey(submitEvent: FormEvent) {
    submitEvent.preventDefault()
    setSaveError("")
    const trimmedUrl = typedUrl.trim()
    const nextUrlError = checkDiscordWebhookUrl(trimmedUrl)
    setUrlError(nextUrlError)
    if (nextUrlError) return
    changePasskeyPromptState(true)
    try {
      const updatedDiscordWebhook = await setAccountDiscordWebhookWithPasskey(trimmedUrl)
      changePasskeyPromptState(false)
      onSaved(updatedDiscordWebhook)
    } catch (saveFailure) {
      changePasskeyPromptState(false)
      if (saveFailure instanceof ApiError && saveFailure.httpStatus === 401) {
        await refreshAccountSession()
        return
      }
      if (saveFailure instanceof ApiError && saveFailure.message.startsWith("url:")) setUrlError(describeError(saveFailure))
      else setSaveError(describePasskeyError(saveFailure))
    }
  }

  return (
    <form onSubmit={(submitEvent) => void confirmWithPasskey(submitEvent)} noValidate className="flex flex-col">
      <Field data-invalid={Boolean(urlError) || undefined}>
        <FieldLabel htmlFor="settings_discord_webhook_url_input">Webhook URL</FieldLabel>
        <Input
          id="settings_discord_webhook_url_input"
          type="url"
          inputMode="url"
          maxLength={2048}
          autoComplete="off"
          spellCheck={false}
          autoCapitalize="off"
          placeholder="e.g. https://discord.com/api/webhooks/…"
          value={typedUrl}
          aria-invalid={Boolean(urlError)}
          onChange={(changeEvent) => {
            setTypedUrl(changeEvent.target.value)
            setUrlError("")
            setSaveError("")
          }}
          className="h-12 rounded-xl px-3.5 font-mono text-base"
        />
        <FieldError id="settings_discord_webhook_url_error">{urlError}</FieldError>
      </Field>
      {saveError ? (
        <Alert id="settings_discord_webhook_change_message" variant="destructive" className="mt-4">
          <AlertDescription>{saveError}</AlertDescription>
        </Alert>
      ) : null}
      <Button
        id="settings_confirm_discord_webhook_button"
        type="submit"
        className={cn(primaryActionButtonClassName, "mt-6 w-full")}
        disabled={passkeyPromptIsOpen}
      >
        {passkeyPromptIsOpen ? <Spinner /> : <Fingerprint />}
        {passkeyPromptIsOpen ? "Waiting for your passkey…" : "Confirm with passkey"}
      </Button>
      <Button type="button" variant="ghost" className={cn(touchTargetButtonClassName, "mt-2 w-full")} disabled={passkeyPromptIsOpen} onClick={onBack}>
        <ArrowLeft /> Back
      </Button>
    </form>
  )
}
