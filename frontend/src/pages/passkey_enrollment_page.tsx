// `/enroll-passkey#<token>`: a one-time link the operator mints (`python -m app.cli mint-passkey-enrollment-link`) to add
// a passkey to an account without signing in, the account recovery path (2026-10-15). The token stays in the fragment,
// which the browser never sends. Opening the page reads the registration options, so a used or expired link says so
// before any prompt; "Add passkey" runs the same ceremony as adding one in Settings, then signs in and opens Settings.
import { KeyRound } from "lucide-react"
import { useEffect, useState } from "react"
import { useNavigate } from "react-router"
import { toast } from "sonner"

import { BrandMark } from "@/components/brand_mark"
import { PageColumn } from "@/components/page_column"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"
import { useAccountSession } from "@/hooks/use_account_session"
import { describeError } from "@/lib/api_client"
import { primaryActionButtonClassName } from "@/lib/button_size_class_names"
import {
  defaultPasskeyLabel,
  describePasskeyError,
  enrollPasskeyWithLink,
  passkeysAreSupported,
  readPasskeyEnrollmentOptions,
} from "@/lib/passkey_access"
import { cn } from "@/lib/utils"

type EnrollmentLinkState =
  | { kind: "loading" }
  | { kind: "ready"; optionsJson: unknown; username: string }
  | { kind: "refused"; message: string }

function readEnrollmentToken(): string {
  return window.location.hash.replace(/^#/, "")
}

function readOptionsUsername(optionsJson: unknown): string {
  const userName = (optionsJson as { user?: { name?: unknown } } | null)?.user?.name
  return typeof userName === "string" ? userName : ""
}

export function PasskeyEnrollmentPage() {
  const navigate = useNavigate()
  const { setAccountSession } = useAccountSession()
  const [enrollmentToken] = useState(readEnrollmentToken)
  const [linkState, setLinkState] = useState<EnrollmentLinkState>(() =>
    !enrollmentToken
      ? { kind: "refused", message: "This passkey link is incomplete. Open the whole link you were sent." }
      : !passkeysAreSupported()
        ? { kind: "refused", message: "This browser can't make passkeys. Open the link in Safari or Chrome." }
        : { kind: "loading" },
  )
  const [isEnrolling, setIsEnrolling] = useState(false)
  const [errorMessage, setErrorMessage] = useState("")

  // The state changes in the promise callbacks, once the API answers, never synchronously in the effect.
  useEffect(() => {
    if (linkState.kind !== "loading") return
    readPasskeyEnrollmentOptions(enrollmentToken).then(
      (optionsJson) => setLinkState({ kind: "ready", optionsJson, username: readOptionsUsername(optionsJson) }),
      (optionsError) => setLinkState({ kind: "refused", message: describeError(optionsError) }),
    )
  }, [enrollmentToken, linkState.kind])

  async function enrollPasskey() {
    if (linkState.kind !== "ready") return
    setIsEnrolling(true)
    setErrorMessage("")
    try {
      // A failed try spent its challenge; the next one asks for fresh options.
      const optionsJson = linkState.optionsJson ?? (await readPasskeyEnrollmentOptions(enrollmentToken))
      const accountSession = await enrollPasskeyWithLink(enrollmentToken, optionsJson, defaultPasskeyLabel())
      setAccountSession(accountSession)
      toast.success("Passkey added. You're signed in.")
      navigate("/settings", { replace: true })
    } catch (enrollError) {
      setLinkState({ ...linkState, optionsJson: null })
      setErrorMessage(describePasskeyError(enrollError))
      setIsEnrolling(false)
    }
  }

  return (
    <PageColumn
      topBar={
        <header className="flex min-h-14 items-center">
          <BrandMark />
        </header>
      }
      footer={
        linkState.kind === "ready" ? (
          <div className="flex flex-col gap-3">
            {errorMessage ? (
              <p id="passkey_enrollment_error" role="alert" className="text-center text-sm text-destructive">
                {errorMessage}
              </p>
            ) : null}
            <Button
              id="enroll_passkey_button"
              className={cn(primaryActionButtonClassName, "w-full")}
              disabled={isEnrolling}
              onClick={() => void enrollPasskey()}
            >
              {isEnrolling ? <Spinner /> : <KeyRound />}
              Add passkey
            </Button>
          </div>
        ) : null
      }
    >
      <section className="flex flex-1 flex-col items-center justify-center gap-2 pb-16 text-center">
        {linkState.kind === "loading" ? <Spinner className="size-6" /> : null}
        {linkState.kind === "ready" ? (
          <>
            <h1 className="text-3xl font-semibold tracking-tight">Add a passkey</h1>
            <p className="text-[15px] text-muted-foreground">
              {linkState.username ? `For @${linkState.username}. ` : ""}This link works once.
            </p>
          </>
        ) : null}
        {linkState.kind === "refused" ? (
          <>
            <h1 className="text-3xl font-semibold tracking-tight">Can't use this link</h1>
            <p id="passkey_enrollment_refusal" className="text-[15px] text-muted-foreground">
              {linkState.message}
            </p>
          </>
        ) : null}
      </section>
    </PageColumn>
  )
}
