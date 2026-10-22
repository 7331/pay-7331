// `/settings`' "Coins and networks" sheet (PLAN.md section 10), one section per network that takes invoices: its coins
// to tick, then that network's defaults for invoices made without them: the payment window in minutes and the accepted
// shortfall in percent (empty: the server's default, which `/chains` names). Only an empty field means the default:
// minutes or confirmations the browser could not read (`validity.badInput`, whose value reads "") are field errors, and
// the shortfall is a text field that reads a decimal comma as a point. An EVM network says it counts a payment final at
// its finalized block (no count to set, PLAN.md section 4); a Monero network holds its wallet (changed only behind a
// fresh passkey check, account_monero_wallet_settings.tsx) and, once it takes XMR, its confirmations. "Save" sends only
// what changed, in one `PATCH /api/v1/accounts/current`; invoices already made keep the terms they were made with.
import { useState, type FormEvent } from "react"
import { toast } from "sonner"

import { AcceptedCoinPicker } from "@/components/accepted_coin_picker"
import { AccountMoneroWalletSettings } from "@/components/account_monero_wallet_settings"
import { ResponsiveSheet } from "@/components/responsive_sheet"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Field, FieldError, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { useAccountSession } from "@/hooks/use_account_session"
import { useChainList } from "@/hooks/use_chain_list"
import { describeError } from "@/lib/api_client"
import { normalizeAccuracyPercentText } from "@/lib/accuracy_percent_text"
import type { AccountSession, ListedChain } from "@/lib/api_types"
import { saveAccountSettings, type AccountSettingsPatchBody } from "@/lib/account_settings_requests"
import { primaryActionButtonClassName } from "@/lib/button_size_class_names"
import { cn } from "@/lib/utils"

const inputClassName = "h-12 w-full min-w-0 rounded-xl px-3.5 text-base"
const secondsPerMinute = 60

// One text field per chain key; null until typed, so the saved values show.
type TypedTextByChain = Record<string, string> | null
// The number fields whose typed text the browser could not read, by `${field}_${chain key}`.
type UnreadableFieldName = "lifetime_minutes" | "confirmations"

function describeLifetimeMinutesProblem(minutesText: string, isUnreadable: boolean): string {
  if (!minutesText.trim() && !isUnreadable) return ""
  const minutes = Number(minutesText)
  return Number.isInteger(minutes) && minutes >= 5 && minutes <= 720 ? "" : "Whole minutes from 5 to 720, or empty for the default."
}

function describeAccuracyPercentProblem(percentText: string): string {
  const trimmedText = normalizeAccuracyPercentText(percentText)
  if (!trimmedText) return ""
  const percent = Number(trimmedText)
  return /^\d+(\.\d{1,2})?$/.test(trimmedText) && percent >= 0 && percent <= 5 ? "" : "From 0 to 5, at most 2 decimals, or empty for the default."
}

function describeConfirmationsProblem(countText: string, isUnreadable: boolean): string {
  if (!countText.trim() && !isUnreadable) return ""
  const count = Number(countText)
  return Number.isInteger(count) && count >= 1 && count <= 100 ? "" : "A whole number from 1 to 100, or empty for the default."
}

function formatSavedLifetimeMinutes(lifetimeSeconds: number | undefined): string {
  return lifetimeSeconds === undefined ? "" : String(lifetimeSeconds / secondsPerMinute)
}

function formatSavedNumber(savedNumber: number | string | undefined): string {
  return savedNumber === undefined ? "" : String(savedNumber)
}

// The typed fields that differ from what is saved, as the PATCH field: `null` for an emptied one.
function collectChangedValues<SavedValue>(
  typedTextByChain: TypedTextByChain,
  savedTextByChain: Record<string, string>,
  readTypedText: (typedText: string) => SavedValue,
): Record<string, SavedValue | null> | undefined {
  if (typedTextByChain === null) return undefined
  const changedEntries = Object.entries(typedTextByChain)
    .filter(([chainKey, typedText]) => typedText.trim() !== (savedTextByChain[chainKey] ?? ""))
    .map(([chainKey, typedText]) => [chainKey, typedText.trim() ? readTypedText(typedText.trim()) : null] as const)
  return changedEntries.length > 0 ? Object.fromEntries(changedEntries) : undefined
}

export function AccountChainSettingsSheet({ accountSession, isOpen, onClose }: { accountSession: AccountSession; isOpen: boolean; onClose(): void }) {
  const { setAccountSession, refreshAccountSession } = useAccountSession()
  const { chains, chainLoadError } = useChainList()
  // null until a coin is tapped: the saved coins.
  const [pickedAcceptedSymbolsByChain, setPickedAcceptedSymbolsByChain] = useState<Record<string, string[]> | null>(null)
  const [typedLifetimeMinutesByChain, setTypedLifetimeMinutesByChain] = useState<TypedTextByChain>(null)
  const [typedAccuracyPercentByChain, setTypedAccuracyPercentByChain] = useState<TypedTextByChain>(null)
  const [typedConfirmationsByChain, setTypedConfirmationsByChain] = useState<TypedTextByChain>(null)
  const [unreadableFieldKeys, setUnreadableFieldKeys] = useState<ReadonlySet<string>>(new Set())
  const [isSaving, setIsSaving] = useState(false)
  const [saveError, setSaveError] = useState("")

  const acceptedSymbolsByChain = pickedAcceptedSymbolsByChain ?? accountSession.accepted_assets_by_chain
  const savedLifetimeMinutesByChain = Object.fromEntries(
    Object.entries(accountSession.invoice_lifetime_seconds_by_chain).map(([chainKey, seconds]) => [chainKey, formatSavedLifetimeMinutes(seconds)]),
  )
  const savedAccuracyPercentByChain = accountSession.accuracy_percent_by_chain
  const savedConfirmationsByChain = Object.fromEntries(
    Object.entries(accountSession.confirmations_final_by_chain).map(([chainKey, count]) => [chainKey, formatSavedNumber(count)]),
  )
  const lifetimeMinutesTextByChain = { ...savedLifetimeMinutesByChain, ...typedLifetimeMinutesByChain }
  const accuracyPercentTextByChain = { ...savedAccuracyPercentByChain, ...typedAccuracyPercentByChain }
  const confirmationsTextByChain = { ...savedConfirmationsByChain, ...typedConfirmationsByChain }

  const changedLifetimeMinutesByChain = collectChangedValues(typedLifetimeMinutesByChain, savedLifetimeMinutesByChain, (minutesText) => Number(minutesText) * secondsPerMinute)
  const changedAccuracyPercentByChain = collectChangedValues(typedAccuracyPercentByChain, savedAccuracyPercentByChain, normalizeAccuracyPercentText)
  const changedConfirmationsByChain = collectChangedValues(typedConfirmationsByChain, savedConfirmationsByChain, (countText) => Number(countText))
  const somethingChanged = Boolean(pickedAcceptedSymbolsByChain || changedLifetimeMinutesByChain || changedAccuracyPercentByChain || changedConfirmationsByChain)
  // Only typed fields are judged: a value saved through the API stands as it is.
  const problemsByChain = Object.fromEntries(
    (chains ?? []).map((chain) => [
      chain.key,
      {
        lifetimeMinutes: describeLifetimeMinutesProblem(typedLifetimeMinutesByChain?.[chain.key] ?? "", unreadableFieldKeys.has(`lifetime_minutes_${chain.key}`)),
        accuracyPercent: describeAccuracyPercentProblem(typedAccuracyPercentByChain?.[chain.key] ?? ""),
        confirmations: describeConfirmationsProblem(typedConfirmationsByChain?.[chain.key] ?? "", unreadableFieldKeys.has(`confirmations_${chain.key}`)),
      },
    ]),
  )
  const someFieldIsWrong = Object.values(problemsByChain).some((problems) => Object.values(problems).some(Boolean))

  function forgetTypedValues() {
    setPickedAcceptedSymbolsByChain(null)
    setTypedLifetimeMinutesByChain(null)
    setTypedAccuracyPercentByChain(null)
    setTypedConfirmationsByChain(null)
    setUnreadableFieldKeys(new Set())
    setSaveError("")
  }

  function noteFieldReadability(fieldName: UnreadableFieldName, chainKey: string, isUnreadable: boolean) {
    const fieldKey = `${fieldName}_${chainKey}`
    if (unreadableFieldKeys.has(fieldKey) === isUnreadable) return
    const nextFieldKeys = new Set(unreadableFieldKeys)
    if (isUnreadable) nextFieldKeys.add(fieldKey)
    else nextFieldKeys.delete(fieldKey)
    setUnreadableFieldKeys(nextFieldKeys)
  }

  function changeOpenState(nextIsOpen: boolean) {
    if (isSaving || nextIsOpen) return
    forgetTypedValues()
    onClose()
  }

  async function saveChainSettings(submitEvent: FormEvent) {
    submitEvent.preventDefault()
    if (!somethingChanged || someFieldIsWrong) return
    setSaveError("")
    setIsSaving(true)
    const patchBody: AccountSettingsPatchBody = {
      accepted_assets_by_chain: pickedAcceptedSymbolsByChain ?? undefined,
      invoice_lifetime_seconds_by_chain: changedLifetimeMinutesByChain,
      accuracy_percent_by_chain: changedAccuracyPercentByChain,
      confirmations_final_by_chain: changedConfirmationsByChain,
    }
    try {
      setAccountSession(await saveAccountSettings(patchBody, refreshAccountSession))
      toast.success("Coins and network defaults saved.")
      forgetTypedValues()
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
      title="Coins and networks"
      description="Per network: the coins payers may use, and the time to pay and shortfall your invoices get when you don't set them."
      testId="settings_accepted_coins_sheet"
    >
      <form onSubmit={(submitEvent) => void saveChainSettings(submitEvent)} noValidate className="flex min-w-0 flex-col">
        <div className="flex min-w-0 flex-col gap-7">
          {chains ? (
            chains.map((chain) => (
              <ChainSettingsSection
                key={chain.key}
                chain={chain}
                problems={problemsByChain[chain.key]}
                takesInvoicesForAccount={chain.key in accountSession.accepted_assets_by_chain}
                acceptedSymbols={acceptedSymbolsByChain[chain.key] ?? chain.default_accepted_assets}
                onAcceptedSymbolsChange={(nextSymbols) => setPickedAcceptedSymbolsByChain({ ...acceptedSymbolsByChain, [chain.key]: nextSymbols })}
                lifetimeMinutesText={lifetimeMinutesTextByChain[chain.key] ?? ""}
                onLifetimeMinutesTextChange={(nextText) => {
                  setTypedLifetimeMinutesByChain({ ...typedLifetimeMinutesByChain, [chain.key]: nextText })
                  setSaveError("")
                }}
                onLifetimeMinutesReadabilityChange={(isUnreadable) => {
                  // Typed but unreadable still counts as typed, so it is judged.
                  if (isUnreadable && typedLifetimeMinutesByChain?.[chain.key] === undefined) setTypedLifetimeMinutesByChain({ ...typedLifetimeMinutesByChain, [chain.key]: "" })
                  noteFieldReadability("lifetime_minutes", chain.key, isUnreadable)
                }}
                accuracyPercentText={accuracyPercentTextByChain[chain.key] ?? ""}
                onAccuracyPercentTextChange={(nextText) => {
                  setTypedAccuracyPercentByChain({ ...typedAccuracyPercentByChain, [chain.key]: nextText })
                  setSaveError("")
                }}
                confirmationsText={confirmationsTextByChain[chain.key] ?? ""}
                onConfirmationsTextChange={(nextText) => {
                  setTypedConfirmationsByChain({ ...typedConfirmationsByChain, [chain.key]: nextText })
                  setSaveError("")
                }}
                onConfirmationsReadabilityChange={(isUnreadable) => {
                  if (isUnreadable && typedConfirmationsByChain?.[chain.key] === undefined) setTypedConfirmationsByChain({ ...typedConfirmationsByChain, [chain.key]: "" })
                  noteFieldReadability("confirmations", chain.key, isUnreadable)
                }}
              />
            ))
          ) : chainLoadError ? null : (
            <Skeleton className="h-11 w-full rounded-full" />
          )}
        </div>
        {saveError || chainLoadError ? (
          <Alert id="settings_accepted_coins_message" variant="destructive" className="mt-4">
            <AlertDescription>{saveError || `Can't load the networks: ${chainLoadError}`}</AlertDescription>
          </Alert>
        ) : null}
        <Button
          id="settings_save_accepted_coins_button"
          type="submit"
          className={cn(primaryActionButtonClassName, "mt-6 w-full")}
          disabled={isSaving || !chains || !somethingChanged || someFieldIsWrong}
        >
          {isSaving ? <Spinner /> : null}
          {isSaving ? "Saving…" : "Save"}
        </Button>
      </form>
    </ResponsiveSheet>
  )
}

interface ChainSettingsProblems {
  lifetimeMinutes: string
  accuracyPercent: string
  confirmations: string
}

function ChainSettingsSection({
  chain,
  problems,
  takesInvoicesForAccount,
  acceptedSymbols,
  onAcceptedSymbolsChange,
  lifetimeMinutesText,
  onLifetimeMinutesTextChange,
  onLifetimeMinutesReadabilityChange,
  accuracyPercentText,
  onAccuracyPercentTextChange,
  confirmationsText,
  onConfirmationsTextChange,
  onConfirmationsReadabilityChange,
}: {
  chain: ListedChain
  problems: ChainSettingsProblems
  // A Monero network takes invoices only once the account has a wallet there (the session then lists its coins).
  takesInvoicesForAccount: boolean
  acceptedSymbols: string[]
  onAcceptedSymbolsChange(nextSymbols: string[]): void
  lifetimeMinutesText: string
  onLifetimeMinutesTextChange(nextText: string): void
  // Each keystroke: whether the browser could not read the field (React skips onChange while its value stays "").
  onLifetimeMinutesReadabilityChange(isUnreadable: boolean): void
  accuracyPercentText: string
  onAccuracyPercentTextChange(nextText: string): void
  confirmationsText: string
  onConfirmationsTextChange(nextText: string): void
  onConfirmationsReadabilityChange(isUnreadable: boolean): void
}) {
  const isMonero = chain.kind === "monero"
  const { lifetimeMinutes: lifetimeMinutesProblem, accuracyPercent: accuracyPercentProblem, confirmations: confirmationsProblem } = problems
  const defaultLifetimeMinutes = chain.default_invoice_lifetime_seconds / secondsPerMinute
  return (
    <section id={`settings_chain_section_${chain.key}`} aria-labelledby={`settings_chain_title_${chain.key}`} className="flex min-w-0 flex-col gap-4">
      <h3 id={`settings_chain_title_${chain.key}`} className="text-base font-semibold">
        {chain.name}
      </h3>
      {isMonero ? <AccountMoneroWalletSettings moneroChainKey={chain.key} /> : null}
      {takesInvoicesForAccount ? (
        <>
          <AcceptedCoinPicker
            chainAssets={chain.assets}
            networkKey={chain.key}
            networkName={chain.name}
            acceptedSymbols={acceptedSymbols}
            onAcceptedSymbolsChange={onAcceptedSymbolsChange}
          />
          {/* minmax(0, 1fr): a column never grows past half the sheet whatever its input's intrinsic width. */}
          <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-3">
            <Field data-invalid={Boolean(lifetimeMinutesProblem) || undefined} className="min-w-0">
              <FieldLabel htmlFor={`settings_lifetime_minutes_input_${chain.key}`}>Time to pay, min</FieldLabel>
              <Input
                id={`settings_lifetime_minutes_input_${chain.key}`}
                type="number"
                inputMode="numeric"
                min={5}
                max={720}
                step={1}
                autoComplete="off"
                placeholder={`Default (${defaultLifetimeMinutes})`}
                value={lifetimeMinutesText}
                aria-invalid={Boolean(lifetimeMinutesProblem)}
                aria-describedby={lifetimeMinutesProblem ? `settings_lifetime_minutes_error_${chain.key}` : undefined}
                onChange={(changeEvent) => onLifetimeMinutesTextChange(changeEvent.target.value)}
                onInput={(inputEvent) => onLifetimeMinutesReadabilityChange(inputEvent.currentTarget.validity.badInput)}
                className={inputClassName}
              />
            </Field>
            <Field data-invalid={Boolean(accuracyPercentProblem) || undefined} className="min-w-0">
              <FieldLabel htmlFor={`settings_accuracy_percent_input_${chain.key}`}>Shortfall, %</FieldLabel>
              <Input
                id={`settings_accuracy_percent_input_${chain.key}`}
                type="text"
                inputMode="decimal"
                autoComplete="off"
                placeholder={`Default (${chain.default_accuracy_percent})`}
                value={accuracyPercentText}
                aria-invalid={Boolean(accuracyPercentProblem)}
                aria-describedby={accuracyPercentProblem ? `settings_accuracy_percent_error_${chain.key}` : undefined}
                onChange={(changeEvent) => onAccuracyPercentTextChange(changeEvent.target.value)}
                className={inputClassName}
              />
            </Field>
          </div>
          {lifetimeMinutesProblem ? <FieldError id={`settings_lifetime_minutes_error_${chain.key}`}>{lifetimeMinutesProblem}</FieldError> : null}
          {accuracyPercentProblem ? <FieldError id={`settings_accuracy_percent_error_${chain.key}`}>{accuracyPercentProblem}</FieldError> : null}
          {isMonero ? (
            <Field data-invalid={Boolean(confirmationsProblem) || undefined}>
              <FieldLabel htmlFor={`settings_confirmations_input_${chain.key}`}>Confirmations</FieldLabel>
              <Input
                id={`settings_confirmations_input_${chain.key}`}
                type="number"
                inputMode="numeric"
                min={1}
                max={100}
                autoComplete="off"
                placeholder={`Default (${chain.confirmations_final ?? "…"})`}
                value={confirmationsText}
                aria-invalid={Boolean(confirmationsProblem)}
                aria-describedby={confirmationsProblem ? `settings_confirmations_error_${chain.key}` : `settings_confirmations_description_${chain.key}`}
                onChange={(changeEvent) => onConfirmationsTextChange(changeEvent.target.value)}
                onInput={(inputEvent) => onConfirmationsReadabilityChange(inputEvent.currentTarget.validity.badInput)}
                className={inputClassName}
              />
              {confirmationsProblem ? (
                <FieldError id={`settings_confirmations_error_${chain.key}`}>{confirmationsProblem}</FieldError>
              ) : (
                <p id={`settings_confirmations_description_${chain.key}`} className="text-sm text-muted-foreground">
                  Blocks before an XMR payment counts. Fewer is faster but takes on the reorg risk.
                </p>
              )}
            </Field>
          ) : (
            <p id={`settings_finality_note_${chain.key}`} className="text-sm text-muted-foreground">
              A payment counts once {chain.name} marks its block finalized.
            </p>
          )}
        </>
      ) : isMonero ? (
        <p className="text-sm text-muted-foreground">Set a wallet to take XMR on this network.</p>
      ) : null}
    </section>
  )
}
