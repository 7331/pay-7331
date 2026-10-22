// The account's Monero wallets (PLAN.md section 14, 2026-10-14): read per enabled Monero chain, and the receiver's
// "I set it" after widening its wallet's lookahead. Shared by `/settings` and `/activity`'s Needs you.
import { fetchApiJson } from "@/lib/api_client"
import type { AccountMoneroWallet, AccountMoneroWalletSettings, MoneroChainSummary } from "@/lib/api_types"

// The exact line a receiver runs in monero-wallet-cli: 50 accounts, 1000 subaddresses each.
export const moneroLookaheadCommand = "set subaddress-lookahead 50:1000"
// What the API records once the receiver confirms that line.
export const widenedMoneroLookaheadMinorCount = 1000

// A standard primary address: 95 base58 characters, starting 4 on mainnet and 5 on stagenet
// (https://docs.getmonero.org/public-address/standard-address/). The API's sidecar checks the checksum.
const moneroPrimaryAddressPattern = /^[1-9A-HJ-NP-Za-km-z]{95}$/
const privateViewKeyPattern = /^[0-9a-fA-F]{64}$/

export function describeMoneroAddressProblem(addressText: string, network: MoneroChainSummary["network"]): string {
  if (!addressText) return "Paste your wallet's primary address."
  if (!moneroPrimaryAddressPattern.test(addressText)) return "A primary address is 95 letters and digits. Copy it whole from your wallet."
  const networkPrefix = network === "mainnet" ? "4" : "5"
  if (!addressText.startsWith(networkPrefix)) return `A ${network} primary address starts with ${networkPrefix}. Use account 0's address, not a subaddress.`
  return ""
}

export function describePrivateViewKeyProblem(privateViewKeyText: string): string {
  if (!privateViewKeyText) return "Paste your wallet's secret view key."
  if (!privateViewKeyPattern.test(privateViewKeyText)) return "A view key is 64 characters of 0-9 and a-f."
  return ""
}

export function readMoneroWalletSettings(chainKey?: string): Promise<AccountMoneroWalletSettings> {
  const query = chainKey ? `?chain_key=${encodeURIComponent(chainKey)}` : ""
  return fetchApiJson<AccountMoneroWalletSettings>(`/api/v1/account-monero-wallet${query}`)
}

// Every enabled Monero chain's wallet (most deployments enable one), with the chains.
export async function readEveryMoneroWallet(): Promise<{ moneroChains: MoneroChainSummary[]; moneroWallets: AccountMoneroWallet[] }> {
  const firstChainSettings = await readMoneroWalletSettings()
  const otherChainSettings = await Promise.all(firstChainSettings.monero_chains.slice(1).map((moneroChain) => readMoneroWalletSettings(moneroChain.key)))
  return {
    moneroChains: firstChainSettings.monero_chains,
    moneroWallets: [firstChainSettings, ...otherChainSettings].flatMap((chainSettings) => (chainSettings.wallet ? [chainSettings.wallet] : [])),
  }
}

export function confirmMoneroLookahead(chainKey: string): Promise<AccountMoneroWalletSettings> {
  return fetchApiJson<AccountMoneroWalletSettings>("/api/v1/account-monero-wallet/lookahead-confirmations", {
    method: "POST",
    body: { chain_key: chainKey },
  })
}
