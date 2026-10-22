// `/settings`, the signed-in account's settings (PLAN.md sections 7 and 10) as rows that each show their value and open
// their own sheet (2026-10-14): the payout address, changed only behind a fresh passkey check; the
// coins new links start with ticked and the support contact payers see, each sheet saving its own field
// (`PATCH /api/v1/accounts/current` with just that field; making a link saves its coins too); the confirmations an XMR
// payment counts final at, per Monero network; the passkeys
// (account_passkey_settings.tsx); the Monero wallet XMR links pay, while a Monero chain is enabled
// (account_monero_wallet_settings.tsx). Then "Connect": the Discord channel webhook (2026-10-13), the webhook for the
// account's own server and its API tokens (2026-10-12), which act at once in their sheets too.
import { Fingerprint } from "lucide-react"
import { useState, type FormEvent } from "react"
import { toast } from "sonner"

import { AcceptedCoinPicker } from "@/components/accepted_coin_picker"
import { AccountApiTokenSettings } from "@/components/account_api_token_settings"
import { AccountInvoiceExportSettings } from "@/components/account_invoice_export_settings"
import { AccountDiscordWebhookSettings } from "@/components/account_discord_webhook_settings"
import { AccountMoneroWalletSettings } from "@/components/account_monero_wallet_settings"
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
import { Field, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { ItemGroup } from "@/components/ui/item"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { useAccountSession } from "@/hooks/use_account_session"
import { listChainsForMaker, useChainList } from "@/hooks/use_chain_list"
import { addressPattern, shortenHexString } from "@/lib/amount_formatting"
import { ApiError, describeError, fetchApiJson } from "@/lib/api_client"
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

// Which sheet a row opened.
type AccountSettingsSheet = "payout_address" | "accepted_coins" | "confirmations" | "support_contact" | null

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
  // Every enabled chain's coins, once each: the session holds them saved, else the chain's defaults.
  const acceptedCoinsText = [...new Set(Object.values(accountSession.accepted_assets_by_chain).flat())].join(", ")

  return (
    <PageColumn topBar={<SiteTopBar />}>
      <div className="mt-4 flex flex-col">
        <h1 className="mb-4 text-3xl font-semibold tracking-tight">Settings</h1>
        <ItemGroup id="settings_account_rows" className="gap-0 rounded-2xl border">
          <AccountSettingsRow
            id="settings_payout_address_row"
            title="Payout"
            valueText={shortenHexString(accountSession.payout_address)}
            valueIsMonospace
            onOpen={() => setOpenedSheet("payout_address")}
          />
          <AccountMoneroWalletSettings />
          <AccountSettingsRow id="settings_accepted_coins_row" title="Coins" valueText={acceptedCoinsText} onOpen={() => setOpenedSheet("accepted_coins")} />
          <ConfirmationsSettingsRow accountSession={accountSession} onOpen={() => setOpenedSheet("confirmations")} />
          <AccountSettingsRow
            id="settings_support_contact_row"
            title="Support contact"
            valueText={accountSession.support_contact ?? "Off"}
            onOpen={() => setOpenedSheet("support_contact")}
          />
          <AccountPasskeySettings />
        </ItemGroup>
        <h2 className="mt-6 mb-2 px-1 text-sm font-medium text-muted-foreground">Connect</h2>
        <ItemGroup id="settings_connect_rows" className="gap-0 rounded-2xl border">
          <AccountDiscordWebhookSettings />
          <AccountWebhookSettings />
          <AccountApiTokenSettings />
        </ItemGroup>
        <h2 className="mt-6 mb-2 px-1 text-sm font-medium text-muted-foreground">Records</h2>
        <ItemGroup id="settings_records_rows" className="gap-0 rounded-2xl border">
          <AccountInvoiceExportSettings />
        </ItemGroup>
      </div>
      <PayoutAddressSheet currentPayoutAddress={accountSession.payout_address} isOpen={openedSheet === "payout_address"} onClose={closeSheet} />
      <AcceptedCoinsSheet accountSession={accountSession} isOpen={openedSheet === "accepted_coins"} onClose={closeSheet} />
      <ConfirmationsSheet accountSession={accountSession} isOpen={openedSheet === "confirmations"} onClose={closeSheet} />
      <SupportContactSheet accountSession={accountSession} isOpen={openedSheet === "support_contact"} onClose={closeSheet} />
    </PageColumn>
  )
}

// One field of `PATCH /api/v1/accounts/current`: the new session on success; a 401 refreshes the session (the page
// then shows the sign-in gate) and still throws, so the sheet stays as it was. A confirmations field of `null` clears
// that chain's saved number back to the chain default.
type AccountSettingsPatchBody = Partial<{
  accepted_assets_by_chain: Record<string, string[]>
  confirmations_final_by_chain: Record<string, number | null>
  support_contact: string | null
}>

async function saveAccountField(
  fieldBody: AccountSettingsPatchBody,
  refreshAccountSession: () => Promise<unknown>,
): Promise<AccountSession> {
  try {
    return await fetchApiJson<AccountSession>("/api/v1/accounts/current", { method: "PATCH", body: fieldBody })
  } catch (saveFailure) {
    if (saveFailure instanceof ApiError && saveFailure.httpStatus === 401) await refreshAccountSession()
    throw saveFailure
  }
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

// The coins new links start with ticked, one picker per chain that takes invoices; "Save" sends only `accepted_assets_by_chain`.
function AcceptedCoinsSheet({ accountSession, isOpen, onClose }: { accountSession: AccountSession; isOpen: boolean; onClose(): void }) {
  const { setAccountSession, refreshAccountSession } = useAccountSession()
  const { chains: listedChains, chainLoadError } = useChainList()
  const chains = listChainsForMaker(listedChains, accountSession)
  // null until a coin is tapped: the saved coins.
  const [pickedAcceptedSymbolsByChain, setPickedAcceptedSymbolsByChain] = useState<Record<string, string[]> | null>(null)
  const [isSaving, setIsSaving] = useState(false)
  const [saveError, setSaveError] = useState("")
  const acceptedSymbolsByChain = pickedAcceptedSymbolsByChain ?? accountSession.accepted_assets_by_chain

  function changeOpenState(nextIsOpen: boolean) {
    if (isSaving || nextIsOpen) return
    setPickedAcceptedSymbolsByChain(null)
    setSaveError("")
    onClose()
  }

  async function saveAcceptedCoins(submitEvent: FormEvent) {
    submitEvent.preventDefault()
    if (pickedAcceptedSymbolsByChain === null) return
    setSaveError("")
    setIsSaving(true)
    try {
      setAccountSession(await saveAccountField({ accepted_assets_by_chain: pickedAcceptedSymbolsByChain }, refreshAccountSession))
      toast.success("Coins saved.")
      setPickedAcceptedSymbolsByChain(null)
      onClose()
    } catch (saveFailure) {
      setSaveError(describeError(saveFailure))
    } finally {
      setIsSaving(false)
    }
  }

  return (
    <ResponsiveSheet isOpen={isOpen} onOpenChange={changeOpenState} title="Coins" testId="settings_accepted_coins_sheet">
      <form onSubmit={(submitEvent) => void saveAcceptedCoins(submitEvent)} noValidate className="flex flex-col">
        <FieldGroup className="gap-6">
          {chains ? (
            chains.map((chain) => (
              <Field key={chain.key}>
                {chains.length > 1 ? <FieldLabel>{chain.name}</FieldLabel> : null}
                <AcceptedCoinPicker
                  chainAssets={chain.assets}
                  acceptedSymbols={acceptedSymbolsByChain[chain.key] ?? chain.default_accepted_assets}
                  onAcceptedSymbolsChange={(nextSymbols) => setPickedAcceptedSymbolsByChain({ ...acceptedSymbolsByChain, [chain.key]: nextSymbols })}
                />
              </Field>
            ))
          ) : chainLoadError ? null : (
            <Skeleton className="h-11 w-full rounded-full" />
          )}
        </FieldGroup>
        {saveError || chainLoadError ? (
          <Alert id="settings_accepted_coins_message" variant="destructive" className="mt-4">
            <AlertDescription>{saveError || `Can't load the coins: ${chainLoadError}`}</AlertDescription>
          </Alert>
        ) : null}
        <Button
          id="settings_save_accepted_coins_button"
          type="submit"
          className={cn(primaryActionButtonClassName, "mt-6 w-full")}
          disabled={isSaving || !chains || pickedAcceptedSymbolsByChain === null}
        >
          {isSaving ? <Spinner /> : null}
          {isSaving ? "Saving…" : "Save"}
        </Button>
      </form>
    </ResponsiveSheet>
  )
}

// The confirmations an XMR invoice counts final at: this account's saved number per Monero chain, else "Default".
function ConfirmationsSettingsRow({ accountSession, onOpen }: { accountSession: AccountSession; onOpen(): void }) {
  const savedEntries = Object.entries(accountSession.confirmations_final_by_chain)
  const valueText = savedEntries.length > 0
    ? savedEntries.map(([chainKey, confirmationsFinal]) => `${chainKey}: ${confirmationsFinal}`).join(", ")
    : "Default"
  return <AccountSettingsRow id="settings_confirmations_row" title="Confirmations" valueText={valueText} onOpen={onOpen} />
}

// One Monero chain's confirmations-before-final: a number from 1 to 100, empty standing for the
// chain's default. "Save" sends every Monero chain, `null` where the field is empty (the API clears that chain's
// saved number back to the default). Invoices already made keep the count they were made with.
function ConfirmationsSheet({ accountSession, isOpen, onClose }: { accountSession: AccountSession; isOpen: boolean; onClose(): void }) {
  const { setAccountSession, refreshAccountSession } = useAccountSession()
  const { chains, chainLoadError } = useChainList()
  const moneroChains = (chains ?? []).filter((chain) => chain.kind === "monero")
  // null until typed: what each field shows, "" meaning the chain's default.
  const [typedCounts, setTypedCounts] = useState<Record<string, string> | null>(null)
  const [isSaving, setIsSaving] = useState(false)
  const [saveError, setSaveError] = useState("")
  const countTextByChain = typedCounts ?? Object.fromEntries(
    moneroChains.map((chain) => [chain.key, accountSession.confirmations_final_by_chain[chain.key] != null
      ? String(accountSession.confirmations_final_by_chain[chain.key])
      : ""]),
  )
  function describeCountProblem(countText: string): string {
    if (!countText) return ""
    const count = Number(countText)
    if (!Number.isInteger(count) || count < 1 || count > 100) return "A whole number from 1 to 100, or empty for the default."
    return ""
  }
  const countProblemByChain = Object.fromEntries(
    Object.entries(countTextByChain).map(([chainKey, countText]) => [chainKey, describeCountProblem(countText)]),
  )
  const countsChanged = typedCounts !== null && moneroChains.some((chain) => {
    const savedCount = accountSession.confirmations_final_by_chain[chain.key]
    return (countTextByChain[chain.key] ?? "") !== (savedCount != null ? String(savedCount) : "")
  })

  function changeOpenState(nextIsOpen: boolean) {
    if (isSaving || nextIsOpen) return
    setTypedCounts(null)
    setSaveError("")
    onClose()
  }

  async function saveConfirmations(submitEvent: FormEvent) {
    submitEvent.preventDefault()
    if (!countsChanged) return
    setSaveError("")
    setIsSaving(true)
    try {
      setAccountSession(await saveAccountField(
        {
          confirmations_final_by_chain: Object.fromEntries(
            moneroChains.map((chain) => [chain.key, countTextByChain[chain.key] === "" ? null : Number(countTextByChain[chain.key])]),
          ),
        },
        refreshAccountSession,
      ))
      toast.success("Confirmations saved.")
      setTypedCounts(null)
      onClose()
    } catch (saveFailure) {
      setSaveError(describeError(saveFailure))
    } finally {
      setIsSaving(false)
    }
  }

  return (
    <ResponsiveSheet
      isOpen={isOpen}
      onOpenChange={changeOpenState}
      title="Confirmations"
      description="How many blocks confirm an XMR payment before it counts. Fewer is faster but takes on the reorg risk; leave a field empty for the network's default."
      testId="settings_confirmations_sheet"
    >
      <form onSubmit={(submitEvent) => void saveConfirmations(submitEvent)} noValidate className="flex flex-col">
        <FieldGroup className="gap-6">
          {moneroChains.length > 0 ? (
            moneroChains.map((chain) => (
              <Field key={chain.key} data-invalid={Boolean(countProblemByChain[chain.key]) || undefined}>
                <FieldLabel htmlFor={`settings_confirmations_input_${chain.key}`}>{chain.name} (XMR)</FieldLabel>
                <Input
                  id={`settings_confirmations_input_${chain.key}`}
                  type="number"
                  inputMode="numeric"
                  min={1}
                  max={100}
                  autoComplete="off"
                  placeholder={`Default (${chain.confirmations_final ?? "…"})`}
                  value={countTextByChain[chain.key] ?? ""}
                  aria-invalid={Boolean(countProblemByChain[chain.key])}
                  aria-describedby={countProblemByChain[chain.key] ? `settings_confirmations_error_${chain.key}` : undefined}
                  onChange={(changeEvent) => {
                    setTypedCounts({ ...countTextByChain, [chain.key]: changeEvent.target.value })
                    setSaveError("")
                  }}
                  className="h-12 rounded-xl px-3.5 text-base"
                />
                <FieldError id={`settings_confirmations_error_${chain.key}`}>{countProblemByChain[chain.key]}</FieldError>
              </Field>
            ))
          ) : chainLoadError ? null : (
            <Skeleton className="h-11 w-full rounded-full" />
          )}
          {moneroChains.length === 0 && chains !== null ? (
            <p className="text-sm text-muted-foreground">No network takes XMR yet. Set a Monero wallet first.</p>
          ) : null}
        </FieldGroup>
        {saveError || chainLoadError ? (
          <Alert id="settings_confirmations_message" variant="destructive" className="mt-4">
            <AlertDescription>{saveError || `Can't load the networks: ${chainLoadError}`}</AlertDescription>
          </Alert>
        ) : null}
        <Button
          id="settings_save_confirmations_button"
          type="submit"
          className={cn(primaryActionButtonClassName, "mt-6 w-full")}
          disabled={isSaving || !countsChanged || Object.values(countProblemByChain).some(Boolean)}
        >
          {isSaving ? <Spinner /> : null}
          {isSaving ? "Saving…" : "Save"}
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
      setAccountSession(await saveAccountField({ support_contact: supportContactText.trim() }, refreshAccountSession))
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
