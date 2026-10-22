// `/settings`' "API tokens" row (count) and its sheet (2026-10-12, rows 2026-10-14): tokens the account's own server
// sends as `Authorization: Bearer pay_tok_...` on the invoice routes. The sheet lists name, made and last used, and
// revokes after an AlertDialog; "Make an API token" turns it into a name field and a fresh passkey check, then shows
// the token once until "Done". Each acts at once.
import { ArrowLeft, Fingerprint, KeyRound, Plus, Trash2 } from "lucide-react"
import { useCallback, useEffect, useState, type FormEvent } from "react"
import { toast } from "sonner"

import { AccountSettingsRow } from "@/components/account_settings_row"
import { ResponsiveSheet } from "@/components/responsive_sheet"
import { TapToCopyButton } from "@/components/tap_to_copy_button"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Empty, EmptyHeader, EmptyTitle } from "@/components/ui/empty"
import { Field, FieldError, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Item, ItemActions, ItemContent, ItemDescription, ItemGroup, ItemMedia, ItemTitle } from "@/components/ui/item"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { useAccountSession } from "@/hooks/use_account_session"
import { ApiError, describeError, fetchApiJson } from "@/lib/api_client"
import type { AccountApiToken, CreatedAccountApiToken } from "@/lib/api_types"
import { primaryActionButtonClassName, touchTargetButtonClassName } from "@/lib/button_size_class_names"
import { createApiTokenWithPasskey, describePasskeyError, passkeysAreSupported } from "@/lib/passkey_access"
import { formatShortTime } from "@/lib/time_formatting"
import { cn } from "@/lib/utils"

// What the sheet shows: the list, the new token's name and passkey check, or the token just made.
type ApiTokenSheetStep = "list" | "naming" | "created"

export function AccountApiTokenSettings() {
  const { refreshAccountSession } = useAccountSession()
  const [apiTokens, setApiTokens] = useState<AccountApiToken[] | null>(null)
  const [apiTokenListError, setApiTokenListError] = useState("")
  const [sheetIsOpen, setSheetIsOpen] = useState(false)
  const [sheetStep, setSheetStep] = useState<ApiTokenSheetStep>("list")
  const [createdApiToken, setCreatedApiToken] = useState<CreatedAccountApiToken | null>(null)
  const [passkeyPromptIsOpen, setPasskeyPromptIsOpen] = useState(false)

  // The state changes in the promise callbacks, once the API answers, never synchronously in the effect below.
  const reloadApiTokens = useCallback(
    () =>
      fetchApiJson<{ api_tokens: AccountApiToken[] }>("/api/v1/account-api-tokens").then(
        (apiTokenList) => {
          setApiTokens(apiTokenList.api_tokens)
          setApiTokenListError("")
        },
        (listFailure: unknown) => setApiTokenListError(describeError(listFailure)),
      ),
    [],
  )

  useEffect(() => {
    void reloadApiTokens()
  }, [reloadApiTokens])

  function changeSheetOpenState(nextIsOpen: boolean) {
    if (passkeyPromptIsOpen) return
    setSheetIsOpen(nextIsOpen)
    if (!nextIsOpen) {
      // The token shows once: closing the sheet drops it.
      setSheetStep("list")
      setCreatedApiToken(null)
    }
  }

  async function revokeApiToken(apiToken: AccountApiToken) {
    try {
      await fetchApiJson<unknown>(`/api/v1/account-api-tokens/${apiToken.id}`, { method: "DELETE" })
      toast.success(`Token "${apiToken.name}" revoked.`)
      await reloadApiTokens()
    } catch (revokeFailure) {
      if (revokeFailure instanceof ApiError && revokeFailure.httpStatus === 401) await refreshAccountSession()
      else toast.error(describeError(revokeFailure))
    }
  }

  const sheetTitles: Record<ApiTokenSheetStep, string> = { list: "API tokens", naming: "Make an API token", created: "Copy your token" }
  return (
    <>
      <AccountSettingsRow
        id="settings_api_tokens_row"
        title="API tokens"
        valueText={apiTokenListError ? "Can't load" : apiTokens === null ? "…" : apiTokens.length || "None"}
        valueTone={apiTokenListError ? "problem" : "default"}
        onOpen={() => setSheetIsOpen(true)}
      />
      <ResponsiveSheet
        isOpen={sheetIsOpen}
        onOpenChange={changeSheetOpenState}
        title={sheetTitles[sheetStep]}
        description={sheetStep === "created" ? "This is the only time it shows. Put it in your server's settings now." : undefined}
        testId="settings_api_tokens_sheet"
      >
        {sheetStep === "created" && createdApiToken ? (
          <div className="flex flex-col">
            <TapToCopyButton
              id="settings_created_api_token"
              textToCopy={createdApiToken.token}
              accessibleLabel="Copy the API token"
              className="rounded-xl border px-3.5 py-3 text-left font-mono text-[15px] break-all"
            >
              {createdApiToken.token}
            </TapToCopyButton>
            <Button
              id="settings_api_token_done_button"
              type="button"
              className={cn(primaryActionButtonClassName, "mt-8 w-full")}
              onClick={() => {
                toast.success(`Token "${createdApiToken.name}" made.`)
                setCreatedApiToken(null)
                setSheetStep("list")
              }}
            >
              Done
            </Button>
          </div>
        ) : sheetStep === "naming" ? (
          <ApiTokenNamingForm
            onPasskeyPromptChange={setPasskeyPromptIsOpen}
            onCreated={(nextCreatedApiToken) => {
              setCreatedApiToken(nextCreatedApiToken)
              setSheetStep("created")
              void reloadApiTokens()
            }}
            onBack={() => setSheetStep("list")}
          />
        ) : (
          <div className="flex flex-col gap-3">
            {apiTokenListError ? (
              <Alert variant="destructive">
                <AlertDescription>Can't load your API tokens: {apiTokenListError}</AlertDescription>
              </Alert>
            ) : apiTokens === null ? (
              <Skeleton className="h-16 w-full rounded-2xl" />
            ) : apiTokens.length ? (
              <ItemGroup className="gap-2">
                {apiTokens.map((apiToken) => (
                  <Item key={apiToken.id} variant="outline" className="min-h-14 rounded-2xl" data-testid="settings_api_token_row">
                    <ItemMedia variant="icon">
                      <KeyRound />
                    </ItemMedia>
                    <ItemContent className="min-w-0">
                      <ItemTitle className="break-words">{apiToken.name}</ItemTitle>
                      <ItemDescription>
                        Made {formatShortTime(apiToken.created_at)}
                        {apiToken.last_used_at ? `, last used ${formatShortTime(apiToken.last_used_at)}` : ", never used"}
                      </ItemDescription>
                    </ItemContent>
                    <ItemActions className="gap-0">
                      <AlertDialog>
                        <AlertDialogTrigger asChild>
                          <Button
                            type="button"
                            variant="ghost"
                            className={touchTargetButtonClassName}
                            aria-label={`Revoke token ${apiToken.name}`}
                            data-testid="settings_revoke_api_token_button"
                          >
                            <Trash2 />
                          </Button>
                        </AlertDialogTrigger>
                        <AlertDialogContent>
                          <AlertDialogHeader>
                            <AlertDialogTitle>Revoke "{apiToken.name}"?</AlertDialogTitle>
                            <AlertDialogDescription>
                              A server using it can't create or read invoices anymore. Invoices it made stay as they are.
                            </AlertDialogDescription>
                          </AlertDialogHeader>
                          <AlertDialogFooter>
                            <AlertDialogCancel className={touchTargetButtonClassName}>Keep it</AlertDialogCancel>
                            <AlertDialogAction
                              id="settings_confirm_revoke_api_token_button"
                              variant="destructive"
                              className={touchTargetButtonClassName}
                              onClick={() => void revokeApiToken(apiToken)}
                            >
                              Revoke
                            </AlertDialogAction>
                          </AlertDialogFooter>
                        </AlertDialogContent>
                      </AlertDialog>
                    </ItemActions>
                  </Item>
                ))}
              </ItemGroup>
            ) : (
              <Empty className="p-4">
                <EmptyHeader>
                  <EmptyTitle>No tokens yet</EmptyTitle>
                </EmptyHeader>
              </Empty>
            )}
            {passkeysAreSupported() ? (
              <Button
                id="settings_create_api_token_button"
                type="button"
                variant="secondary"
                className={cn(touchTargetButtonClassName, "w-full")}
                onClick={() => setSheetStep("naming")}
              >
                <Plus />
                Make an API token
              </Button>
            ) : null}
          </div>
        )}
      </ResponsiveSheet>
    </>
  )
}

// A name, then the passkey check; the API answers the token, which the sheet shows next.
function ApiTokenNamingForm({
  onPasskeyPromptChange,
  onCreated,
  onBack,
}: {
  onPasskeyPromptChange(passkeyPromptIsOpen: boolean): void
  onCreated(createdApiToken: CreatedAccountApiToken): void
  onBack(): void
}) {
  const { refreshAccountSession } = useAccountSession()
  const [tokenName, setTokenName] = useState("")
  const [tokenNameError, setTokenNameError] = useState("")
  const [creationError, setCreationError] = useState("")
  const [passkeyPromptIsOpen, setPasskeyPromptIsOpen] = useState(false)

  function changePasskeyPromptState(nextPasskeyPromptIsOpen: boolean) {
    setPasskeyPromptIsOpen(nextPasskeyPromptIsOpen)
    onPasskeyPromptChange(nextPasskeyPromptIsOpen)
  }

  async function confirmWithPasskey(submitEvent: FormEvent) {
    submitEvent.preventDefault()
    setCreationError("")
    const trimmedName = tokenName.trim()
    if (!trimmedName) {
      setTokenNameError("Name it after the server that uses it, like “My shop”.")
      return
    }
    changePasskeyPromptState(true)
    try {
      const createdApiToken = await createApiTokenWithPasskey(trimmedName)
      changePasskeyPromptState(false)
      onCreated(createdApiToken)
    } catch (creationFailure) {
      changePasskeyPromptState(false)
      if (creationFailure instanceof ApiError && creationFailure.httpStatus === 401) {
        await refreshAccountSession()
        return
      }
      setCreationError(describePasskeyError(creationFailure))
    }
  }

  return (
    <form onSubmit={(submitEvent) => void confirmWithPasskey(submitEvent)} noValidate className="flex flex-col">
      <Field data-invalid={Boolean(tokenNameError) || undefined}>
        <FieldLabel htmlFor="settings_api_token_name_input">Name</FieldLabel>
        <Input
          id="settings_api_token_name_input"
          maxLength={100}
          autoComplete="off"
          placeholder="e.g. My shop"
          value={tokenName}
          aria-invalid={Boolean(tokenNameError)}
          onChange={(changeEvent) => {
            setTokenName(changeEvent.target.value)
            setTokenNameError("")
            setCreationError("")
          }}
          className="h-12 rounded-xl px-3.5 text-base"
        />
        <FieldError>{tokenNameError}</FieldError>
      </Field>
      {creationError ? (
        <Alert id="settings_api_token_message" variant="destructive" className="mt-4">
          <AlertDescription>{creationError}</AlertDescription>
        </Alert>
      ) : null}
      <Button id="settings_confirm_api_token_button" type="submit" className={cn(primaryActionButtonClassName, "mt-6 w-full")} disabled={passkeyPromptIsOpen}>
        {passkeyPromptIsOpen ? <Spinner /> : <Fingerprint />}
        {passkeyPromptIsOpen ? "Waiting for your passkey…" : "Confirm with passkey"}
      </Button>
      <Button type="button" variant="ghost" className={cn(touchTargetButtonClassName, "mt-2 w-full")} disabled={passkeyPromptIsOpen} onClick={onBack}>
        <ArrowLeft /> Back
      </Button>
    </form>
  )
}
