// "Options" on `/`, signed in only: the sheet with every invoice option `/` doesn't show (invoice_options.ts), a Drawer
// on a phone and a Dialog on a wide screen. Labels name the value; a hint stays only where the value doesn't explain
// itself (the shortfall, the order id, the contract tick); placeholders are examples. A wrong value shows under its
// field; "Create link" opens the sheet at the first one. "Repeat" turns the invoice into a schedule (invoice_options.ts). Each change applies at once; "Done" only closes it.
import { ResponsiveSheet } from "@/components/responsive_sheet"
import { TemporalInput } from "@/components/temporal_input"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { InputGroup, InputGroupAddon, InputGroupInput, InputGroupText } from "@/components/ui/input-group"
import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectSeparator, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import type { ChainAsset } from "@/lib/api_types"
import { primaryActionButtonClassName } from "@/lib/button_size_class_names"
import { cn } from "@/lib/utils"
import {
  fiatCurrencyCodes,
  lifetimePresets,
  maximumPayerQuantity,
  repeatIntervalChoices,
  type InvoiceOptionFieldErrors,
  type InvoiceOptionFieldName,
  type InvoiceOptions,
} from "@/pages/invoice_creation/invoice_options"

const inputClassName = "h-12 rounded-xl px-3.5 text-base"

export function InvoiceOptionsSheet({
  isOpen,
  onOpenChange,
  options,
  onOptionsChange,
  fieldErrors,
  onFieldErrorClear,
  coins,
}: {
  isOpen: boolean
  onOpenChange(nextIsOpen: boolean): void
  options: InvoiceOptions
  onOptionsChange(changedOptions: Partial<InvoiceOptions>): void
  fieldErrors: InvoiceOptionFieldErrors
  onFieldErrorClear(fieldName: InvoiceOptionFieldName): void
  // The coins a link can be priced in: the ticked networks' combined list.
  coins: ChainAsset[]
}) {
  function changeCheckedOption(fieldName: InvoiceOptionFieldName, changedOptions: Partial<InvoiceOptions>) {
    onOptionsChange(changedOptions)
    onFieldErrorClear(fieldName)
  }

  return (
    <ResponsiveSheet isOpen={isOpen} onOpenChange={onOpenChange} title="Options" sheetId="invoice_options_sheet">
      <FieldGroup className="gap-5">
        <div className="grid grid-cols-2 gap-3">
          <Field>
            <FieldLabel htmlFor="currency_select">Currency</FieldLabel>
            <Select value={options.currencyCode} onValueChange={(nextCurrencyCode) => onOptionsChange({ currencyCode: nextCurrencyCode })}>
              <SelectTrigger id="currency_select" className="h-12! w-full rounded-xl text-base">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  <SelectLabel>Money</SelectLabel>
                  {fiatCurrencyCodes.map((fiatCurrencyCode) => (
                    <SelectItem key={fiatCurrencyCode} value={fiatCurrencyCode} className="min-h-11">
                      {fiatCurrencyCode}
                    </SelectItem>
                  ))}
                </SelectGroup>
                {coins.length > 0 ? (
                  <>
                    <SelectSeparator />
                    <SelectGroup>
                      <SelectLabel>Coin</SelectLabel>
                      {coins.map((asset) => (
                        <SelectItem key={asset.symbol} value={asset.symbol} className="min-h-11">
                          {asset.symbol}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  </>
                ) : null}
              </SelectContent>
            </Select>
          </Field>
          <Field data-invalid={Boolean(fieldErrors.payerQuantity) || undefined}>
            <FieldLabel htmlFor="quantity_input">How many payers</FieldLabel>
            <Input
              id="quantity_input"
              type="number"
              inputMode="numeric"
              min={1}
              max={maximumPayerQuantity}
              step={1}
              value={options.payerQuantityText}
              aria-invalid={Boolean(fieldErrors.payerQuantity) || undefined}
              aria-describedby={fieldErrors.payerQuantity ? "quantity_error" : undefined}
              onChange={(changeEvent) => changeCheckedOption("payerQuantity", { payerQuantityText: changeEvent.target.value })}
              className={inputClassName}
            />
          </Field>
        </div>
        {/* The quantity's error spans both columns: it can be about the currency too. */}
        {fieldErrors.payerQuantity ? (
          <FieldError id="quantity_error" className="-mt-3">
            {fieldErrors.payerQuantity}
          </FieldError>
        ) : null}
        <Field data-invalid={Boolean(fieldErrors.repeatInterval) || undefined}>
          <FieldLabel htmlFor="repeat_interval_never">Repeat</FieldLabel>
          <ToggleGroup
            type="single"
            variant="outline"
            spacing={0}
            value={options.repeatInterval || "never"}
            onValueChange={(pickedValue) => {
              if (!pickedValue) return
              const repeatInterval = pickedValue === "never" ? "" : (pickedValue as InvoiceOptions["repeatInterval"])
              changeCheckedOption("repeatInterval", { repeatInterval })
            }}
            className="grid w-full grid-cols-4"
            aria-label="Repeat"
            aria-describedby={fieldErrors.repeatInterval ? "repeat_interval_error" : options.repeatInterval ? "repeat_interval_description" : undefined}
          >
            {repeatIntervalChoices.map((repeatChoice) => (
              <ToggleGroupItem
                key={repeatChoice.label}
                id={`repeat_interval_${repeatChoice.value || "never"}`}
                value={repeatChoice.value || "never"}
                className="h-11 text-[15px] font-medium data-[state=on]:bg-foreground data-[state=on]:text-background"
              >
                {repeatChoice.label}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
          {fieldErrors.repeatInterval ? (
            <FieldError id="repeat_interval_error">{fieldErrors.repeatInterval}</FieldError>
          ) : options.repeatInterval ? (
            <FieldDescription id="repeat_interval_description">
              A new invoice each time, from now, counted in UTC (monthly: the same UTC day each month); nothing is taken from the payer. Your webhook gets each one as schedule.invoice_created.
            </FieldDescription>
          ) : null}
        </Field>
        <Field data-invalid={Boolean(fieldErrors.lifetimeMinutes) || undefined}>
          <FieldLabel htmlFor="lifetime_minutes_input">Time to pay</FieldLabel>
          <ToggleGroup
            type="single"
            variant="outline"
            spacing={0}
            value={lifetimePresets.some((lifetimePreset) => lifetimePreset.minutes === options.lifetimeMinutesText) ? options.lifetimeMinutesText : ""}
            // Tapping the picked preset again goes back to the chain default.
            onValueChange={(presetMinutes) => changeCheckedOption("lifetimeMinutes", { lifetimeMinutesText: presetMinutes })}
            className="grid w-full grid-cols-5"
            aria-label="Time to pay presets"
          >
            {lifetimePresets.map((lifetimePreset) => (
              <ToggleGroupItem
                key={lifetimePreset.minutes}
                value={lifetimePreset.minutes}
                data-testid={`lifetime_preset_${lifetimePreset.minutes}`}
                className="h-11 text-[15px] font-medium data-[state=on]:bg-foreground data-[state=on]:text-background"
              >
                {lifetimePreset.label}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
          <InputGroup className="h-12 rounded-xl">
            <InputGroupInput
              id="lifetime_minutes_input"
              type="number"
              inputMode="numeric"
              min={5}
              max={720}
              step={1}
              aria-label="Time to pay in minutes"
              placeholder={options.repeatInterval ? "Chain default, else 12 h" : "Chain default"}
              aria-invalid={Boolean(fieldErrors.lifetimeMinutes) || undefined}
              aria-describedby={fieldErrors.lifetimeMinutes ? "lifetime_minutes_error" : undefined}
              value={options.lifetimeMinutesText}
              onChange={(changeEvent) => changeCheckedOption("lifetimeMinutes", { lifetimeMinutesText: changeEvent.target.value })}
              className="px-3.5 text-base"
            />
            <InputGroupAddon align="inline-end">
              <InputGroupText>minutes, 5 to 720</InputGroupText>
            </InputGroupAddon>
          </InputGroup>
          <FieldError id="lifetime_minutes_error">{fieldErrors.lifetimeMinutes}</FieldError>
        </Field>
        <Field data-invalid={Boolean(fieldErrors.returnUrl) || undefined}>
          <FieldLabel htmlFor="return_url_input">Back-to-shop link</FieldLabel>
          <Input
            id="return_url_input"
            type="url"
            autoComplete="off"
            placeholder="e.g. https://example.org/thanks"
            aria-invalid={Boolean(fieldErrors.returnUrl) || undefined}
            aria-describedby={fieldErrors.returnUrl ? "return_url_error" : undefined}
            value={options.returnUrl}
            onChange={(changeEvent) => changeCheckedOption("returnUrl", { returnUrl: changeEvent.target.value })}
            className={inputClassName}
          />
          <FieldError id="return_url_error">{fieldErrors.returnUrl}</FieldError>
        </Field>
        <Field>
          <FieldLabel htmlFor="underpaid_open_until_input">Top-ups open until</FieldLabel>
          <TemporalInput
            id="underpaid_open_until_input"
            type="datetime-local"
            value={options.underpaidOpenUntilLocal}
            onChange={(changeEvent) => changeCheckedOption("payerQuantity", { underpaidOpenUntilLocal: changeEvent.target.value })}
          />
        </Field>
        <Field data-invalid={Boolean(fieldErrors.accuracyPercent) || undefined}>
          <FieldLabel htmlFor="accuracy_percent_input">Accepted shortfall, %</FieldLabel>
          <Input
            id="accuracy_percent_input"
            type="number"
            inputMode="decimal"
            min={0}
            max={5}
            step={0.1}
            placeholder="Chain default"
            aria-invalid={Boolean(fieldErrors.accuracyPercent) || undefined}
            aria-describedby={fieldErrors.accuracyPercent ? "accuracy_percent_error" : "accuracy_percent_description"}
            value={options.accuracyPercentText}
            onChange={(changeEvent) => changeCheckedOption("accuracyPercent", { accuracyPercentText: changeEvent.target.value })}
            className={inputClassName}
          />
          {fieldErrors.accuracyPercent ? (
            <FieldError id="accuracy_percent_error">{fieldErrors.accuracyPercent}</FieldError>
          ) : (
            <FieldDescription id="accuracy_percent_description">Paid this much short still counts as paid. Empty: each network's default from Settings.</FieldDescription>
          )}
        </Field>
        <Field>
          <FieldLabel htmlFor="order_id_input">Order id</FieldLabel>
          <Input
            id="order_id_input"
            autoComplete="off"
            placeholder="e.g. 1042"
            aria-describedby="order_id_description"
            value={options.orderId}
            onChange={(changeEvent) => changeCheckedOption("payerQuantity", { orderId: changeEvent.target.value })}
            className={inputClassName}
          />
          <FieldDescription id="order_id_description">
            {options.repeatInterval ? "Each invoice's id is this plus the day, like 1042-20261021." : "The same id again opens that invoice."}
          </FieldDescription>
        </Field>
        <Field data-invalid={Boolean(fieldErrors.metadata) || undefined}>
          <FieldLabel htmlFor="metadata_input">Metadata, JSON</FieldLabel>
          <Textarea
            id="metadata_input"
            spellCheck={false}
            rows={2}
            placeholder='e.g. {"customer": "acme"}'
            aria-invalid={Boolean(fieldErrors.metadata) || undefined}
            aria-describedby={fieldErrors.metadata ? "metadata_error" : undefined}
            value={options.metadataText}
            onChange={(changeEvent) => changeCheckedOption("metadata", { metadataText: changeEvent.target.value })}
            className="min-h-12 rounded-xl px-3.5 py-3 font-mono text-sm"
          />
          <FieldError id="metadata_error">{fieldErrors.metadata}</FieldError>
        </Field>
        <Field orientation="horizontal" className="items-start">
          <Checkbox
            id="allow_contract_destination_checkbox"
            checked={options.allowContractDestination}
            onCheckedChange={(checkedState) => onOptionsChange({ allowContractDestination: checkedState === true })}
            aria-describedby="allow_contract_destination_description"
            className="mt-0.5 size-5 touch-target-hit-area"
          />
          <div className="flex flex-col gap-1">
            <FieldLabel htmlFor="allow_contract_destination_checkbox" className="font-normal">
              Pay to a smart-contract wallet (a Safe, say)
            </FieldLabel>
            <FieldDescription id="allow_contract_destination_description">
              Without this tick a contract address is refused, in case it can't take POL.
            </FieldDescription>
          </div>
        </Field>
      </FieldGroup>
      <Button
        id="invoice_options_done_button"
        type="button"
        variant="secondary"
        className={cn(primaryActionButtonClassName, "mt-6 w-full")}
        onClick={() => onOpenChange(false)}
      >
        Done
      </Button>
    </ResponsiveSheet>
  )
}
