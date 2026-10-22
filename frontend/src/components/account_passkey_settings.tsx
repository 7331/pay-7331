// `/settings`' "Passkeys" row (count) and its sheet (2026-10-11, rows 2026-10-14): the list with the one this session
// signed in with marked, rename (a step in the same sheet), add, and delete after an AlertDialog; the last one cannot
// go. Adding and deleting each ask one of the account's passkeys first (a fresh passkey check), so a stolen session
// can't swap the owner's passkeys for its own; the sheet stays open while that prompt is up.
import { ArrowLeft, Fingerprint, KeyRound, Pencil, Plus, Trash2 } from "lucide-react"
import { useCallback, useEffect, useState, type FormEvent } from "react"
import { toast } from "sonner"

import { AccountSettingsRow } from "@/components/account_settings_row"
import { ResponsiveSheet } from "@/components/responsive_sheet"
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
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Field, FieldError, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Item, ItemActions, ItemContent, ItemDescription, ItemGroup, ItemMedia, ItemTitle } from "@/components/ui/item"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { useAccountSession } from "@/hooks/use_account_session"
import { ApiError, describeError } from "@/lib/api_client"
import type { AccountPasskey } from "@/lib/api_types"
import { primaryActionButtonClassName, touchTargetButtonClassName } from "@/lib/button_size_class_names"
import {
  addAccountPasskeyWithPasskey,
  defaultPasskeyLabel,
  deleteAccountPasskeyWithPasskey,
  describePasskeyError,
  listAccountPasskeys,
  passkeysAreSupported,
  renameAccountPasskey,
} from "@/lib/passkey_access"
import { formatShortTime } from "@/lib/time_formatting"
import { cn } from "@/lib/utils"

export function AccountPasskeySettings() {
  const { refreshAccountSession } = useAccountSession()
  const [accountPasskeys, setAccountPasskeys] = useState<AccountPasskey[] | null>(null)
  const [passkeyListError, setPasskeyListError] = useState("")
  const [sheetIsOpen, setSheetIsOpen] = useState(false)
  const [isAddingPasskey, setIsAddingPasskey] = useState(false)
  // The passkey whose deletion waits for the passkey check, if any.
  const [deletingPasskeyId, setDeletingPasskeyId] = useState<string | null>(null)
  const passkeyPromptIsOpen = isAddingPasskey || deletingPasskeyId !== null
  // The passkey being renamed: the sheet shows its name field instead of the list.
  const [renamedPasskey, setRenamedPasskey] = useState<AccountPasskey | null>(null)

  // The state changes in the promise callbacks, once the API answers, never synchronously in the effect below.
  const reloadAccountPasskeys = useCallback(
    () =>
      listAccountPasskeys().then(
        (loadedPasskeys) => {
          setAccountPasskeys(loadedPasskeys)
          setPasskeyListError("")
        },
        (listFailure: unknown) => setPasskeyListError(describeError(listFailure)),
      ),
    [],
  )

  useEffect(() => {
    void reloadAccountPasskeys()
  }, [reloadAccountPasskeys])

  async function addPasskey() {
    setIsAddingPasskey(true)
    try {
      const addedPasskey = await addAccountPasskeyWithPasskey(defaultPasskeyLabel())
      toast.success(`Passkey "${addedPasskey.label}" added.`)
      await reloadAccountPasskeys()
    } catch (addFailure) {
      if (addFailure instanceof ApiError && addFailure.httpStatus === 401) await refreshAccountSession()
      else toast.error(describePasskeyError(addFailure))
    } finally {
      setIsAddingPasskey(false)
    }
  }

  async function deletePasskey(accountPasskey: AccountPasskey) {
    setDeletingPasskeyId(accountPasskey.id)
    try {
      await deleteAccountPasskeyWithPasskey(accountPasskey.id)
      toast.success(`Passkey "${accountPasskey.label}" deleted.`)
      // Deleting the passkey this session came from signs it out.
      await refreshAccountSession()
      await reloadAccountPasskeys()
    } catch (deleteFailure) {
      if (deleteFailure instanceof ApiError && deleteFailure.httpStatus === 401) await refreshAccountSession()
      else toast.error(describePasskeyError(deleteFailure))
    } finally {
      setDeletingPasskeyId(null)
    }
  }

  const isOnlyPasskey = accountPasskeys?.length === 1
  return (
    <>
      <AccountSettingsRow
        id="settings_passkeys_row"
        title="Passkeys"
        valueText={passkeyListError ? "Can't load" : accountPasskeys === null ? "…" : accountPasskeys.length}
        valueTone={passkeyListError ? "problem" : "default"}
        onOpen={() => setSheetIsOpen(true)}
      />
      <ResponsiveSheet
        isOpen={sheetIsOpen}
        onOpenChange={(nextIsOpen) => {
          if (passkeyPromptIsOpen) return
          setSheetIsOpen(nextIsOpen)
          if (!nextIsOpen) setRenamedPasskey(null)
        }}
        title={renamedPasskey ? "Rename passkey" : "Passkeys"}
        testId="settings_passkeys_sheet"
      >
        {renamedPasskey ? (
          <PasskeyRenameForm
            key={renamedPasskey.id}
            renamedPasskey={renamedPasskey}
            onDone={(passkeyWasRenamed) => {
              setRenamedPasskey(null)
              if (passkeyWasRenamed) void reloadAccountPasskeys()
            }}
          />
        ) : (
          <div className="flex flex-col gap-3">
            {passkeyListError ? (
              <Alert variant="destructive">
                <AlertDescription>Can't load your passkeys: {passkeyListError}</AlertDescription>
              </Alert>
            ) : accountPasskeys === null ? (
              <Skeleton className="h-16 w-full rounded-2xl" />
            ) : (
              <ItemGroup className="gap-2">
                {accountPasskeys.map((accountPasskey) => (
                  <Item key={accountPasskey.id} variant="outline" className="min-h-14 rounded-2xl" data-testid="settings_passkey_row">
                    <ItemMedia variant="icon">
                      <KeyRound />
                    </ItemMedia>
                    <ItemContent className="min-w-0">
                      <ItemTitle className="flex-wrap">
                        <span className="break-words">{accountPasskey.label}</span>
                        {accountPasskey.used_by_current_session ? (
                          <Badge variant="secondary" data-testid="settings_current_passkey_badge">
                            Signed in with this
                          </Badge>
                        ) : null}
                      </ItemTitle>
                      <ItemDescription>
                        Added {formatShortTime(accountPasskey.created_at)}
                        {accountPasskey.last_used_at ? `, last used ${formatShortTime(accountPasskey.last_used_at)}` : ""}
                      </ItemDescription>
                    </ItemContent>
                    <ItemActions className="gap-0">
                      <Button
                        type="button"
                        variant="ghost"
                        className={touchTargetButtonClassName}
                        aria-label={`Rename passkey ${accountPasskey.label}`}
                        data-testid="settings_rename_passkey_button"
                        onClick={() => setRenamedPasskey(accountPasskey)}
                      >
                        <Pencil />
                      </Button>
                      <AlertDialog>
                        <AlertDialogTrigger asChild>
                          <Button
                            type="button"
                            variant="ghost"
                            className={touchTargetButtonClassName}
                            aria-label={`Delete passkey ${accountPasskey.label}`}
                            disabled={isOnlyPasskey || passkeyPromptIsOpen}
                            title={isOnlyPasskey ? "Add another passkey before deleting this one" : undefined}
                            data-testid="settings_delete_passkey_button"
                          >
                            {deletingPasskeyId === accountPasskey.id ? <Spinner /> : <Trash2 />}
                          </Button>
                        </AlertDialogTrigger>
                        <AlertDialogContent>
                          <AlertDialogHeader>
                            <AlertDialogTitle>Delete "{accountPasskey.label}"?</AlertDialogTitle>
                            <AlertDialogDescription>
                              It can't sign in here anymore, and anyone signed in with it is signed out. You confirm with one of your passkeys.
                            </AlertDialogDescription>
                          </AlertDialogHeader>
                          <AlertDialogFooter>
                            <AlertDialogCancel className={touchTargetButtonClassName}>Keep it</AlertDialogCancel>
                            <AlertDialogAction
                              variant="destructive"
                              className={touchTargetButtonClassName}
                              data-testid="settings_confirm_delete_passkey_button"
                              onClick={() => void deletePasskey(accountPasskey)}
                            >
                              <Fingerprint />
                              Confirm with passkey
                            </AlertDialogAction>
                          </AlertDialogFooter>
                        </AlertDialogContent>
                      </AlertDialog>
                    </ItemActions>
                  </Item>
                ))}
              </ItemGroup>
            )}
            {passkeysAreSupported() ? (
              <Button
                id="settings_add_passkey_button"
                type="button"
                variant="secondary"
                className={cn(touchTargetButtonClassName, "w-full")}
                disabled={passkeyPromptIsOpen}
                onClick={() => void addPasskey()}
              >
                {isAddingPasskey ? <Spinner /> : <Plus />}
                {isAddingPasskey ? "Waiting for your passkey…" : "Add a passkey"}
              </Button>
            ) : null}
          </div>
        )}
      </ResponsiveSheet>
    </>
  )
}

function PasskeyRenameForm({ renamedPasskey, onDone }: { renamedPasskey: AccountPasskey; onDone(passkeyWasRenamed: boolean): void }) {
  const [labelText, setLabelText] = useState(renamedPasskey.label)
  const [renameError, setRenameError] = useState("")
  const [isRenaming, setIsRenaming] = useState(false)

  async function saveLabel(submitEvent: FormEvent) {
    submitEvent.preventDefault()
    const trimmedLabel = labelText.trim()
    if (!trimmedLabel) {
      setRenameError("Give it a name, like “Phone” or “Work laptop”.")
      return
    }
    setIsRenaming(true)
    try {
      const updatedPasskey = await renameAccountPasskey(renamedPasskey.id, trimmedLabel)
      toast.success(`Renamed to "${updatedPasskey.label}".`)
      onDone(true)
    } catch (renameFailure) {
      setRenameError(describeError(renameFailure))
      setIsRenaming(false)
    }
  }

  return (
    <form onSubmit={(submitEvent) => void saveLabel(submitEvent)} noValidate className="flex flex-col">
      <Field data-invalid={Boolean(renameError) || undefined}>
        <FieldLabel htmlFor="settings_passkey_label_input">Name</FieldLabel>
        <Input
          id="settings_passkey_label_input"
          maxLength={100}
          autoComplete="off"
          value={labelText}
          aria-invalid={Boolean(renameError)}
          onChange={(changeEvent) => {
            setLabelText(changeEvent.target.value)
            setRenameError("")
          }}
          className="h-12 rounded-xl px-3.5 text-base"
        />
        <FieldError>{renameError}</FieldError>
      </Field>
      <Button id="settings_save_passkey_label_button" type="submit" className={cn(primaryActionButtonClassName, "mt-6 w-full")} disabled={isRenaming}>
        {isRenaming ? <Spinner /> : null}
        Save name
      </Button>
      <Button type="button" variant="ghost" className={cn(touchTargetButtonClassName, "mt-2 w-full")} onClick={() => onDone(false)}>
        <ArrowLeft /> Back
      </Button>
    </form>
  )
}
