// Passkeys (WebAuthn) through the browser's own JSON helpers, no library:
// PublicKeyCredential.parseCreationOptionsFromJSON / parseRequestOptionsFromJSON and credential.toJSON()
// (https://developer.mozilla.org/en-US/docs/Web/API/PublicKeyCredential). The session is an HttpOnly cookie;
// nothing goes to localStorage (PLAN.md sections 7 and 10).
import { ApiError, describeError, fetchApiJson } from "@/lib/api_client"
import type {
  AccountDiscordWebhook,
  AccountMoneroWalletSettings,
  AccountPasskey,
  AccountSession,
  ChangedAccountWebhook,
  CreatedAccountApiToken,
} from "@/lib/api_types"

interface PublicKeyCredentialJsonHelpers {
  parseCreationOptionsFromJSON(options: unknown): PublicKeyCredentialCreationOptions
  parseRequestOptionsFromJSON(options: unknown): PublicKeyCredentialRequestOptions
}

interface CredentialWithJson extends Credential {
  toJSON(): unknown
}

const credentialHelpers = () => window.PublicKeyCredential as unknown as PublicKeyCredentialJsonHelpers

export const usernamePattern = /^[A-Za-z0-9_]{3,20}$/

export function passkeysAreSupported(): boolean {
  return (
    typeof window.PublicKeyCredential === "function" &&
    typeof credentialHelpers().parseCreationOptionsFromJSON === "function" &&
    typeof credentialHelpers().parseRequestOptionsFromJSON === "function" &&
    window.isSecureContext
  )
}

// The signed-in session, or null signed out (`GET /sessions/current` answers null rather than 401).
export async function readCurrentAccountSession(): Promise<AccountSession | null> {
  try {
    return await fetchApiJson<AccountSession | null>("/api/v1/sessions/current")
  } catch (sessionError) {
    if (sessionError instanceof ApiError && sessionError.httpStatus === 401) return null
    throw sessionError
  }
}

async function createPasskeyCredential(optionsJson: unknown): Promise<unknown> {
  const credential = (await navigator.credentials.create({
    publicKey: credentialHelpers().parseCreationOptionsFromJSON(optionsJson),
  })) as CredentialWithJson | null
  if (!credential) throw new Error("The browser made no passkey.")
  return credential.toJSON()
}

async function getPasskeyAssertion(optionsJson: unknown): Promise<unknown> {
  const credential = (await navigator.credentials.get({
    publicKey: credentialHelpers().parseRequestOptionsFromJSON(optionsJson),
  })) as CredentialWithJson | null
  if (!credential) throw new Error("The browser gave no passkey.")
  return credential.toJSON()
}

export async function signInWithPasskey(): Promise<AccountSession> {
  const optionsJson = await fetchApiJson<unknown>("/api/v1/sessions/authentication-options", { method: "POST" })
  const credential = await getPasskeyAssertion(optionsJson)
  return fetchApiJson<AccountSession>("/api/v1/sessions", { method: "POST", body: { credential } })
}

// The payout address changes only behind a fresh passkey check of this account: the options name
// the new address, so the passkey approves exactly that one; the API answers the session with it.
export async function changePayoutAddressWithPasskey(payoutAddress: string): Promise<AccountSession> {
  const optionsJson = await fetchApiJson<unknown>("/api/v1/accounts/current/payout-address-change-options", {
    method: "POST",
    body: { payout_address: payoutAddress },
  })
  const credential = await getPasskeyAssertion(optionsJson)
  return fetchApiJson<AccountSession>("/api/v1/accounts/current/payout-address", {
    method: "PUT",
    body: { payout_address: payoutAddress, credential },
  })
}

// A new API token behind a fresh passkey check of this account (2026-10-12); the answer carries the token once.
export async function createApiTokenWithPasskey(tokenName: string): Promise<CreatedAccountApiToken> {
  const optionsJson = await fetchApiJson<unknown>("/api/v1/account-api-tokens/creation-options", { method: "POST" })
  const credential = await getPasskeyAssertion(optionsJson)
  return fetchApiJson<CreatedAccountApiToken>("/api/v1/account-api-tokens", {
    method: "POST",
    body: { name: tokenName, credential },
  })
}

// Setting the webhook URL and rotating its secret both ask for a fresh passkey check (2026-10-12): either decides
// where the account's invoice events go or what signs them.
export async function setAccountWebhookWithPasskey(webhookUrl: string): Promise<ChangedAccountWebhook> {
  const optionsJson = await fetchApiJson<unknown>("/api/v1/account-webhook/change-options", { method: "POST" })
  const credential = await getPasskeyAssertion(optionsJson)
  return fetchApiJson<ChangedAccountWebhook>("/api/v1/account-webhook", { method: "PUT", body: { url: webhookUrl, credential } })
}

export async function rotateAccountWebhookSecretWithPasskey(): Promise<ChangedAccountWebhook> {
  const optionsJson = await fetchApiJson<unknown>("/api/v1/account-webhook/change-options", { method: "POST" })
  const credential = await getPasskeyAssertion(optionsJson)
  return fetchApiJson<ChangedAccountWebhook>("/api/v1/account-webhook/secret-rotation", { method: "POST", body: { credential } })
}

// The Discord webhook is set behind the same fresh passkey check (2026-10-13): it decides where payment news goes.
export async function setAccountDiscordWebhookWithPasskey(discordWebhookUrl: string): Promise<AccountDiscordWebhook> {
  const optionsJson = await fetchApiJson<unknown>("/api/v1/account-discord-webhook/change-options", { method: "POST" })
  const credential = await getPasskeyAssertion(optionsJson)
  return fetchApiJson<AccountDiscordWebhook>("/api/v1/account-discord-webhook", {
    method: "PUT",
    body: { url: discordWebhookUrl, credential },
  })
}

// The Monero wallet is set behind the same fresh passkey check (2026-10-14): it decides where XMR links pay. The options
// name the wallet, so the passkey approves exactly that one. The view key goes in the PUT body and nowhere else.
export async function setAccountMoneroWalletWithPasskey(chainKey: string, primaryAddress: string, privateViewKey: string): Promise<AccountMoneroWalletSettings> {
  const optionsJson = await fetchApiJson<unknown>("/api/v1/account-monero-wallet/change-options", {
    method: "POST",
    body: { chain_key: chainKey, primary_address: primaryAddress },
  })
  const credential = await getPasskeyAssertion(optionsJson)
  return fetchApiJson<AccountMoneroWalletSettings>("/api/v1/account-monero-wallet", {
    method: "PUT",
    body: { chain_key: chainKey, primary_address: primaryAddress, private_view_key: privateViewKey, credential },
  })
}

// Makes the account and its first passkey together; answers the new session (signed in).
export async function signUpWithPasskey(username: string, payoutAddress: string, passkeyLabel: string): Promise<AccountSession> {
  const optionsJson = await fetchApiJson<unknown>("/api/v1/accounts/registration-options", {
    method: "POST",
    body: { username, payout_address: payoutAddress },
  })
  const credential = await createPasskeyCredential(optionsJson)
  return fetchApiJson<AccountSession>("/api/v1/accounts", { method: "POST", body: { label: passkeyLabel, credential } })
}

// One more passkey behind a fresh passkey check of this account: a passkey it already has approves, then the new one is
// made. A stolen session alone can't add one.
export async function addAccountPasskeyWithPasskey(passkeyLabel: string): Promise<AccountPasskey> {
  const approvalOptionsJson = await fetchApiJson<unknown>("/api/v1/account-passkeys/addition-options", { method: "POST", body: {} })
  const approvalCredential = await getPasskeyAssertion(approvalOptionsJson)
  const registrationOptionsJson = await fetchApiJson<unknown>("/api/v1/account-passkeys/registration-options", {
    method: "POST",
    body: { credential: approvalCredential },
  })
  const credential = await createPasskeyCredential(registrationOptionsJson)
  return fetchApiJson<AccountPasskey>("/api/v1/account-passkeys", { method: "POST", body: { label: passkeyLabel, credential } })
}

// An operator-minted enrollment link (`/enroll-passkey#<token>`, 2026-10-15): the options name the link's account and
// its passkeys as an "add a passkey" ceremony does; they are fetched when the page opens, so a used or expired link
// says so before any prompt.
export async function readPasskeyEnrollmentOptions(enrollmentToken: string): Promise<unknown> {
  return fetchApiJson<unknown>("/api/v1/passkey-enrollment-links/registration-options", {
    method: "POST",
    body: { token: enrollmentToken },
  })
}

// Makes the passkey from those options and enrolls it; answers the new session (signed in).
export async function enrollPasskeyWithLink(enrollmentToken: string, optionsJson: unknown, passkeyLabel: string): Promise<AccountSession> {
  const credential = await createPasskeyCredential(optionsJson)
  return fetchApiJson<AccountSession>("/api/v1/passkey-enrollment-links/passkeys", {
    method: "POST",
    body: { token: enrollmentToken, label: passkeyLabel, credential },
  })
}

export async function listAccountPasskeys(): Promise<AccountPasskey[]> {
  return (await fetchApiJson<{ passkeys: AccountPasskey[] }>("/api/v1/account-passkeys")).passkeys
}

export async function renameAccountPasskey(accountPasskeyId: string, passkeyLabel: string): Promise<AccountPasskey> {
  return fetchApiJson<AccountPasskey>(`/api/v1/account-passkeys/${accountPasskeyId}`, {
    method: "PATCH",
    body: { label: passkeyLabel },
  })
}

// Behind a fresh passkey check bound to this passkey, so a stolen session can't delete the owner's. Its sessions end
// with it; the API refuses the last one (409 `last_passkey`).
export async function deleteAccountPasskeyWithPasskey(accountPasskeyId: string): Promise<void> {
  const optionsJson = await fetchApiJson<unknown>(`/api/v1/account-passkeys/${accountPasskeyId}/deletion-options`, {
    method: "POST",
    body: {},
  })
  const credential = await getPasskeyAssertion(optionsJson)
  await fetchApiJson<unknown>(`/api/v1/account-passkeys/${accountPasskeyId}/deletion`, { method: "POST", body: { credential } })
}

export async function signOutAccountSession(): Promise<void> {
  await fetchApiJson<unknown>("/api/v1/sessions/current", { method: "DELETE" })
}

export function describePasskeyError(failure: unknown): string {
  if (failure instanceof ApiError) return describeError(failure)
  const name = (failure as { name?: string } | null)?.name
  if (name === "NotAllowedError") return "The passkey prompt was closed or timed out."
  if (name === "InvalidStateError") return "This device already holds a passkey for this account."
  return (failure as Error | null)?.message || "The passkey step failed."
}

// "Chrome on Android", "Safari on iPhone": enough to tell two passkeys apart in /settings. Read from the user
// agent string, which every browser still sends; a name the browser hides becomes "Passkey" for that part.
export function defaultPasskeyLabel(): string {
  const userAgent = navigator.userAgent
  const browserName = /Edg\//.test(userAgent)
    ? "Edge"
    : /OPR\//.test(userAgent)
      ? "Opera"
      : /Firefox\/|FxiOS\//.test(userAgent)
        ? "Firefox"
        : /Chrome\/|CriOS\//.test(userAgent)
          ? "Chrome"
          : /Safari\//.test(userAgent)
            ? "Safari"
            : ""
  const systemName = /iPhone/.test(userAgent)
    ? "iPhone"
    : /iPad/.test(userAgent)
      ? "iPad"
      : /Android/.test(userAgent)
        ? "Android"
        : /Mac OS X|Macintosh/.test(userAgent)
          ? "Mac"
          : /Windows/.test(userAgent)
            ? "Windows"
            : /CrOS/.test(userAgent)
              ? "ChromeOS"
              : /Linux/.test(userAgent)
                ? "Linux"
                : ""
  if (browserName && systemName) return `${browserName} on ${systemName}`
  return browserName || systemName || "Passkey"
}
