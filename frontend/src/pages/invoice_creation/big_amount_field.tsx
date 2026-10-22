// The big amount on `/`: the number itself is the field (a dashed underline that turns solid while typing), its
// currency's sign before it or its coin's symbol after it, and, priced in money, $1/$2/$5/$10 as small round chips
// centered under it. A tapped chip counts the number up or down to its value; typing stops the
// count. Up to 9 whole digits and the currency's decimals; anything typed beyond them is dropped and the field says
// why under itself.
import { useLayoutEffect, useRef, type ChangeEvent } from "react"

import { SlidingSelectionPill } from "@/components/sliding_selection_pill"
import { FieldError } from "@/components/ui/field"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { useCountingNumberText } from "@/hooks/use_counting_number_text"
import { cn } from "@/lib/utils"
import type { AmountUnit } from "@/pages/invoice_creation/amount_unit"

// Up to 9 whole digits (under a billion), so "1234567.89" fits.
const maximumWholeDigits = 9
// The page's own commas group digits ("1,234.50"), so commas in the field are dropped, except a "," or "." just typed,
// which is the decimal point, and a pasted or filled "12,50" (one comma, one or two digits after, no dot), a decimal
// comma. `insertedText` is the InputEvent's `data` (null for a paste in Chrome), `caretPosition` the caret after it.
function readTypedAmountText(typedText: string, inputType: string, insertedText: string | null, caretPosition: number | null): string {
  if (inputType === "insertText" && (insertedText === "," || insertedText === ".") && caretPosition !== null) {
    return typedText.slice(0, caretPosition - 1).replace(/,/g, "") + "." + typedText.slice(caretPosition).replace(/,/g, "")
  }
  const isWholeInsertion = inputType.startsWith("insert") && (insertedText === null || insertedText.length > 1)
  if (isWholeInsertion && /^\s*\d+,\d{1,2}\s*$/.test(typedText)) return typedText.trim().replace(",", ".")
  return typedText.replace(/,/g, "")
}

// Keeps digits and one dot and cuts the whole and fraction parts to their limits; `wasCut` says whether anything typed
// was dropped, so the field can say why.
function sanitizeTypedAmount(typedText: string, fractionDigits: number): { amountText: string; wasCut: boolean } {
  const digitsAndDots = typedText.replace(/[^0-9.]/g, "")
  const [wholePart, ...fractionParts] = digitsAndDots.split(".")
  const hasDot = fractionParts.length > 0
  const fractionPart = fractionParts.join("")
  const amountText = wholePart.slice(0, maximumWholeDigits) + (hasDot ? "." + fractionPart.slice(0, fractionDigits) : "")
  return { amountText, wasCut: amountText !== digitsAndDots }
}

// "1234567.5" reads "1,234,567.5": a comma every three whole digits, display only.
function groupWholeDigits(amountText: string): string {
  const [wholePart, ...fractionParts] = amountText.split(".")
  const groupedWholePart = wholePart.replace(/\B(?=(\d{3})+(?!\d))/g, ",")
  return fractionParts.length > 0 ? `${groupedWholePart}.${fractionParts.join("")}` : groupedWholePart
}

// Where the caret goes to sit after `characterCount` characters that aren't grouping commas.
function findCaretAfterCharacters(groupedText: string, characterCount: number): number {
  let seenCharacters = 0
  for (let position = 0; position < groupedText.length; position++) {
    if (seenCharacters === characterCount) return position
    if (groupedText[position] !== ",") seenCharacters++
  }
  return groupedText.length
}

// The input's width in em: tabular digits about 0.6em, the dot and commas about 0.3em, plus room for the caret.
function estimateAmountWidthEm(groupedText: string): number {
  if (!groupedText) return 0.7
  return [...groupedText].reduce((widthEm, character) => widthEm + (character === "," || character === "." ? 0.3 : 0.6), 0.1)
}

// The big amount shrinks as it grows, so up to "999,999,999.99" stays on one line in a 320 px column.
function pickAmountFontSizeClassName(amountLength: number): string {
  if (amountLength <= 5) return "text-7xl"
  if (amountLength <= 7) return "text-6xl"
  if (amountLength <= 9) return "text-5xl"
  if (amountLength <= 14) return "text-4xl"
  return "text-3xl"
}

export function BigAmountField({
  amountText,
  amountUnit,
  amountError,
  onAmountTextChange,
  onAmountErrorChange,
}: {
  amountText: string
  amountUnit: AmountUnit
  amountError: string
  onAmountTextChange(nextAmountText: string): void
  onAmountErrorChange(nextAmountError: string): void
}) {
  const { countingText, countBetweenNumbers, stopCounting } = useCountingNumberText()
  const rawShownAmountText = countingText ?? amountText
  const shownAmountText = groupWholeDigits(rawShownAmountText)
  const amountInputElement = useRef<HTMLInputElement>(null)
  // The caret as a count of non-comma characters, put back after the grouping commas move (null: leave it be).
  const pendingCaretCharacterCount = useRef<number | null>(null)
  useLayoutEffect(() => {
    if (pendingCaretCharacterCount.current === null || !amountInputElement.current) return
    const caretPosition = findCaretAfterCharacters(amountInputElement.current.value, pendingCaretCharacterCount.current)
    amountInputElement.current.setSelectionRange(caretPosition, caretPosition)
    pendingCaretCharacterCount.current = null
  })
  const pickedQuickAmount =
    amountUnit.quickAmounts.find((quickAmount) => amountText !== "" && Number(quickAmount) === Number(amountText)) ?? ""
  const amountFontSizeClassName = pickAmountFontSizeClassName(rawShownAmountText.length)

  function changeTypedAmount(changeEvent: ChangeEvent<HTMLInputElement>) {
    stopCounting()
    const inputEvent = changeEvent.nativeEvent as InputEvent
    const caretPosition = changeEvent.target.selectionStart
    const typedAmountText = readTypedAmountText(changeEvent.target.value, inputEvent.inputType ?? "", inputEvent.data ?? null, caretPosition)
    const { amountText: sanitizedAmountText, wasCut } = sanitizeTypedAmount(typedAmountText, amountUnit.fractionDigits)
    if (caretPosition !== null) {
      const typedCharactersBeforeCaret = changeEvent.target.value.slice(0, caretPosition).replace(/,/g, "").length
      pendingCaretCharacterCount.current = Math.min(typedCharactersBeforeCaret, sanitizedAmountText.length)
    }
    onAmountTextChange(sanitizedAmountText)
    onAmountErrorChange(wasCut ? amountUnit.cutMessage : "")
  }

  return (
    <section className="flex flex-col items-center pt-8 pb-2">
      {/* The amount looks like a field: a dashed underline that turns solid, with the focus ring, while typing; a tap
          anywhere on it, the sign or symbol too, puts the caret in. */}
      <label
        className={cn(
          "flex max-w-full cursor-text items-baseline justify-center rounded-none border-b-2 border-dashed border-input px-3 font-semibold tracking-tighter tabular-nums",
          "transition-[border-color,box-shadow] focus-within:rounded-2xl focus-within:border-solid focus-within:border-ring focus-within:ring-[3px] focus-within:ring-ring/50",
          amountError && "border-destructive",
        )}
      >
        {amountUnit.sign ? (
          <span aria-hidden="true" className="text-5xl text-muted-foreground">
            {amountUnit.sign}
          </span>
        ) : null}
        {/* The field always holds the real amount: a count frame written into a focused input moves the caret and
            drops a selection. While counting, the field's text is transparent under a span that shows the frames
            (2026-10-12). */}
        <span className="relative inline-flex">
          <input
            id="amount_input"
            ref={amountInputElement}
            aria-label={`Amount in ${amountUnit.spokenName}`}
            inputMode="decimal"
            autoComplete="off"
            value={groupWholeDigits(amountText)}
            placeholder="0"
            aria-invalid={Boolean(amountError)}
            aria-describedby={amountError ? "amount_error" : undefined}
            onChange={changeTypedAmount}
            onFocus={stopCounting}
            style={{ width: `${estimateAmountWidthEm(shownAmountText)}em` }}
            className={cn(
              "min-w-[1ch] bg-transparent outline-none placeholder:text-muted-foreground",
              amountFontSizeClassName,
              countingText !== null && "text-transparent placeholder:text-transparent",
            )}
          />
          {countingText !== null ? (
            <span
              aria-hidden="true"
              data-testid="amount_counting_text"
              className={cn("pointer-events-none absolute inset-0 flex items-center", amountFontSizeClassName)}
            >
              {shownAmountText}
            </span>
          ) : null}
        </span>
        {amountUnit.symbol ? (
          <span aria-hidden="true" className="ml-2 text-3xl tracking-tight text-muted-foreground">
            {amountUnit.symbol}
          </span>
        ) : null}
      </label>
      <FieldError id="amount_error" className="mt-2 text-center">
        {amountError}
      </FieldError>
      {amountUnit.quickAmounts.length > 0 ? (
        <ToggleGroup
          type="single"
          spacing={2}
          value={pickedQuickAmount}
          onValueChange={(quickAmount) => {
            if (!quickAmount) return
            countBetweenNumbers(Number(rawShownAmountText) || 0, Number(quickAmount))
            onAmountTextChange(quickAmount)
            onAmountErrorChange("")
          }}
          className="mt-5 justify-center"
          aria-label="Quick amounts"
        >
          {amountUnit.quickAmounts.map((quickAmount) => (
            <ToggleGroupItem
              key={quickAmount}
              value={quickAmount}
              data-testid={`quick_amount_${quickAmount}`}
              className={cn(
                "relative h-9 min-w-12 touch-target-hit-area rounded-full bg-muted px-3.5 text-[15px] font-medium tabular-nums",
                "hover:bg-muted data-[state=on]:bg-transparent data-[state=on]:text-background",
              )}
            >
              {pickedQuickAmount === quickAmount ? <SlidingSelectionPill layoutId="quick_amount_selected_pill" borderRadius={999} className="inset-0" /> : null}
              <span className="relative">
                {amountUnit.sign}
                {quickAmount}
              </span>
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
      ) : null}
    </section>
  )
}
