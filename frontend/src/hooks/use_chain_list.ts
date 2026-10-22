// GET /api/v1/chains, once per page load: the chains and their coins in the server's order (PLAN.md section 7.1).
import { useEffect, useState } from "react"

import { describeError, fetchApiJson } from "@/lib/api_client"
import type { AccountSession, ChainListResponse, ListedChain } from "@/lib/api_types"

let chainListRequest: Promise<ListedChain[]> | null = null

function loadChainListOnce(): Promise<ListedChain[]> {
  chainListRequest ??= fetchApiJson<ChainListResponse>("/api/v1/chains").then((response) => response.chains ?? [])
  chainListRequest.catch(() => {
    chainListRequest = null
  })
  return chainListRequest
}

export function useChainList(): { chains: ListedChain[] | null; chainLoadError: string } {
  const [chains, setChains] = useState<ListedChain[] | null>(null)
  const [chainLoadError, setChainLoadError] = useState("")
  useEffect(() => {
    let isCurrent = true
    loadChainListOnce()
      .then((loadedChains) => {
        if (!isCurrent) return
        if (loadedChains.length === 0) setChainLoadError("No chain can take invoices yet.")
        setChains(loadedChains)
      })
      .catch((loadError) => isCurrent && setChainLoadError(describeError(loadError)))
    return () => {
      isCurrent = false
    }
  }, [])
  return { chains, chainLoadError }
}

// The chains this maker can make a link on: every EVM chain, and a Monero chain only for an account with a Monero
// wallet on it, which is exactly when the session's accepted coins list that chain (PLAN.md section 14). Signed out,
// EVM only.
export function listChainsForMaker(chains: ListedChain[] | null, accountSession: AccountSession | null): ListedChain[] | null {
  if (chains === null) return null
  return chains.filter((chain) => chain.kind === "evm" || (accountSession !== null && chain.key in accountSession.accepted_assets_by_chain))
}

// The networks a new link starts on: every EVM network the maker can use (the payer picks one, PLAN.md section 15), else
// the first the maker can use (a deployment with only Monero).
export function pickStartingNetworkKeys(makerChains: ListedChain[] | null): string[] {
  if (!makerChains) return []
  const evmChainKeys = makerChains.filter((chain) => chain.kind === "evm").map((chain) => chain.key)
  return evmChainKeys.length > 0 ? evmChainKeys : makerChains.slice(0, 1).map((chain) => chain.key)
}
