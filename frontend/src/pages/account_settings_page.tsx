// `/settings`, the signed-in account's settings (PLAN.md sections 7 and 10) as rows that each show a short value and
// open their own sheet (2026-10-14), in groups by what the account is doing: "Getting paid" (the payout address,
// changed only behind a fresh passkey check, and "Coins and networks", one section per network with its coins, its
// invoice defaults and, on a Monero network, its wallet and confirmations: account_chain_settings_sheet.tsx), "Payer
// page" (the support contact), "Security" (the passkeys, account_passkey_settings.tsx), "Connect" (the Discord channel
// webhook, the webhook for the account's own server and its API tokens, which sit with the webhook they serve) and
// "Records" (the CSV export). Each sheet saves its own fields (`PATCH /api/v1/accounts/current`; making a link saves
// its coins too) or acts at once.
import { Fingerprint } from "lucide-react"
import { useState, type FormEvent } from "react"
import { toast } from "sonner"

import { AccountApiTokenSettings } from "@/components/account_api_token_settings"
import { AccountChainSettingsSheet } from "@/components/account_chain_settings_sheet"
import { AccountInvoiceExportSettings } from "@/components/account_invoice_export_settings"
import { AccountDiscordWebhookSettings } from "@/components/account_discord_webhook_settings"
import { AccountPasskeySettings } from "@/components/account_passkey_settings"
import { AccountSettingsRow } from "@/components/account_settings_row"
import { AccountWebhookSettings } from "@/components/account_webhook_settings"
import { PageColumn } from "@/components/page_column"
import { ReceiverAddressField } from "@/components/receiver_address_field"
import { ResponsiveSheet } from "@/components/responsive_sheet"
import { SignInGate } from "@/components/sign_in_gate"
import { SiteTopBar } from "@/components/site_top_bar"
import { TapToCopyButton } from "@/components/tap_to_copy_button"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Field, FieldError, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { ItemGroup } from "@/components/ui/item"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { useAccountSession } from "@/hooks/use_account_session"
import { addressPattern, shortenHexString } from "@/lib/amount_formatting"
import { saveAccountSettings } from "@/lib/account_settings_requests"
import { ApiError, describeError } from "@/lib/api_client"
import type { AccountSession } from "@/lib/api_types"
import { changePayoutAddressWithPasskey, describePasskeyError, passkeysAreSupported } from "@/lib/passkey_access"
import { primaryActionButtonClassName } from "@/lib/button_size_class_names"
import { cn } from "@/lib/utils"

const supportContactMaximumLength = 200
const telegramHandlePattern = /^@[A-Za-z0-9_]{5,32}$/
// HTML's valid email address with a dotted domain, as the API checks it.
const emailAddressPattern =
  /^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$/
const supportContactHint = "An email, a Telegram @name (5 to 32 letters, digits or _) or an https:// link."

// Mirrors the API's check so a typo shows under the field before saving; the API stays the judge.
function describeSupportContactProblem(supportContactText: string): string {
  const trimmedText = supportContactText.trim()
  if (!trimmedText) return ""
  if (trimmedText.length > supportContactMaximumLength) return `At most ${supportContactMaximumLength} characters.`
  if (telegramHandlePattern.test(trimmedText) || emailAddressPattern.test(trimmedText)) return ""
  if (trimmedText.startsWith("https://") && !/\s/.test(trimmedText)) {
    try {
      const parsedUrl = new URL(trimmedText)
      if (parsedUrl.hostname.includes(".") && !parsedUrl.username && !parsedUrl.password) return ""
    } catch {
      // Falls through to the hint.
    }
  }
  if (trimmedText.startsWith("@")) return "A Telegram name is @ and then 5 to 32 letters, digits or _."
  if (trimmedText.startsWith("http://")) return "Use an https:// link."
  return supportContactHint
}

const shownCoinSymbolCount = 3

// One group's short heading, in the page's muted style.
function SettingsGroupHeading({ children }: { children: string }) {
  return <h2 className="mt-6 mb-2 px-1 text-sm font-medium text-muted-foreground first-of-type:mt-0">{children}</h2>
}

// Which sheet a row opened.
type AccountSettingsSheet = "payout_address" | "accepted_coins" | "support_contact" | null

export function AccountSettingsPage() {
  const { accountSession, sessionIsLoading } = useAccountSession()
  if (!accountSession) {
    return (
      <PageColumn topBar={<SiteTopBar />}>
        {sessionIsLoading ? <Skeleton className="mt-12 h-10 w-56" /> : <SignInGate title="Settings" />}
      </PageColumn>
    )
  }
  return <SignedInAccountSettings accountSession={accountSession} />
}

function SignedInAccountSettings({ accountSession }: { accountSession: AccountSession }) {
  const [openedSheet, setOpenedSheet] = useState<AccountSettingsSheet>(null)
  const closeSheet = () => setOpenedSheet(null)
  // Every enabled chain's coins, once each (the session holds them saved, else the chain's defaults): the first three
  // and how many more, so the row stays one short line on a phone.
  const acceptedCoinSymbols = [...new Set(Object.values(accountSession.accepted_assets_by_chain).flat())]
  const acceptedCoinsText =
    acceptedCoinSymbols.length > shownCoinSymbolCount
      ? `${acceptedCoinSymbols.slice(0, shownCoinSymbolCount).join(", ")} +${acceptedCoinSymbols.length - shownCoinSymbolCount}`
      : acceptedCoinSymbols.join(", ")

  return (
    <PageColumn topBar={<SiteTopBar />}>
      <div className="mt-4 flex flex-col">
        <h1 className="mb-4 text-3xl font-semibold tracking-tight">Settings</h1>
        <SettingsGroupHeading>Getting paid</SettingsGroupHeading>
        <ItemGroup id="settings_getting_paid_rows" className="gap-0 rounded-2xl border">
          <AccountSettingsRow
            id="settings_payout_address_row"
            title="Payout"
            valueText={shortenHexString(accountSession.payout_address)}
            valueIsMonospace
            onOpen={() => setOpenedSheet("payout_address")}
          />
          <AccountSettingsRow id="settings_accepted_coins_row" title="Coins and networks" valueText={acceptedCoinsText} onOpen={() => setOpenedSheet("accepted_coins")} />
        </ItemGroup>
        <SettingsGroupHeading>Payer page</SettingsGroupHeading>
        <ItemGroup id="settings_payer_page_rows" className="gap-0 rounded-2xl border">
          <AccountSettingsRow
            id="settings_support_contact_row"
            title="Support contact"
            valueText={accountSession.support_contact ?? "Off"}
            onOpen={() => setOpenedSheet("support_contact")}
          />
        </ItemGroup>
        <SettingsGroupHeading>Security</SettingsGroupHeading>
        <ItemGroup id="settings_security_rows" className="gap-0 rounded-2xl border">
          <AccountPasskeySettings />
        </ItemGroup>
        <SettingsGroupHeading>Connect</SettingsGroupHeading>
        <ItemGroup id="settings_connect_rows" className="gap-0 rounded-2xl border">
          <AccountDiscordWebhookSettings />
          <AccountWebhookSettings />
          <AccountApiTokenSettings />
        </ItemGroup>
        <SettingsGroupHeading>Records</SettingsGroupHeading>
        <ItemGroup id="settings_records_rows" className="gap-0 rounded-2xl border">
          <AccountInvoiceExportSettings />
        </ItemGroup>
      </div>
      <PayoutAddressSheet currentPayoutAddress={accountSession.payout_address} isOpen={openedSheet === "payout_address"} onClose={closeSheet} />
      <AccountChainSettingsSheet accountSession={accountSession} isOpen={openedSheet === "accepted_coins"} onClose={closeSheet} />
      <SupportContactSheet accountSession={accountSession} isOpen={openedSheet === "support_contact"} onClose={closeSheet} />
    </PageColumn>
  )
}

// The address now (tap to copy), then the new one and a fresh passkey check of this account: the API asks for one of
// its passkeys with user verification and changes nothing until it verifies.
function PayoutAddressSheet({ currentPayoutAddress, isOpen, onClose }: { currentPayoutAddress: string; isOpen: boolean; onClose(): void }) {
  const { setAccountSession, refreshAccountSession } = useAccountSession()
  const [newPayoutAddress, setNewPayoutAddress] = useState("")
  const [newPayoutAddressError, setNewPayoutAddressError] = useState("")
  const [changeError, setChangeError] = useState("")
  const [passkeyPromptIsOpen, setPasskeyPromptIsOpen] = useState(false)

  function changeOpenState(nextIsOpen: boolean) {
    if (passkeyPromptIsOpen || nextIsOpen) return
    setNewPayoutAddress("")
    setNewPayoutAddressError("")
    setChangeError("")
    onClose()
  }

  async function confirmWithPasskey(submitEvent: FormEvent) {
    submitEvent.preventDefault()
    setChangeError("")
    let nextAddressError = ""
    if (!newPayoutAddress) nextAddressError = "Paste the new address."
    else if (!addressPattern.test(newPayoutAddress)) nextAddressError = "That's not a full address: 0x and then 40 letters and digits (0-9, a-f)."
    else if (newPayoutAddress.toLowerCase() === currentPayoutAddress.toLowerCase()) nextAddressError = "That's already your payout address."
    setNewPayoutAddressError(nextAddressError)
    if (nextAddressError) return
    setPasskeyPromptIsOpen(true)
    try {
      const updatedSession = await changePayoutAddressWithPasskey(newPayoutAddress)
      setAccountSession(updatedSession)
      toast.success(`Payout address changed to ${shortenHexString(updatedSession.payout_address)}.`)
      setPasskeyPromptIsOpen(false)
      setNewPayoutAddress("")
      onClose()
    } catch (changeFailure) {
      setPasskeyPromptIsOpen(false)
      if (changeFailure instanceof ApiError && changeFailure.httpStatus === 401) {
        await refreshAccountSession()
        return
      }
      setChangeError(describePasskeyError(changeFailure))
    }
  }

  return (
    <ResponsiveSheet
      isOpen={isOpen}
      onOpenChange={changeOpenState}
      title="Payout address"
      description={
        <>
          New links pay here. Links you already made keep paying <span className="font-mono">{shortenHexString(currentPayoutAddress)}</span>.
        </>
      }
      testId="settings_payout_address_sheet"
    >
      <form onSubmit={(submitEvent) => void confirmWithPasskey(submitEvent)} noValidate className="flex flex-col">
        <TapToCopyButton
          id="settings_payout_address"
          textToCopy={currentPayoutAddress}
          accessibleLabel="Copy your payout address"
          className="mb-5 rounded-xl border px-3.5 py-3 text-left font-mono text-[15px] break-all"
        >
          {currentPayoutAddress}
        </TapToCopyButton>
        <ReceiverAddressField
          inputId="settings_new_payout_address_input"
          errorId="settings_new_payout_address_error"
          pasteButtonId="settings_paste_payout_address_button"
          label="New payout address"
          placeholder="0x… your wallet address"
          addressText={newPayoutAddress}
          errorText={newPayoutAddressError}
          onAddressTextChange={(nextAddressText) => {
            setNewPayoutAddress(nextAddressText)
            setChangeError("")
          }}
          onErrorTextChange={setNewPayoutAddressError}
        />
        {changeError ? (
          <Alert id="settings_payout_address_change_message" variant="destructive" className="mt-4">
            <AlertDescription>{changeError}</AlertDescription>
          </Alert>
        ) : null}
        <Button
          id="settings_confirm_payout_address_button"
          type="submit"
          className={cn(primaryActionButtonClassName, "mt-6 w-full")}
          disabled={passkeyPromptIsOpen || !passkeysAreSupported()}
        >
          {passkeyPromptIsOpen ? <Spinner /> : <Fingerprint />}
          {passkeyPromptIsOpen ? "Waiting for your passkey…" : "Confirm with passkey"}
        </Button>
      </form>
    </ResponsiveSheet>
  )
}

// The contact payers see as "Need help? Contact the seller" on this account's invoices; empty turns it off. "Save" sends
// only `support_contact`.
function SupportContactSheet({ accountSession, isOpen, onClose }: { accountSession: AccountSession; isOpen: boolean; onClose(): void }) {
  const { setAccountSession, refreshAccountSession } = useAccountSession()
  // null until typed: the saved contact.
  const [typedSupportContact, setTypedSupportContact] = useState<string | null>(null)
  const [supportContactError, setSupportContactError] = useState("")
  const [isSaving, setIsSaving] = useState(false)
  const [saveError, setSaveError] = useState("")
  const supportContactText = typedSupportContact ?? accountSession.support_contact ?? ""
  const supportContactChanged = typedSupportContact !== null && typedSupportContact.trim() !== (accountSession.support_contact ?? "")

  function changeOpenState(nextIsOpen: boolean) {
    if (isSaving || nextIsOpen) return
    setTypedSupportContact(null)
    setSupportContactError("")
    setSaveError("")
    onClose()
  }

  async function saveSupportContact(submitEvent: FormEvent) {
    submitEvent.preventDefault()
    if (!supportContactChanged) return
    setSaveError("")
    const nextSupportContactError = describeSupportContactProblem(supportContactText)
    setSupportContactError(nextSupportContactError)
    if (nextSupportContactError) return
    setIsSaving(true)
    try {
      setAccountSession(await saveAccountSettings({ support_contact: supportContactText.trim() }, refreshAccountSession))
      toast.success(supportContactText.trim() ? "Support contact saved." : "Support contact removed.")
      setTypedSupportContact(null)
      onClose()
    } catch (saveFailure) {
      if (saveFailure instanceof ApiError && saveFailure.message.startsWith("support_contact:")) setSupportContactError(supportContactHint)
      else setSaveError(describeError(saveFailure))
    } finally {
      setIsSaving(false)
    }
  }

  return (
    <ResponsiveSheet
      isOpen={isOpen}
      onOpenChange={changeOpenState}
      title="Support contact"
      description="Payers see “Need help? Contact the seller” linking here. Leave it empty to show none."
      testId="settings_support_contact_sheet"
    >
      <form onSubmit={(submitEvent) => void saveSupportContact(submitEvent)} noValidate className="flex flex-col">
        <Field data-invalid={Boolean(supportContactError) || undefined}>
          <FieldLabel htmlFor="settings_support_contact_input">Email, Telegram @name or https:// link</FieldLabel>
          <Input
            id="settings_support_contact_input"
            maxLength={supportContactMaximumLength}
            autoComplete="off"
            spellCheck={false}
            autoCapitalize="off"
            placeholder="e.g. help@yourshop.com or @yourshop"
            value={supportContactText}
            aria-invalid={Boolean(supportContactError)}
            aria-describedby={supportContactError ? "settings_support_contact_error" : undefined}
            onChange={(changeEvent) => {
              setTypedSupportContact(changeEvent.target.value)
              setSupportContactError("")
              setSaveError("")
            }}
            className="h-12 rounded-xl px-3.5 text-base"
          />
          <FieldError id="settings_support_contact_error">{supportContactError}</FieldError>
        </Field>
        {saveError ? (
          <Alert id="settings_support_contact_message" variant="destructive" className="mt-4">
            <AlertDescription>{saveError}</AlertDescription>
          </Alert>
        ) : null}
        <Button
          id="settings_save_support_contact_button"
          type="submit"
          className={cn(primaryActionButtonClassName, "mt-6 w-full")}
          disabled={isSaving || !supportContactChanged}
        >
          {isSaving ? <Spinner /> : null}
          {isSaving ? "Saving…" : "Save"}
        </Button>
      </form>
    </ResponsiveSheet>
  )
}
