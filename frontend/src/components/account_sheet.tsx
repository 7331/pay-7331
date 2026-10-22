// The account sheet on every account page (PLAN.md section 10), opened from the top bar's "Sign in" or "@username": a
// Drawer on a phone, a Dialog on a wide screen (shadcn's responsive pattern, https://ui.shadcn.com/docs/components/drawer).
// Signed out it is passkey first: "Sign in with passkey", and under it "New here?", which shows the sign-up form
// (username, payout address starting from the receiver typed on `/`, "Create account with passkey"); a sign-in or
// sign-up closes it. Signed in it says who and offers "Sign out"; Activity and Settings sit in the bottom tab bar
// (components/account_tab_bar.tsx). A passkey and its HttpOnly session cookie are the only way in.
import { Fingerprint, LogOut } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { useState, type FormEvent, type ReactNode } from "react"

import { ReceiverAddressField } from "@/components/receiver_address_field"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { Drawer, DrawerContent, DrawerTitle, DrawerTrigger } from "@/components/ui/drawer"
import { Field, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field"
import { InputGroup, InputGroupAddon, InputGroupInput } from "@/components/ui/input-group"
import { Spinner } from "@/components/ui/spinner"
import { useAccountSession } from "@/hooks/use_account_session"
import { useMediaQuery } from "@/hooks/use_media_query"
import { addressPattern, shortenHexString } from "@/lib/amount_formatting"
import { ApiError } from "@/lib/api_client"
import {
  defaultPasskeyLabel,
  describePasskeyError,
  passkeysAreSupported,
  signInWithPasskey,
  signOutAccountSession,
  signUpWithPasskey,
  usernamePattern,
} from "@/lib/passkey_access"
import { cn } from "@/lib/utils"
import { primaryActionButtonClassName, touchTargetButtonClassName } from "@/lib/button_size_class_names"

// shadcn's `md` breakpoint, as in its responsive Dialog/Drawer example.
const wideScreenMediaQuery = "(min-width: 768px)"

export function AccountSheetTrigger() {
  const { accountSession, sessionIsLoading, accountSheetIsOpen, setAccountSheetIsOpen } = useAccountSession()
  const isWideScreen = useMediaQuery(wideScreenMediaQuery)
  const triggerButton = (
    <Button
      id="account_sheet_trigger"
      data-testid="account_sheet_trigger"
      variant={accountSession ? "secondary" : "ghost"}
      disabled={sessionIsLoading}
      className={cn(touchTargetButtonClassName, "max-w-[11rem] truncate", !accountSession && "-mr-2")}
    >
      {accountSession ? `@${accountSession.username}` : "Sign in"}
    </Button>
  )
  // The title is for screen readers: the sheet's buttons say what it is for. No description, on purpose
  // (https://www.radix-ui.com/primitives/docs/components/dialog#description).
  const sheetTitle = accountSession ? "Your account" : "Sign in or create an account"
  const sheetBody = accountSession ? <SignedInAccountSummary /> : <PasskeySignInOrSignUp />

  if (isWideScreen) {
    return (
      <Dialog open={accountSheetIsOpen} onOpenChange={setAccountSheetIsOpen}>
        <DialogTrigger asChild>{triggerButton}</DialogTrigger>
        <DialogContent aria-describedby={undefined} className="max-h-[90dvh] gap-0 overflow-y-auto rounded-3xl p-7 pt-12 sm:max-w-[400px]">
          <DialogTitle className="sr-only">{sheetTitle}</DialogTitle>
          {sheetBody}
        </DialogContent>
      </Dialog>
    )
  }
  return (
    <Drawer open={accountSheetIsOpen} onOpenChange={setAccountSheetIsOpen}>
      <DrawerTrigger asChild>{triggerButton}</DrawerTrigger>
      <DrawerContent aria-describedby={undefined}>
        <DrawerTitle className="sr-only">{sheetTitle}</DrawerTitle>
        <div className="mx-auto w-full max-w-[440px] overflow-y-auto px-5 pt-6 pb-[max(env(safe-area-inset-bottom),1.25rem)]">
          {sheetBody}
        </div>
      </DrawerContent>
    </Drawer>
  )
}

function SheetErrorAlert({ children }: { children: ReactNode }) {
  return (
    <Alert variant="destructive" className="mb-4">
      <AlertDescription>{children}</AlertDescription>
    </Alert>
  )
}

function PasskeySignInOrSignUp() {
  const { setAccountSession, setAccountSheetIsOpen, accountSheetStartsOnSignUp, signUpPayoutAddressSuggestion } = useAccountSession()
  const [showsSignUpForm, setShowsSignUpForm] = useState(accountSheetStartsOnSignUp)
  const [username, setUsername] = useState("")
  const [usernameError, setUsernameError] = useState("")
  const [payoutAddress, setPayoutAddress] = useState(signUpPayoutAddressSuggestion)
  const [payoutAddressError, setPayoutAddressError] = useState("")
  // Which button's passkey prompt is open, so only that one shows the spinner.
  const [openPasskeyStep, setOpenPasskeyStep] = useState<"sign_in" | "sign_up" | null>(null)
  const [passkeyErrorMessage, setPasskeyErrorMessage] = useState("")
  const supported = passkeysAreSupported()

  async function runPasskeyStep(
    passkeyStepName: "sign_in" | "sign_up",
    passkeyStep: () => Promise<Awaited<ReturnType<typeof signInWithPasskey>>>,
  ) {
    setPasskeyErrorMessage("")
    setOpenPasskeyStep(passkeyStepName)
    try {
      setAccountSession(await passkeyStep())
      setAccountSheetIsOpen(false)
    } catch (passkeyError) {
      setPasskeyErrorMessage(describePasskeyError(passkeyError))
    } finally {
      setOpenPasskeyStep(null)
    }
  }

  function submitSignUp(submitEvent: FormEvent) {
    submitEvent.preventDefault()
    // The sheet is portaled, but React still bubbles its submit to a form around the trigger (the `/` form).
    submitEvent.stopPropagation()
    const trimmedUsername = username.trim()
    const trimmedPayoutAddress = payoutAddress.trim()
    // A wrong value shows under its own field; only an API refusal shows by the button.
    const nextUsernameError = usernamePattern.test(trimmedUsername) ? "" : "Pick a username of 3 to 20 letters, digits or _."
    const nextPayoutAddressError = addressPattern.test(trimmedPayoutAddress)
      ? ""
      : "The payout address must be 0x followed by 40 hex characters."
    setUsernameError(nextUsernameError)
    setPayoutAddressError(nextPayoutAddressError)
    if (nextUsernameError || nextPayoutAddressError) {
      document.getElementById(nextUsernameError ? "sign_up_username_input" : "sign_up_payout_address_input")?.focus()
      return
    }
    void runPasskeyStep("sign_up", () => signUpWithPasskey(trimmedUsername, trimmedPayoutAddress, defaultPasskeyLabel()))
  }

  const passkeyButtonContent = (passkeyStepName: "sign_in" | "sign_up", idleText: string) => (
    <>
      {openPasskeyStep === passkeyStepName ? <Spinner /> : <Fingerprint />}
      {openPasskeyStep === passkeyStepName ? "Confirm with your passkey" : idleText}
    </>
  )

  return (
    <div className="flex flex-col">
      {!supported ? <SheetErrorAlert>This browser can't use passkeys here. Open this page in one that can.</SheetErrorAlert> : null}
      {passkeyErrorMessage && !showsSignUpForm ? <SheetErrorAlert>{passkeyErrorMessage}</SheetErrorAlert> : null}
      <Button
        id="passkey_sign_in_button"
        type="button"
        variant={showsSignUpForm ? "outline" : "default"}
        className={cn(primaryActionButtonClassName, "w-full")}
        disabled={!supported || openPasskeyStep !== null}
        onClick={() => void runPasskeyStep("sign_in", signInWithPasskey)}
      >
        {passkeyButtonContent("sign_in", "Sign in with passkey")}
      </Button>
      {/* The sign-up form grows in under "New here?"; overflow hidden for the height animation, with room for the
          fields' focus rings inside the clip (as the note field on `/`). */}
      <AnimatePresence initial={false} mode="wait">
        {showsSignUpForm ? (
          <motion.div
            key="sign_up_form"
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            transition={{ type: "spring", visualDuration: 0.35, bounce: 0 }}
            className="-mx-1.5 overflow-hidden"
          >
            <form onSubmit={submitSignUp} noValidate className="flex flex-col px-1.5 pt-6 pb-1.5">
              <FieldGroup className="gap-4">
                <Field data-invalid={Boolean(usernameError) || undefined}>
                  <FieldLabel htmlFor="sign_up_username_input">Username</FieldLabel>
                  <InputGroup className="h-12 rounded-xl">
                    <InputGroupAddon className="pl-3.5 text-base">@</InputGroupAddon>
                    <InputGroupInput
                      id="sign_up_username_input"
                      maxLength={20}
                      autoComplete="username"
                      spellCheck={false}
                      autoCapitalize="off"
                      autoFocus
                      placeholder="yourname"
                      aria-invalid={Boolean(usernameError) || undefined}
                      aria-describedby={usernameError ? "sign_up_username_error" : undefined}
                      value={username}
                      onChange={(changeEvent) => {
                        setUsername(changeEvent.target.value)
                        setUsernameError("")
                      }}
                      className="text-base"
                    />
                  </InputGroup>
                  <FieldError id="sign_up_username_error">{usernameError}</FieldError>
                </Field>
                <ReceiverAddressField
                  inputId="sign_up_payout_address_input"
                  errorId="sign_up_payout_address_error"
                  pasteButtonId="sign_up_paste_payout_address_button"
                  label="Payout address"
                  placeholder="0x… your wallet address"
                  addressText={payoutAddress}
                  errorText={payoutAddressError}
                  onAddressTextChange={(nextPayoutAddress) => {
                    setPayoutAddress(nextPayoutAddress)
                    setPasskeyErrorMessage("")
                  }}
                  onErrorTextChange={setPayoutAddressError}
                />
              </FieldGroup>
              {passkeyErrorMessage ? (
                <Alert variant="destructive" className="mt-4">
                  <AlertDescription>{passkeyErrorMessage}</AlertDescription>
                </Alert>
              ) : null}
              <Button id="sign_up_button" type="submit" className={cn(primaryActionButtonClassName, "mt-6 w-full")} disabled={!supported || openPasskeyStep !== null}>
                {passkeyButtonContent("sign_up", "Create account with passkey")}
              </Button>
            </form>
          </motion.div>
        ) : (
          <motion.div key="new_here_button" exit={{ opacity: 0 }} transition={{ duration: 0.1 }}>
            <Button
              id="show_sign_up_form_button"
              type="button"
              variant="ghost"
              className={cn(touchTargetButtonClassName, "mt-2 w-full text-muted-foreground")}
              onClick={() => {
                setPasskeyErrorMessage("")
                setShowsSignUpForm(true)
              }}
            >
              New here? Create an account
            </Button>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

function SignedInAccountSummary() {
  const { accountSession, setAccountSession, setAccountSheetIsOpen } = useAccountSession()
  const [signingOut, setSigningOut] = useState(false)
  const [signOutErrorMessage, setSignOutErrorMessage] = useState("")
  if (!accountSession) return null

  async function signOut() {
    setSigningOut(true)
    setSignOutErrorMessage("")
    try {
      await signOutAccountSession()
    } catch (signOutError) {
      // A 401 means the session had already ended; anything else leaves the cookie in place, so the sheet says so.
      if (!(signOutError instanceof ApiError && signOutError.httpStatus === 401)) {
        setSignOutErrorMessage(describePasskeyError(signOutError))
        setSigningOut(false)
        return
      }
    }
    setAccountSession(null)
    setAccountSheetIsOpen(false)
  }

  return (
    <div className="flex flex-col">
      <p id="account_access_state" className="text-lg font-semibold tracking-tight">
        Signed in as @{accountSession.username}
      </p>
      <p className="mt-1 text-[15px] text-muted-foreground">
        Paid to <span className="font-mono whitespace-nowrap">{shortenHexString(accountSession.payout_address, 6)}</span>
      </p>
      {signOutErrorMessage ? (
        <Alert variant="destructive" className="mt-4">
          <AlertDescription>{signOutErrorMessage}</AlertDescription>
        </Alert>
      ) : null}
      <Button
        id="sign_out_button"
        variant="outline"
        className={cn(touchTargetButtonClassName, "mt-6 w-full text-destructive")}
        disabled={signingOut}
        onClick={() => void signOut()}
      >
        {signingOut ? <Spinner /> : <LogOut />} Sign out
      </Button>
    </div>
  )
}
