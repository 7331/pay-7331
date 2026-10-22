// What the account pages show signed out: a title (and one line, where it helps) and the passkey sign-in, with sign-up
// one tap away.
import { Fingerprint } from "lucide-react"
import { useState } from "react"

import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"
import { useAccountSession } from "@/hooks/use_account_session"
import { describePasskeyError, passkeysAreSupported, signInWithPasskey } from "@/lib/passkey_access"
import { primaryActionButtonClassName, touchTargetButtonClassName } from "@/lib/button_size_class_names"
import { cn } from "@/lib/utils"

export function SignInGate({ title, description }: { title: string; description?: string }) {
  const { setAccountSession, openAccountSheetOnSignUp } = useAccountSession()
  const [signingIn, setSigningIn] = useState(false)
  const [errorMessage, setErrorMessage] = useState("")

  async function signIn() {
    setErrorMessage("")
    setSigningIn(true)
    try {
      setAccountSession(await signInWithPasskey())
    } catch (signInError) {
      setErrorMessage(describePasskeyError(signInError))
    } finally {
      setSigningIn(false)
    }
  }

  return (
    <section className="flex flex-1 flex-col">
      <div className="mt-12">
        <h1 className="text-4xl font-semibold tracking-tight">{title}</h1>
        {description ? <p className="mt-2 text-[17px] text-muted-foreground">{description}</p> : null}
      </div>
      <div className="flex-1" />
      {errorMessage ? (
        <Alert variant="destructive" className="mb-3">
          <AlertDescription>{errorMessage}</AlertDescription>
        </Alert>
      ) : null}
      <Button id="gate_sign_in_button" className={cn(primaryActionButtonClassName, "w-full")} disabled={signingIn || !passkeysAreSupported()} onClick={() => void signIn()}>
        {signingIn ? <Spinner /> : <Fingerprint />} Sign in with passkey
      </Button>
      <Button id="gate_sign_up_button" variant="ghost" className={cn(touchTargetButtonClassName, "mt-2 w-full")} onClick={openAccountSheetOnSignUp}>
        New here? Create an account
      </Button>
    </section>
  )
}
