// The browser wallet's last send for an invoice, kept in sessionStorage so the send lock survives a reload
// (2026-10-12: a reload in the window before the watcher saw the payment showed a fresh "Pay with wallet"). The page
// session "survives over page reloads and restores" and is "partitioned by both origin and browser tabs"
// (https://developer.mozilla.org/en-US/docs/Web/API/Window/sessionStorage): a reload keeps the lock, a new tab starts
// without it. A browser that refuses storage (SecurityError, some private modes) keeps the lock for the page's life
// only, as before.
const storageKeyPrefix = "pay_7331_sent_wallet_payment:"

export interface SentWalletPayment {
  transactionHash: string
  sentAtMilliseconds: number
  // The invoice as it stood when the wallet sent (`describeInvoiceProgressKey`); a different key means it moved.
  invoiceProgressKeyWhenSent: string
}

function buildSentWalletPaymentKey(invoiceId: string): string {
  return `${storageKeyPrefix}${invoiceId}`
}

export function readSentWalletPayment(invoiceId: string): SentWalletPayment | null {
  try {
    const storedText = window.sessionStorage.getItem(buildSentWalletPaymentKey(invoiceId))
    if (!storedText) return null
    const stored = JSON.parse(storedText) as Partial<SentWalletPayment>
    if (
      typeof stored.transactionHash !== "string" ||
      typeof stored.sentAtMilliseconds !== "number" ||
      typeof stored.invoiceProgressKeyWhenSent !== "string"
    ) {
      return null
    }
    return {
      transactionHash: stored.transactionHash,
      sentAtMilliseconds: stored.sentAtMilliseconds,
      invoiceProgressKeyWhenSent: stored.invoiceProgressKeyWhenSent,
    }
  } catch {
    return null
  }
}

export function rememberSentWalletPayment(invoiceId: string, sentWalletPayment: SentWalletPayment): void {
  try {
    window.sessionStorage.setItem(buildSentWalletPaymentKey(invoiceId), JSON.stringify(sentWalletPayment))
  } catch {
    // Storage refused: the lock still holds for this page's life (the caller keeps it in state).
  }
}

export function forgetSentWalletPayment(invoiceId: string): void {
  try {
    window.sessionStorage.removeItem(buildSentWalletPaymentKey(invoiceId))
  } catch {
    // Nothing was stored.
  }
}
