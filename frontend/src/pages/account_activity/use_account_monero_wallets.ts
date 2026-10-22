// The account's Monero wallets for `/activity`'s Needs you (PLAN.md section 14, 2026-10-14): loaded once and again with
// each debounced reload after live events, so a wallet whose run of unpaid subaddresses reached the lookahead warning
// asks for the wider lookahead, and stops asking once the receiver taps "I set it". Only while `/chains` lists a Monero
// chain: a Polygon-only deployment never asks. A load failure shows nothing: the invoice list already says when the API
// is down.
import { useCallback, useEffect, useState } from "react"

import { useChainList } from "@/hooks/use_chain_list"
import type { AccountMoneroWallet } from "@/lib/api_types"
import { confirmMoneroLookahead, readEveryMoneroWallet } from "@/lib/monero_wallet_settings"

export function useAccountMoneroWallets(liveReloadCount: number) {
  const [moneroWallets, setMoneroWallets] = useState<AccountMoneroWallet[]>([])
  const { chains } = useChainList()
  const moneroChainIsEnabled = chains?.some((chain) => chain.kind === "monero") ?? false

  // The state changes in the promise callback, once the API answers, never synchronously in the effect below.
  const loadMoneroWallets = useCallback(
    () =>
      readEveryMoneroWallet().then(
        ({ moneroWallets: loadedMoneroWallets }) => setMoneroWallets(loadedMoneroWallets),
        () => undefined,
      ),
    [],
  )
  useEffect(() => {
    if (moneroChainIsEnabled) void loadMoneroWallets()
  }, [loadMoneroWallets, liveReloadCount, moneroChainIsEnabled])

  // Throws the API's refusal for the caller to show.
  const confirmWidenedLookahead = useCallback(async (chainKey: string) => {
    const updatedWalletSettings = await confirmMoneroLookahead(chainKey)
    const updatedWallet = updatedWalletSettings.wallet
    if (updatedWallet) {
      setMoneroWallets((currentWallets) => currentWallets.map((moneroWallet) => (moneroWallet.chain_key === chainKey ? updatedWallet : moneroWallet)))
    }
  }, [])

  return { moneroWalletsNeedingLookahead: moneroWallets.filter((moneroWallet) => moneroWallet.lookahead_warning), confirmWidenedLookahead }
}
