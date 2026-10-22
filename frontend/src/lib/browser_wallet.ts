// The browser wallet (EIP-1193 `window.ethereum`, https://eips.ethereum.org/EIPS/eip-1193): paying an invoice's
// remainder and signing a refund request with `personal_sign` (EIP-191). Only the payer's pages use it, so its
// sentences are in the payer's language.
import { addressPattern, convertDecimalToBaseUnits } from "@/lib/amount_formatting"
import { ApiError } from "@/lib/api_client"
import type { InvoiceChain, PublicInvoice } from "@/lib/api_types"
import { describePayerError } from "@/lib/payer_errors"
import { readPayerStrings } from "@/lib/payer_language"

interface Eip1193Provider {
  request(arguments_: { method: string; params?: unknown[] }): Promise<unknown>
}

interface WalletError {
  code?: number
  message?: string
}

declare global {
  interface Window {
    ethereum?: Eip1193Provider
  }
}

const erc20TransferSelector = "0xa9059cbb" // transfer(address,uint256)

export function hasBrowserWallet(): boolean {
  return Boolean(window.ethereum && typeof window.ethereum.request === "function")
}

function requireBrowserWallet(): Eip1193Provider {
  if (!window.ethereum) throw new Error(readPayerStrings().noBrowserWallet)
  return window.ethereum
}

export async function requestWalletAccount(): Promise<string> {
  const accounts = (await requireBrowserWallet().request({ method: "eth_requestAccounts" })) as string[] | null
  if (!accounts || accounts.length === 0) throw new Error(readPayerStrings().walletSharedNoAccount)
  return accounts[0]
}

async function switchWalletToChain(chain: InvoiceChain): Promise<void> {
  // A Monero chain has no chain id and no injected wallet; the payer page never offers "Pay with wallet" there.
  if (chain.chain_id === null) throw new Error(readPayerStrings().browserWalletCantPayOn(chain.name))
  const chainIdHex = `0x${chain.chain_id.toString(16)}`
  try {
    await requireBrowserWallet().request({ method: "wallet_switchEthereumChain", params: [{ chainId: chainIdHex }] })
  } catch (switchError) {
    if ((switchError as WalletError).code === 4902) {
      throw new Error(readPayerStrings().walletDoesNotKnowChain(chain.name, chain.chain_id))
    }
    throw switchError
  }
}

function padTo32Bytes(hexWithoutPrefix: string): string {
  return hexWithoutPrefix.toLowerCase().padStart(64, "0")
}

function buildErc20TransferData(recipientAddress: string, baseUnits: bigint): string {
  if (!addressPattern.test(recipientAddress)) throw new Error(`Not an address: ${recipientAddress}`)
  return erc20TransferSelector + padTo32Bytes(recipientAddress.slice(2)) + padTo32Bytes(baseUnits.toString(16))
}

// Sends `amount_remaining` of the invoice's asset to its deposit address (native or ERC-20); returns the tx hash.
export async function sendInvoicePaymentFromWallet(invoice: PublicInvoice): Promise<string> {
  if (!invoice.asset || !invoice.amount_remaining || !addressPattern.test(invoice.deposit_address ?? "")) {
    throw new Error(readPayerStrings().noDepositAddressYet)
  }
  const depositAddress = invoice.deposit_address as string
  const sendingAccount = await requestWalletAccount()
  await switchWalletToChain(invoice.chain)
  const baseUnits = convertDecimalToBaseUnits(invoice.amount_remaining, invoice.asset.decimals)
  if (baseUnits <= 0n) throw new Error(readPayerStrings().nothingLeftToPay)
  const transaction = invoice.asset.token_address
    ? { from: sendingAccount, to: invoice.asset.token_address, data: buildErc20TransferData(depositAddress, baseUnits) }
    : { from: sendingAccount, to: depositAddress, value: `0x${baseUnits.toString(16)}` }
  return (await requireBrowserWallet().request({ method: "eth_sendTransaction", params: [transaction] })) as string
}

function encodeTextAsHex(text: string): string {
  return `0x${Array.from(new TextEncoder().encode(text), (byte) => byte.toString(16).padStart(2, "0")).join("")}`
}

// personal_sign takes the message as hex-encoded UTF-8; wallets show it as the original text.
export async function signTextWithWallet(messageText: string, account: string): Promise<string> {
  return (await requireBrowserWallet().request({
    method: "personal_sign",
    params: [encodeTextAsHex(messageText), account],
  })) as string
}

// Wallets nest the provider error (MetaMask under `data.originalError`, ethers under `info.error`, others as `cause`
// or `error`); this walks the chain and keeps every code and message it finds.
function collectWalletErrorCodesAndMessages(walletError: unknown): { codes: (number | string)[]; messages: string[] } {
  const codes: (number | string)[] = []
  const messages: string[] = []
  const seenErrors = new Set<unknown>()
  const errorsToVisit: unknown[] = [walletError]
  while (errorsToVisit.length > 0 && seenErrors.size < 10) {
    const nestedError = errorsToVisit.shift()
    if (!nestedError || typeof nestedError !== "object" || seenErrors.has(nestedError)) continue
    seenErrors.add(nestedError)
    const fields = nestedError as WalletError & Record<string, unknown>
    if (typeof fields.code === "number" || typeof fields.code === "string") codes.push(fields.code)
    if (typeof fields.message === "string") messages.push(fields.message)
    const nestedData = fields.data as Record<string, unknown> | undefined
    const nestedInfo = fields.info as Record<string, unknown> | undefined
    errorsToVisit.push(fields.cause, fields.error, nestedData, nestedData?.originalError, nestedInfo?.error)
  }
  return { codes, messages }
}

// Plain sentences for the codes a payer meets: EIP-1193's 4001/4100/4200/4900/4901
// (https://eips.ethereum.org/EIPS/eip-1193#provider-errors), EIP-1474's -32002 "Resource unavailable" (MetaMask's
// "request already pending"), and 4902, MetaMask's unknown chain on wallet_switchEthereumChain (EIP-3326 names no
// code; checked 2026-10-11). Anything else keeps the wallet's own message, cut short; the page wraps it.
export function describeWalletError(walletError: unknown): string {
  // The refund request's API refusals reach here too; they get the API's sentences.
  if (walletError instanceof ApiError) return describePayerError(walletError)
  const payerStrings = readPayerStrings()
  const { codes, messages } = collectWalletErrorCodesAndMessages(walletError)
  const allMessagesText = messages.join(" ").toLowerCase()
  if (codes.includes(4001) || codes.includes("ACTION_REJECTED") || /user (rejected|denied)/.test(allMessagesText)) {
    return payerStrings.walletDeclined
  }
  if (/insufficient funds|exceeds balance|insufficient balance/.test(allMessagesText)) return payerStrings.walletNotEnoughFunds
  if (codes.includes(4902)) return payerStrings.walletUnknownNetwork
  if (codes.includes(-32002)) return payerStrings.walletRequestAlreadyOpen
  if (codes.includes(4100)) return payerStrings.walletNotLetIn
  if (codes.includes(4900) || codes.includes(4901)) return payerStrings.walletNotConnected
  if (codes.includes(4200)) return payerStrings.walletCantSendFromPage
  const firstMessage = messages[0]?.trim()
  if (!firstMessage) return payerStrings.walletReportedError
  return firstMessage.length > 160 ? `${firstMessage.slice(0, 157)}…` : firstMessage
}
