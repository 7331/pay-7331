// `/`, the one screen that makes a link (PLAN.md section 10), sentence-shaped with no labels the content already
// explains: the big amount with $1/$2/$5/$10 under it, where the money goes (required signed out;
// signed in the payout address, shown short until "Change", or the last link's receiver after "Make another"), a quiet
// "+ Add a note" once the address is valid, and one row of the accepted coins that opens their sheet. Signed in,
// "Options" beside "+ Add a note" opens every other invoice option (invoice_options_sheet.tsx); `/?options=1` (where
// the old `/new` goes) opens it too. "Create link" posts `POST /api/v1/public/invoices` signed out,
// `POST /api/v1/invoices` signed in, `POST /api/v1/payment-links` for more than one payer (or
// `POST /api/v1/invoice-schedules` with "Repeat" set, then `/activity`'s repeating invoices), and goes to the new
// invoice's (or link's) page under the share overlay, so the address bar is the link. A wrong value shows under its own
// field (an option's in the options sheet, which opens at it); only a refusal from the API shows by the button.
import { Plus, SlidersHorizontal, Wallet } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { useEffect, useState, type FormEvent } from "react"
import { useLocation, useNavigate, useSearchParams } from "react-router"
import { toast } from "sonner"

import { PageColumn } from "@/components/page_column"
import { ReceiverAddressField } from "@/components/receiver_address_field"
import { SiteTopBar } from "@/components/site_top_bar"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"
import { useAccountSession } from "@/hooks/use_account_session"
import { listChainsForMaker, pickStartingNetworkKeys, useChainList } from "@/hooks/use_chain_list"
import { readReceiverAddressForNextInvoice, useNavigateToCreatedInvoice } from "@/hooks/use_created_invoice_share"
import { useNavigateToCreatedPaymentLink } from "@/hooks/use_created_payment_link_share"
import { addressPattern, formatDecimalForDisplay, formatFiatAmount, shortenHexString } from "@/lib/amount_formatting"
import { ApiError, fetchApiJson } from "@/lib/api_client"
import type { AccountInvoice, AccountInvoiceSchedule, AccountPaymentLink, InvoiceNetworkRequestFields, PublicInvoice } from "@/lib/api_types"
import { primaryActionButtonClassName, touchTargetButtonClassName } from "@/lib/button_size_class_names"
import { describeInvoiceCreationError } from "@/lib/invoice_creation_errors"
import { listCoinsAcrossNetworks, listNetworksOfferingSymbol, readNetworkAcceptedSymbols } from "@/lib/maker_networks"
import { cn } from "@/lib/utils"
import { AcceptedCoinsRow } from "@/pages/invoice_creation/accepted_coins_row"
import { describeAmountUnit, type AmountUnit } from "@/pages/invoice_creation/amount_unit"
import { BigAmountField } from "@/pages/invoice_creation/big_amount_field"
import {
  checkInvoiceOptions,
  currencyCodeIsCoin,
  defaultInvoiceOptions,
  invoiceOptionInputIds,
  readPayerQuantity,
  repeatIntervalChoices,
  summarizeChangedInvoiceOptions,
  type CheckedInvoiceOptions,
  type InvoiceOptionFieldErrors,
  type InvoiceOptionFieldName,
  type InvoiceOptions,
} from "@/pages/invoice_creation/invoice_options"
import { InvoiceOptionsSheet } from "@/pages/invoice_creation/invoice_options_sheet"

const minimumAmountUsd = 1
const quietTextButtonClassName = cn(touchTargetButtonClassName, "px-2 text-muted-foreground hover:text-foreground")

// The amount's own sentence when it can't be sent: empty, not a number in its currency, or below $1.
function describeAmountProblem(amountText: string, amountUnit: AmountUnit, currencyCode: string): string {
  const decimalPattern = new RegExp(`^\\d+(\\.\\d{1,${amountUnit.fractionDigits}})?$`)
  if (!amountText) return "Type an amount, like 12.50."
  if (!decimalPattern.test(amountText) || Number(amountText) <= 0) return "That's not an amount: a number above zero, like 12.50."
  if (currencyCode === "USD" && Number(amountText) < minimumAmountUsd) return `The smallest invoice is $${minimumAmountUsd}.`
  return ""
}

export function InvoiceCreationPage() {
  const { accountSession, refreshAccountSession, setSignUpPayoutAddressSuggestion } = useAccountSession()
  const { chains: listedChains, chainLoadError } = useChainList()
  const makerChains = listChainsForMaker(listedChains, accountSession)
  const pageLocation = useLocation()
  const [searchParams, setSearchParams] = useSearchParams()
  const [invoiceOptions, setInvoiceOptions] = useState<InvoiceOptions>(defaultInvoiceOptions)
  const [amountText, setAmountText] = useState("5")
  // null until typed: the last link's receiver after "Make another", else signed in the payout address, signed out
  // empty.
  const [typedReceiverAddress, setTypedReceiverAddress] = useState<string | null>(() =>
    readReceiverAddressForNextInvoice(pageLocation.state),
  )
  const [receiverAddressIsBeingChanged, setReceiverAddressIsBeingChanged] = useState(false)
  const [noteText, setNoteText] = useState("")
  const [noteFieldIsShown, setNoteFieldIsShown] = useState(false)
  // null until the maker taps a network: every EVM network (the payer picks one, PLAN.md section 15).
  const [pickedNetworkKeys, setPickedNetworkKeys] = useState<string[] | null>(null)
  // The coins the maker ticked, by network key; a network without an entry starts from its saved or default coins.
  const [pickedSymbolsByNetworkKey, setPickedSymbolsByNetworkKey] = useState<Record<string, string[]>>({})
  const [optionsSheetIsOpen, setOptionsSheetIsOpen] = useState(false)
  // `/?options=1` (where the old `/new` goes) opens the options while signed in; signed out there are none.
  const optionsSheetIsShown = optionsSheetIsOpen || (searchParams.get("options") === "1" && accountSession !== null)
  const [amountError, setAmountError] = useState("")
  const [receiverAddressError, setReceiverAddressError] = useState("")
  const [optionFieldErrors, setOptionFieldErrors] = useState<InvoiceOptionFieldErrors>({})
  const [formError, setFormError] = useState("")
  const [isCreating, setIsCreating] = useState(false)
  const navigateToCreatedInvoice = useNavigateToCreatedInvoice()
  const navigateToCreatedPaymentLink = useNavigateToCreatedPaymentLink()
  const navigate = useNavigate()

  const pickedNetworkKeysStillOffered = pickedNetworkKeys?.filter((networkKey) => makerChains?.some((chain) => chain.key === networkKey)) ?? []
  const selectedNetworkKeys = pickedNetworkKeysStillOffered.length > 0 ? pickedNetworkKeysStillOffered : pickStartingNetworkKeys(makerChains)
  const selectedNetworks = (makerChains ?? []).filter((chain) => selectedNetworkKeys.includes(chain.key))
  // An XMR link pays the account's Monero wallet from Settings; the server ignores a receiver address for it. Monero is
  // one network on its own, never mixed with the EVM ones.
  const paysMoneroWallet = selectedNetworks[0]?.kind === "monero"
  const networkCoins = listCoinsAcrossNetworks(selectedNetworks)
  // Signed out, the options don't apply: the public invoice is in USD.
  const effectiveOptions = accountSession ? invoiceOptions : defaultInvoiceOptions
  const currencyIsCoin = currencyCodeIsCoin(effectiveOptions.currencyCode)
  const amountUnit = describeAmountUnit(effectiveOptions.currencyCode, networkCoins)
  // Coin-denominated amounts name one network; fiat amounts let the payer choose among networks.
  const acceptingNetworks = currencyIsCoin ? listNetworksOfferingSymbol(selectedNetworks, effectiveOptions.currencyCode).slice(0, 1) : selectedNetworks
  // Each network keeps its own ticks: USDC can be accepted on Polygon alone. A coin-priced link is that coin everywhere.
  const acceptedSymbolsByNetworkKey: Record<string, string[]> = Object.fromEntries(
    acceptingNetworks.map((network) => [
      network.key,
      currencyIsCoin ? [effectiveOptions.currencyCode] : readNetworkAcceptedSymbols(network, pickedSymbolsByNetworkKey, accountSession),
    ]),
  )
  const receiverAddress = typedReceiverAddress ?? accountSession?.payout_address ?? ""
  const receiverAddressIsValid = paysMoneroWallet || addressPattern.test(receiverAddress.trim())
  const receiverAddressIsPayoutAddress =
    accountSession !== null && receiverAddress.trim().toLowerCase() === accountSession.payout_address.toLowerCase()
  const showsShortPayoutAddress = receiverAddressIsPayoutAddress && !receiverAddressIsBeingChanged && !receiverAddressError
  const payerQuantity = accountSession ? readPayerQuantity(invoiceOptions.payerQuantityText) : 1
  const changedOptionWords = accountSession ? summarizeChangedInvoiceOptions(invoiceOptions) : []

  // Signing up from here prefills the payout address with the receiver already typed.
  useEffect(() => {
    setSignUpPayoutAddressSuggestion(receiverAddressIsValid ? receiverAddress.trim() : "")
  }, [receiverAddress, receiverAddressIsValid, setSignUpPayoutAddressSuggestion])

  function changeOptionsSheetIsOpen(nextIsOpen: boolean) {
    setOptionsSheetIsOpen(nextIsOpen)
    if (!nextIsOpen && searchParams.has("options")) setSearchParams({}, { replace: true, state: pageLocation.state })
  }

  function changeInvoiceOptions(changedOptions: Partial<InvoiceOptions>) {
    if (changedOptions.currencyCode && currencyCodeIsCoin(changedOptions.currencyCode)) {
      const coinNetworks = listNetworksOfferingSymbol(selectedNetworks, changedOptions.currencyCode)
      if (coinNetworks.length > 0) setPickedNetworkKeys([coinNetworks[0].key])
    }
    // The currency can make a quantity wrong.
    if (changedOptions.currencyCode !== undefined && changedOptions.currencyCode !== invoiceOptions.currencyCode) clearOptionFieldError("payerQuantity")
    setInvoiceOptions((currentOptions) => ({ ...currentOptions, ...changedOptions }))
    setFormError("")
  }

  // A network ticked or unticked in the Accept sheet. Every network keeps its own coin ticks (a network that comes back
  // has them still), so nothing is merged or widened here.
  function changeSelectedNetworkKeys(nextNetworkKeys: string[]) {
    setPickedNetworkKeys(nextNetworkKeys)
    // A coin currency the new networks don't list goes back to dollars.
    const nextCoins = listCoinsAcrossNetworks((makerChains ?? []).filter((chain) => nextNetworkKeys.includes(chain.key)))
    if (currencyIsCoin && !nextCoins.some((coin) => coin.symbol === effectiveOptions.currencyCode)) {
      changeInvoiceOptions({ currencyCode: defaultInvoiceOptions.currencyCode })
    }
    setFormError("")
  }

  function changeNetworkAcceptedSymbols(networkKey: string, nextSymbols: string[]) {
    setPickedSymbolsByNetworkKey((currentSymbols) => ({ ...currentSymbols, [networkKey]: nextSymbols }))
    setFormError("")
  }

  function clearOptionFieldError(fieldName: InvoiceOptionFieldName) {
    setOptionFieldErrors((currentErrors) => (currentErrors[fieldName] ? { ...currentErrors, [fieldName]: undefined } : currentErrors))
    setFormError("")
  }

  // Opens the options at the first wrong one, once the sheet's fields are on screen.
  function revealOptionFieldErrors(fieldErrors: InvoiceOptionFieldErrors) {
    setOptionFieldErrors(fieldErrors)
    setOptionsSheetIsOpen(true)
    const firstWrongFieldName = (Object.keys(invoiceOptionInputIds) as InvoiceOptionFieldName[]).find((fieldName) => fieldErrors[fieldName])
    if (!firstWrongFieldName) return
    window.setTimeout(() => {
      const fieldElement = document.getElementById(invoiceOptionInputIds[firstWrongFieldName])
      fieldElement?.scrollIntoView({ block: "center" })
      fieldElement?.focus({ preventScroll: true })
    }, 300)
  }

  async function submitInvoice(submitEvent: FormEvent) {
    submitEvent.preventDefault()
    const normalizedAmount = amountText.trim()
    const trimmedReceiverAddress = receiverAddress.trim()
    const nextAmountError = describeAmountProblem(normalizedAmount, amountUnit, effectiveOptions.currencyCode)
    let nextReceiverAddressError = ""
    if (paysMoneroWallet) nextReceiverAddressError = ""
    else if (!trimmedReceiverAddress) nextReceiverAddressError = "Paste your wallet address: where should the money go?"
    else if (!receiverAddressIsValid) nextReceiverAddressError = "That's not a full address: 0x and then 40 letters and digits (0-9, a-f)."
    setAmountError(nextAmountError)
    setReceiverAddressError(nextReceiverAddressError)
    setFormError("")
    if (nextAmountError || nextReceiverAddressError || acceptingNetworks.length === 0) return

    let checkedOptions: CheckedInvoiceOptions | null = null
    if (accountSession) {
      const optionCheck = checkInvoiceOptions(invoiceOptions)
      if (!optionCheck.checkedOptions) {
        revealOptionFieldErrors(optionCheck.fieldErrors)
        return
      }
      checkedOptions = optionCheck.checkedOptions
    }
    setOptionFieldErrors({})
    setIsCreating(true)
    try {
      // The page unmounts on the way, so isCreating stays set: no second tap in between.
      if (!checkedOptions) await createPublicInvoice(normalizedAmount, trimmedReceiverAddress)
      else if (checkedOptions.repeatInterval) await createInvoiceSchedule(normalizedAmount, trimmedReceiverAddress, checkedOptions, checkedOptions.repeatInterval)
      else if (checkedOptions.payerQuantity > 1) await createPaymentLink(normalizedAmount, trimmedReceiverAddress, checkedOptions)
      else await createAccountInvoice(normalizedAmount, trimmedReceiverAddress, checkedOptions)
      // Signed in, the ticked coins of a money-priced link are now the account's starting coins.
      if (accountSession) void refreshAccountSession()
    } catch (createError) {
      if (createError instanceof ApiError && createError.httpStatus === 401) await refreshAccountSession()
      setFormError(describeInvoiceCreationError(createError))
      setIsCreating(false)
    }
  }

  async function createPublicInvoice(normalizedAmount: string, trimmedReceiverAddress: string) {
    // The API defaults to every coin of every network; send the ticks only when the maker narrowed any network's.
    const acceptsEveryCoin = acceptingNetworks.every((network) => acceptedSymbolsByNetworkKey[network.key].length === network.assets.length)
    const createdInvoice = await fetchApiJson<PublicInvoice>("/api/v1/public/invoices", {
      method: "POST",
      body: {
        amount_usd: normalizedAmount,
        destination_address: trimmedReceiverAddress,
        note: noteText.trim() || undefined,
        chains: acceptingNetworks.map((network) => network.key),
        assets_by_chain: acceptsEveryCoin ? undefined : acceptedSymbolsByNetworkKey,
      },
    })
    await navigateToCreatedInvoice(createdInvoice, {
      amountText: formatFiatAmount(normalizedAmount, "USD"),
      noteText: noteText.trim(),
      receiverAddress: trimmedReceiverAddress,
    })
  }

  // The account's fields every invoice and quantity link share; JSON.stringify drops the undefined ones.
  function buildAccountRequestBody(normalizedAmount: string, trimmedReceiverAddress: string, checkedOptions: CheckedInvoiceOptions) {
    const networkFields: InvoiceNetworkRequestFields = paysMoneroWallet
      ? { chain: acceptingNetworks[0]?.key, assets: acceptedSymbolsByNetworkKey[acceptingNetworks[0]?.key] }
      : {
          chains: acceptingNetworks.map((network) => network.key),
          // The exact ticks per network win; the union is the older `assets` field, which the API reads second.
          assets_by_chain: acceptedSymbolsByNetworkKey,
          assets: [...new Set(Object.values(acceptedSymbolsByNetworkKey).flat())],
        }
    return {
      amount: normalizedAmount,
      currency: invoiceOptions.currencyCode,
      // One Monero chain on its own, else the EVM networks the payer picks from (PLAN.md section 15).
      ...networkFields,
      // Left out on an XMR link: it pays the account's Monero wallet.
      destination_address: paysMoneroWallet ? undefined : trimmedReceiverAddress,
      allow_contract_destination: checkedOptions.allowContractDestination,
      note: noteText.trim() || undefined,
      lifetime_seconds: checkedOptions.lifetimeSeconds,
      accuracy_percent: checkedOptions.accuracyPercent,
      return_url: checkedOptions.returnUrl,
    }
  }

  async function createAccountInvoice(normalizedAmount: string, trimmedReceiverAddress: string, checkedOptions: CheckedInvoiceOptions) {
    const createdInvoice = await fetchApiJson<AccountInvoice>("/api/v1/invoices", {
      method: "POST",
      body: {
        ...buildAccountRequestBody(normalizedAmount, trimmedReceiverAddress, checkedOptions),
        order_id: checkedOptions.orderId,
        underpaid_open_until: checkedOptions.underpaidOpenUntil,
        metadata: checkedOptions.metadata,
      },
    })
    await navigateToCreatedInvoice(createdInvoice, {
      amountText:
        createdInvoice.fiat.currency && createdInvoice.fiat.amount
          ? formatFiatAmount(createdInvoice.fiat.amount, createdInvoice.fiat.currency)
          : `${formatDecimalForDisplay(normalizedAmount)} ${invoiceOptions.currencyCode}`,
      noteText: noteText.trim(),
      // An XMR invoice's destination is the Monero wallet, never an EVM receiver "Make another" could prefill.
      receiverAddress: createdInvoice.chain.kind === "monero" ? "" : createdInvoice.destination_address,
    })
  }

  async function createPaymentLink(normalizedAmount: string, trimmedReceiverAddress: string, checkedOptions: CheckedInvoiceOptions) {
    const createdPaymentLink = await fetchApiJson<AccountPaymentLink>("/api/v1/payment-links", {
      method: "POST",
      body: { ...buildAccountRequestBody(normalizedAmount, trimmedReceiverAddress, checkedOptions), quantity: checkedOptions.payerQuantity },
    })
    await navigateToCreatedPaymentLink(createdPaymentLink, {
      amountText: `${formatFiatAmount(createdPaymentLink.fiat.amount, createdPaymentLink.fiat.currency ?? invoiceOptions.currencyCode)} × ${checkedOptions.payerQuantity}`,
      noteText: noteText.trim(),
    })
  }

  // A repeating invoice's first one is made by the watcher within a minute and goes to the account's webhook; `/` opens
  // `/activity`'s repeating invoices, under its Links tab, where it shows.
  async function createInvoiceSchedule(
    normalizedAmount: string,
    trimmedReceiverAddress: string,
    checkedOptions: CheckedInvoiceOptions,
    repeatInterval: NonNullable<CheckedInvoiceOptions["repeatInterval"]>,
  ) {
    await fetchApiJson<AccountInvoiceSchedule>("/api/v1/invoice-schedules", {
      method: "POST",
      body: {
        ...buildAccountRequestBody(normalizedAmount, trimmedReceiverAddress, checkedOptions),
        interval: repeatInterval,
        order_id_prefix: checkedOptions.orderId,
        metadata: checkedOptions.metadata,
      },
    })
    toast.success("Repeating invoice set up. The first one comes within a minute.")
    await navigate("/activity#schedules")
  }

  const repeatLabel = accountSession ? repeatIntervalChoices.find((choice) => choice.value && choice.value === invoiceOptions.repeatInterval)?.label : undefined
  let createButtonText = "Create link"
  if (repeatLabel) createButtonText = `Create ${repeatLabel.toLowerCase()} invoice`
  else if (payerQuantity !== null && payerQuantity > 1) createButtonText = `Create link for ${payerQuantity}`

  return (
    <form onSubmit={submitInvoice} noValidate className="contents">
      <PageColumn
        topBar={<SiteTopBar />}
        footer={
          <>
            {formError || chainLoadError ? (
              <Alert id="form_message_area" variant="destructive">
                <AlertDescription>{formError || `Can't load the coins: ${chainLoadError}`}</AlertDescription>
              </Alert>
            ) : null}
            <Button id="create_button" type="submit" className={cn(primaryActionButtonClassName, "w-full")} disabled={isCreating || acceptingNetworks.length === 0}>
              {isCreating ? <Spinner /> : null}
              {isCreating ? "Creating link…" : createButtonText}
            </Button>
          </>
        }
      >
        <BigAmountField
          amountText={amountText}
          amountUnit={amountUnit}
          amountError={amountError}
          onAmountTextChange={(nextAmountText) => {
            setAmountText(nextAmountText)
            setFormError("")
          }}
          onAmountErrorChange={setAmountError}
        />

        <div className="mt-6 flex flex-col gap-3">
          {paysMoneroWallet ? (
            <p id="monero_receiver_line" className="flex min-h-12 items-center gap-3 rounded-xl border border-input px-3.5 text-[15px] text-muted-foreground">
              <Wallet aria-hidden="true" className="size-5 shrink-0" />
              Paid to your Monero wallet from Settings
            </p>
          ) : showsShortPayoutAddress && accountSession ? (
            <button
              id="change_receiver_address_button"
              type="button"
              aria-label={`Get paid at your payout address ${accountSession.payout_address}. Change it for this link`}
              onClick={() => {
                setReceiverAddressIsBeingChanged(true)
                setTypedReceiverAddress(receiverAddress)
              }}
              className={cn(
                "flex min-h-12 w-full items-center gap-3 rounded-xl border border-input px-3.5 text-left text-[15px] outline-none",
                "transition-colors hover:bg-muted/60 focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50",
              )}
            >
              <Wallet aria-hidden="true" className="size-5 shrink-0 text-muted-foreground" />
              <span className="min-w-0 flex-1 truncate font-mono">{shortenHexString(accountSession.payout_address, 6)}</span>
              <span aria-hidden="true" className="shrink-0 text-muted-foreground">
                Change
              </span>
            </button>
          ) : (
            <ReceiverAddressField
              inputId="receiver_address_input"
              errorId="receiver_address_error"
              pasteButtonId="paste_receiver_address_button"
              label="Get paid at"
              labelIsHidden
              focusesOnMount={receiverAddressIsBeingChanged}
              placeholder="0x… your wallet address"
              addressText={receiverAddress}
              errorText={receiverAddressError}
              onAddressTextChange={(nextReceiverAddress) => {
                setTypedReceiverAddress(nextReceiverAddress)
                setFormError("")
              }}
              onErrorTextChange={setReceiverAddressError}
            />
          )}
          <AcceptedCoinsRow
            makerNetworks={makerChains}
            selectedNetworks={selectedNetworks}
            acceptingNetworks={acceptingNetworks}
            onSelectedNetworkKeysChange={changeSelectedNetworkKeys}
            acceptedSymbolsByNetworkKey={acceptedSymbolsByNetworkKey}
            onNetworkAcceptedSymbolsChange={changeNetworkAcceptedSymbols}
            lockedToSymbol={currencyIsCoin ? effectiveOptions.currencyCode : null}
          />
          <AnimatePresence initial={false}>
            {noteFieldIsShown && receiverAddressIsValid ? (
              // The height animation needs overflow hidden; 6 px of padding, taken back by negative margins, leaves room
              // for the field's border and 3 px focus ring inside the clip, so neither is cut.
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ type: "spring", visualDuration: 0.35, bounce: 0 }}
                className="-m-1.5 overflow-hidden"
              >
                <div className="p-1.5">
                  <Input
                    id="note_input"
                    aria-label="Note for the payer"
                    maxLength={500}
                    autoComplete="off"
                    autoFocus
                    placeholder="e.g. Logo design, invoice 14"
                    value={noteText}
                    onChange={(changeEvent) => setNoteText(changeEvent.target.value)}
                    className="h-12 rounded-xl px-3.5 text-base"
                  />
                </div>
              </motion.div>
            ) : null}
          </AnimatePresence>
          {/* The quiet line: "+ Add a note" once the address is valid, "Options" signed in. */}
          <div className="-mx-2 flex min-h-11 items-center justify-between gap-2">
            {receiverAddressIsValid && !noteFieldIsShown ? (
              <Button id="add_note_button" type="button" variant="ghost" className={quietTextButtonClassName} onClick={() => setNoteFieldIsShown(true)}>
                <Plus /> Add a note
              </Button>
            ) : (
              <span />
            )}
            {accountSession ? (
              <Button
                id="invoice_options_button"
                type="button"
                variant="ghost"
                className={cn(
                  quietTextButtonClassName,
                  "min-w-0",
                  Object.values(optionFieldErrors).some(Boolean) && "text-destructive hover:text-destructive",
                )}
                onClick={() => setOptionsSheetIsOpen(true)}
              >
                <SlidersHorizontal />
                <span className="truncate">{changedOptionWords.length > 0 ? `Options: ${changedOptionWords.join(", ")}` : "Options"}</span>
              </Button>
            ) : null}
          </div>
        </div>
        {accountSession ? (
          <InvoiceOptionsSheet
            isOpen={optionsSheetIsShown}
            onOpenChange={changeOptionsSheetIsOpen}
            options={invoiceOptions}
            onOptionsChange={changeInvoiceOptions}
            fieldErrors={optionFieldErrors}
            onFieldErrorClear={clearOptionFieldError}
            coins={networkCoins}
          />
        ) : null}
      </PageColumn>
    </form>
  )
}
