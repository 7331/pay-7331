// Where the money goes, as `/`, the account sheet's sign-up and `/settings`' payout address change ask for it: the whole
// address on up to two mono lines (a Textarea that grows, never a one-line input that clips it), Paste inside the field
// while it's empty and Clear once it has text, the error under it. Spaces and line breaks in a paste are dropped; Enter
// submits the form like a one-line input. `labelIsHidden` keeps the label for screen readers only, where the
// placeholder and the page already say what the field is (`/`).
import { ClipboardPaste, X } from "lucide-react"
import type { KeyboardEvent, ReactNode } from "react"

import { Field, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field"
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupTextarea } from "@/components/ui/input-group"
import { addressPattern } from "@/lib/amount_formatting"

const clipboardCanBeRead = typeof navigator !== "undefined" && typeof navigator.clipboard?.readText === "function"

export function ReceiverAddressField({
  inputId,
  errorId,
  pasteButtonId,
  label,
  labelIsHidden = false,
  focusesOnMount = false,
  placeholder,
  addressText,
  errorText,
  description,
  onAddressTextChange,
  onErrorTextChange,
}: {
  inputId: string
  errorId: string
  pasteButtonId: string
  label: string
  labelIsHidden?: boolean
  // Set when a tap revealed the field, so typing can start at once.
  focusesOnMount?: boolean
  placeholder: string
  addressText: string
  errorText: string
  description?: ReactNode
  onAddressTextChange(nextAddressText: string): void
  onErrorTextChange(nextErrorText: string): void
}) {
  const addressLooksWrong = Boolean(addressText.trim()) && !addressPattern.test(addressText.trim())

  function changeAddressText(nextAddressText: string) {
    // An address has no spaces or line breaks; a paste with them still lands whole.
    onAddressTextChange(nextAddressText.replace(/\s+/g, ""))
    onErrorTextChange("")
  }

  async function pasteAddressText() {
    try {
      changeAddressText(await navigator.clipboard.readText())
    } catch {
      onErrorTextChange("The browser didn't allow reading the clipboard: long-press the field and paste.")
    }
  }

  // Enter submits like a one-line input; the address box only wraps, it never takes a new line.
  function submitOnEnterKey(keyboardEvent: KeyboardEvent<HTMLTextAreaElement>) {
    if (keyboardEvent.key !== "Enter" || keyboardEvent.nativeEvent.isComposing) return
    keyboardEvent.preventDefault()
    keyboardEvent.currentTarget.form?.requestSubmit()
  }

  return (
    <Field data-invalid={Boolean(errorText) || undefined}>
      <FieldLabel htmlFor={inputId} className={labelIsHidden ? "sr-only" : undefined}>
        {label}
      </FieldLabel>
      <InputGroup className="min-h-12 rounded-xl">
        <InputGroupTextarea
          id={inputId}
          rows={1}
          autoFocus={focusesOnMount}
          autoComplete="off"
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          placeholder={placeholder}
          value={addressText}
          aria-invalid={Boolean(errorText) || addressLooksWrong}
          aria-describedby={errorText ? errorId : undefined}
          onChange={(changeEvent) => changeAddressText(changeEvent.target.value)}
          onKeyDown={submitOnEnterKey}
          className="min-h-0 px-3.5 py-3 font-mono text-[15px] leading-6 break-all placeholder:font-sans placeholder:text-muted-foreground"
        />
        <InputGroupAddon align="inline-end">
          {addressText ? (
            <InputGroupButton size="icon-sm" className="size-11 rounded-lg" aria-label="Clear the address" onClick={() => changeAddressText("")}>
              <X />
            </InputGroupButton>
          ) : clipboardCanBeRead ? (
            <InputGroupButton id={pasteButtonId} size="sm" className="h-11 rounded-lg px-3" onClick={() => void pasteAddressText()}>
              <ClipboardPaste /> Paste
            </InputGroupButton>
          ) : null}
        </InputGroupAddon>
      </InputGroup>
      {description ? <FieldDescription>{description}</FieldDescription> : null}
      <FieldError id={errorId}>{errorText}</FieldError>
    </Field>
  )
}
