// `/settings`' "Monero wallet" row and its sheet (PLAN.md section 14, 2026-10-14), shown only while a Monero chain is
// enabled (`/chains` lists one; until then nothing renders and nothing is fetched): the receiver's own wallet, which XMR
// links pay at a fresh subaddress each. Setting or changing it asks for a fresh passkey check bound to that wallet, like
// the payout address. The private view key lives only in the form's state while it is open (a masked text field that
// no password manager offers to save) and goes to the API once; the API never sends it back. The sheet also says to
// use a dedicated wallet, that payments land in account 1, and how to widen the wallet's lookahead, with "I set it".
import { ArrowLeft, Fingerprint } from "lucide-react"
import { useCallback, useEffect, useState, type FormEvent } from "react"
import { toast } from "sonner"

import { AccountSettingsRow } from "@/components/account_settings_row"
import { ResponsiveSheet } from "@/components/responsive_sheet"
import { TapToCopyButton } from "@/components/tap_to_copy_button"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Field, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { useAccountSession } from "@/hooks/use_account_session"
import { useChainList } from "@/hooks/use_chain_list"
import { shortenMoneroAddress } from "@/lib/amount_formatting"
import { ApiError, describeError } from "@/lib/api_client"
import type { AccountMoneroWalletSettings, MoneroChainSummary } from "@/lib/api_types"
import { primaryActionButtonClassName, touchTargetButtonClassName } from "@/lib/button_size_class_names"
import {
  confirmMoneroLookahead,
  describeMoneroAddressProblem,
  describePrivateViewKeyProblem,
  moneroLookaheadCommand,
  readMoneroWalletSettings,
  widenedMoneroLookaheadMinorCount,
} from "@/lib/monero_wallet_settings"
import { describePasskeyError, passkeysAreSupported, setAccountMoneroWalletWithPasskey } from "@/lib/passkey_access"
import { cn } from "@/lib/utils"

// Asks for the wallet only once `/chains` lists a Monero chain: a Polygon-only deployment shows no row and makes no
// request, and a failed `/chains` read shows no row either (the page's other rows say when the API is down).
export function AccountMoneroWalletSettings() {
  const { chains } = useChainList()
  const moneroChainIsEnabled = chains?.some((chain) => chain.kind === "monero") ?? false
  return moneroChainIsEnabled ? <EnabledMoneroWalletSettings /> : null
}

function EnabledMoneroWalletSettings() {
  const { refreshAccountSession } = useAccountSession()
  const [walletSettings, setWalletSettings] = useState<AccountMoneroWalletSettings | null>(null)
  const [pickedChainKey, setPickedChainKey] = useState<string | null>(null)
  const [loadError, setLoadError] = useState("")
  const [sheetIsOpen, setSheetIsOpen] = useState(false)
  const [showsWalletForm, setShowsWalletForm] = useState(false)
  const [passkeyPromptIsOpen, setPasskeyPromptIsOpen] = useState(false)
  const [isConfirmingLookahead, setIsConfirmingLookahead] = useState(false)

  // The state changes in the promise callbacks, once the API answers, never synchronously in the effect below.
  const reloadWalletSettings = useCallback(
    (chainKey?: string) =>
      readMoneroWalletSettings(chainKey).then(
        (loadedWalletSettings) => {
          setWalletSettings(loadedWalletSettings)
          setLoadError("")
        },
        (loadFailure: unknown) => setLoadError(describeError(loadFailure)),
      ),
    [],
  )

  useEffect(() => {
    void reloadWalletSettings()
  }, [reloadWalletSettings])

  // Nothing until the wallet read answers; a deployment that lists no Monero chain after all shows no row.
  if (!loadError && (walletSettings === null || walletSettings.monero_chains.length === 0)) return null

  const moneroChains = walletSettings?.monero_chains ?? []
  const wallet = walletSettings?.wallet ?? null
  const currentChain = moneroChains.find((moneroChain) => moneroChain.key === (pickedChainKey ?? wallet?.chain_key)) ?? moneroChains[0]
  const lookaheadIsWidened = (wallet?.confirmed_lookahead_minor_count ?? 0) >= widenedMoneroLookaheadMinorCount

  async function pickChain(chainKey: string) {
    setPickedChainKey(chainKey)
    await reloadWalletSettings(chainKey)
  }

  async function confirmWidenedLookahead() {
    if (!wallet) return
    setIsConfirmingLookahead(true)
    try {
      setWalletSettings(await confirmMoneroLookahead(wallet.chain_key))
      toast.success(`Saved: your wallet looks ${widenedMoneroLookaheadMinorCount} subaddresses ahead.`)
    } catch (confirmFailure) {
      if (confirmFailure instanceof ApiError && confirmFailure.httpStatus === 401) await refreshAccountSession()
      else toast.error(describeError(confirmFailure))
    } finally {
      setIsConfirmingLookahead(false)
    }
  }

  return (
    <>
      <AccountSettingsRow
        id="settings_monero_wallet_row"
        title="Monero wallet"
        valueText={loadError ? "Can't load" : wallet ? shortenMoneroAddress(wallet.primary_address) : "Off"}
        valueIsMonospace={Boolean(wallet) && !loadError}
        valueTone={loadError ? "problem" : "default"}
        onOpen={() => setSheetIsOpen(true)}
      />
      <ResponsiveSheet
        isOpen={sheetIsOpen}
        onOpenChange={(nextIsOpen) => {
          if (passkeyPromptIsOpen) return
          setSheetIsOpen(nextIsOpen)
          if (!nextIsOpen) setShowsWalletForm(false)
        }}
        title={showsWalletForm ? (wallet ? "Change Monero wallet" : "Set a Monero wallet") : "Monero wallet"}
        description={showsWalletForm ? "XMR links pay this wallet directly, each at a new subaddress. The view key lets this site see payments; it can't spend." : undefined}
        testId="settings_monero_wallet_sheet"
      >
        {moneroChains.length > 1 && currentChain ? (
          <ToggleGroup
            type="single"
            variant="outline"
            spacing={0}
            value={currentChain.key}
            disabled={passkeyPromptIsOpen}
            onValueChange={(chainKey) => {
              if (chainKey) void pickChain(chainKey)
            }}
            className="mb-5 grid w-full grid-cols-2"
            aria-label="Monero network"
          >
            {moneroChains.map((moneroChain) => (
              <ToggleGroupItem
                key={moneroChain.key}
                value={moneroChain.key}
                data-testid={`settings_monero_chain_${moneroChain.key}`}
                className="h-11 text-[15px] font-medium data-[state=on]:bg-foreground data-[state=on]:text-background"
              >
                {moneroChain.name}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        ) : null}
        {showsWalletForm && currentChain ? (
          <MoneroWalletForm
            key={currentChain.key}
            moneroChain={currentChain}
            currentPrimaryAddress={wallet?.chain_key === currentChain.key ? wallet.primary_address : null}
            onPasskeyPromptChange={setPasskeyPromptIsOpen}
            onBack={() => setShowsWalletForm(false)}
            onSaved={(updatedWalletSettings) => {
              setWalletSettings(updatedWalletSettings)
              setShowsWalletForm(false)
              toast.success("Monero wallet set. New XMR links pay it.")
            }}
          />
        ) : loadError ? (
          <Alert id="settings_monero_wallet_message" variant="destructive">
            <AlertDescription>Can't load your Monero wallet: {loadError}</AlertDescription>
          </Alert>
        ) : walletSettings === null ? (
          <Skeleton className="h-16 w-full rounded-2xl" />
        ) : (
          <div className="flex flex-col gap-3">
            {wallet ? (
              <TapToCopyButton
                id="settings_monero_wallet_address"
                textToCopy={wallet.primary_address}
                accessibleLabel="Copy your Monero wallet address"
                className="rounded-xl border px-3.5 py-3 text-left font-mono text-[15px]"
              >
                {shortenMoneroAddress(wallet.primary_address)}
              </TapToCopyButton>
            ) : (
              <p id="settings_monero_wallet_state" className="rounded-xl border px-3.5 py-3 text-[15px] text-muted-foreground">
                No Monero wallet yet
              </p>
            )}
            {wallet ? (
              <div id="settings_monero_lookahead" className="flex flex-col gap-2 rounded-xl border px-3.5 py-3 text-[15px]">
                {lookaheadIsWidened ? (
                  <p>Your wallet looks {wallet.confirmed_lookahead_minor_count} subaddresses ahead.</p>
                ) : (
                  <>
                    <p>So your wallet keeps seeing new links, run this in monero-wallet-cli:</p>
                    <MoneroLookaheadCommand />
                    <Button
                      id="settings_confirm_monero_lookahead_button"
                      type="button"
                      variant="secondary"
                      className={cn(touchTargetButtonClassName, "self-start")}
                      disabled={isConfirmingLookahead}
                      onClick={() => void confirmWidenedLookahead()}
                    >
                      {isConfirmingLookahead ? <Spinner /> : null}I set it
                    </Button>
                  </>
                )}
                {/* wallet2 applies a wider lookahead at once only from v0.18.4.1, and can miss two payments past the
                    lookahead in one refresh batch (PLAN.md section 14). */}
                <p id="settings_monero_wallet_version_note" className="text-sm text-muted-foreground">
                  Use Monero wallet v0.18.4.1 or newer. If a paid invoice doesn't show in your wallet, run{" "}
                  <code className="font-mono">rescan_bc</code> in it.
                </p>
              </div>
            ) : null}
            {passkeysAreSupported() ? (
              <Button
                id="settings_set_monero_wallet_button"
                type="button"
                variant="secondary"
                className={cn(touchTargetButtonClassName, "self-start")}
                onClick={() => setShowsWalletForm(true)}
              >
                {wallet ? "Change" : "Set"}
              </Button>
            ) : null}
          </div>
        )}
      </ResponsiveSheet>
    </>
  )
}

// The exact line, tap to copy.
export function MoneroLookaheadCommand({ id = "settings_monero_lookahead_command" }: { id?: string }) {
  return (
    <TapToCopyButton
      id={id}
      textToCopy={moneroLookaheadCommand}
      accessibleLabel="Copy the lookahead command"
      className="min-h-11 rounded-lg bg-muted px-3 py-2.5 text-left font-mono text-[15px]"
    >
      {moneroLookaheadCommand}
    </TapToCopyButton>
  )
}

function MoneroWalletForm({
  moneroChain,
  currentPrimaryAddress,
  onPasskeyPromptChange,
  onBack,
  onSaved,
}: {
  moneroChain: MoneroChainSummary
  currentPrimaryAddress: string | null
  onPasskeyPromptChange(passkeyPromptIsOpen: boolean): void
  onBack(): void
  onSaved(updatedWalletSettings: AccountMoneroWalletSettings): void
}) {
  const { refreshAccountSession } = useAccountSession()
  const [typedAddress, setTypedAddress] = useState("")
  // Only here, while the form is open: gone once it unmounts, never stored.
  const [typedViewKey, setTypedViewKey] = useState("")
  const [addressError, setAddressError] = useState("")
  const [viewKeyError, setViewKeyError] = useState("")
  const [saveError, setSaveError] = useState("")
  const [passkeyPromptIsOpen, setPasskeyPromptIsOpen] = useState(false)

  function changePasskeyPromptState(nextPasskeyPromptIsOpen: boolean) {
    setPasskeyPromptIsOpen(nextPasskeyPromptIsOpen)
    onPasskeyPromptChange(nextPasskeyPromptIsOpen)
  }

  async function confirmWithPasskey(submitEvent: FormEvent) {
    submitEvent.preventDefault()
    setSaveError("")
    const trimmedAddress = typedAddress.trim()
    const trimmedViewKey = typedViewKey.trim()
    let nextAddressError = describeMoneroAddressProblem(trimmedAddress, moneroChain.network)
    if (!nextAddressError && trimmedAddress === currentPrimaryAddress) nextAddressError = "That's already your Monero wallet."
    const nextViewKeyError = describePrivateViewKeyProblem(trimmedViewKey)
    setAddressError(nextAddressError)
    setViewKeyError(nextViewKeyError)
    if (nextAddressError || nextViewKeyError) return
    changePasskeyPromptState(true)
    try {
      const updatedWalletSettings = await setAccountMoneroWalletWithPasskey(moneroChain.key, trimmedAddress, trimmedViewKey)
      changePasskeyPromptState(false)
      setTypedViewKey("")
      onSaved(updatedWalletSettings)
      // The session's accepted coins list XMR on a chain only once a wallet is set there.
      void refreshAccountSession()
    } catch (saveFailure) {
      changePasskeyPromptState(false)
      if (saveFailure instanceof ApiError && saveFailure.httpStatus === 401) {
        await refreshAccountSession()
        return
      }
      const failureCode = saveFailure instanceof ApiError ? saveFailure.code : null
      if (failureCode === "monero_address_invalid" || failureCode === "monero_address_taken" || failureCode === "monero_wallet_unchanged") {
        setAddressError(describeError(saveFailure))
      } else if (failureCode === "monero_view_key_mismatch") setViewKeyError(describeError(saveFailure))
      else setSaveError(describePasskeyError(saveFailure))
    }
  }

  const exampleAddressStart = moneroChain.network === "mainnet" ? "4" : "5"
  return (
    <form onSubmit={(submitEvent) => void confirmWithPasskey(submitEvent)} noValidate className="flex flex-col">
      <FieldGroup className="gap-5">
        <Field data-invalid={Boolean(addressError) || undefined}>
          <FieldLabel htmlFor="settings_monero_address_input">Primary address</FieldLabel>
          <Input
            id="settings_monero_address_input"
            maxLength={106}
            autoComplete="off"
            spellCheck={false}
            autoCapitalize="off"
            placeholder={`e.g. ${exampleAddressStart}… (95 characters)`}
            value={typedAddress}
            aria-invalid={Boolean(addressError)}
            aria-describedby={addressError ? "settings_monero_address_error" : undefined}
            onChange={(changeEvent) => {
              setTypedAddress(changeEvent.target.value)
              setAddressError("")
              setSaveError("")
            }}
            className="h-12 rounded-xl px-3.5 font-mono text-base"
          />
          <FieldError id="settings_monero_address_error">{addressError}</FieldError>
        </Field>
        <Field data-invalid={Boolean(viewKeyError) || undefined}>
          <FieldLabel htmlFor="settings_monero_view_key_input">Secret view key</FieldLabel>
          {/* A text field masked with CSS, not type="password": browsers offer to save any password field whatever
              autocomplete says, and MDN's "Turning off form autocompletion"
              (https://developer.mozilla.org/en-US/docs/Web/Security/Practical_implementation_guides/Turning_off_form_autocompletion)
              notes autocomplete="off" is ignored for login-like fields; a view key must never land in a password
              manager. data-1p-ignore and data-lpignore keep 1Password and LastPass off it too. */}
          <Input
            id="settings_monero_view_key_input"
            type="text"
            maxLength={64}
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="off"
            spellCheck={false}
            data-1p-ignore
            data-lpignore="true"
            placeholder="e.g. 64 characters of 0-9 and a-f"
            value={typedViewKey}
            aria-invalid={Boolean(viewKeyError)}
            aria-describedby={viewKeyError ? "settings_monero_view_key_error" : undefined}
            onChange={(changeEvent) => {
              setTypedViewKey(changeEvent.target.value)
              setViewKeyError("")
              setSaveError("")
            }}
            className="h-12 rounded-xl px-3.5 font-mono text-base [-webkit-text-security:disc]"
          />
          <FieldError id="settings_monero_view_key_error">{viewKeyError}</FieldError>
        </Field>
      </FieldGroup>
      <ul id="settings_monero_wallet_notes" className="mt-5 flex list-disc flex-col gap-2 pl-5 text-[15px] text-muted-foreground">
        <li>Use a wallet made just for this: whoever holds the view key sees every payment into it.</li>
        <li>Payments show up under account 1 of that wallet, one subaddress per link.</li>
        <li>
          When Activity asks, run <span className="font-mono text-foreground">{moneroLookaheadCommand}</span> in the wallet and tap “I set it”.
        </li>
      </ul>
      {saveError ? (
        <Alert id="settings_monero_wallet_change_message" variant="destructive" className="mt-4">
          <AlertDescription>{saveError}</AlertDescription>
        </Alert>
      ) : null}
      <Button
        id="settings_confirm_monero_wallet_button"
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
