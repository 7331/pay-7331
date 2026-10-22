// The payer's one tap on "Pay 5.00 USDC on Polygon" before the address shows, remembered per
// invoice, chain and coin in sessionStorage so a reload or a poll doesn't ask again; a new tab asks once more. `/l/`
// writes it for the invoice its tap made, so that payer lands on the send screen. A browser that refuses storage (some
// private modes) just asks again after a reload.
const storageKeyPrefix = "pay_7331_coin_network_confirmed:"

export function buildCoinNetworkConfirmationKey(invoiceId: string, chainKey: string, assetSymbol: string): string {
  return `${storageKeyPrefix}${invoiceId}:${chainKey}:${assetSymbol}`
}

export function readCoinNetworkConfirmation(confirmationKey: string): boolean {
  try {
    return window.sessionStorage.getItem(confirmationKey) === "confirmed"
  } catch {
    return false
  }
}

export function rememberCoinNetworkConfirmation(confirmationKey: string): void {
  try {
    window.sessionStorage.setItem(confirmationKey, "confirmed")
  } catch {
    // Storage refused: the confirmation still holds for this page's life (the caller keeps it in state).
  }
}
