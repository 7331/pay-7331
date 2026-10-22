// What `/` offers the maker across networks (PLAN.md section 15). Coins are ticked per network (`assets_by_chain`), so
// USDC can be accepted on Polygon alone; each network keeps its own saved or default coins. These helpers read the
// network list the API gave; none keeps a list here.
import type { AccountSession, ChainAsset, ListedChain } from "@/lib/api_types"

// Every coin the networks list, one per symbol, in the server's order (the first network's entry names it): what a link can be priced in.
export function listCoinsAcrossNetworks(networks: ListedChain[]): ChainAsset[] {
  const coinsBySymbol = new Map<string, ChainAsset>()
  for (const network of networks) {
    for (const asset of network.assets) if (!coinsBySymbol.has(asset.symbol)) coinsBySymbol.set(asset.symbol, asset)
  }
  return [...coinsBySymbol.values()]
}

// The coins one network starts with ticked: the account's saved ones for it, else the network's defaults.
function listNetworkStartingSymbols(network: ListedChain, accountSession: AccountSession | null): string[] {
  return accountSession?.accepted_assets_by_chain[network.key] ?? network.default_accepted_assets
}

// The coins ticked on one network: the maker's own ticks there, else its saved or default coins, only those it lists, and
// never none (the first coin it lists stands in). Each network keeps its own list; nothing is merged across networks.
export function readNetworkAcceptedSymbols(
  network: ListedChain,
  pickedSymbolsByNetworkKey: Record<string, string[]>,
  accountSession: AccountSession | null,
): string[] {
  const wantedSymbols = pickedSymbolsByNetworkKey[network.key] ?? listNetworkStartingSymbols(network, accountSession)
  const listedWantedSymbols = network.assets.map((asset) => asset.symbol).filter((assetSymbol) => wantedSymbols.includes(assetSymbol))
  return listedWantedSymbols.length > 0 ? listedWantedSymbols : network.assets.slice(0, 1).map((asset) => asset.symbol)
}

// The networks that list a coin: a coin-priced link goes only to these.
export function listNetworksOfferingSymbol(networks: ListedChain[], assetSymbol: string): ListedChain[] {
  return networks.filter((network) => network.assets.some((asset) => asset.symbol === assetSymbol))
}
