// A coin's logo from /static/coin_logos/<symbol lowercased>.png (committed with the coin list), with the symbol's
// initials as the fallback, so a coin without a file still looks deliberate.
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import { cn } from "@/lib/utils"

// A coin whose symbol differs from its logo file's: USDT0 (Arbitrum's USDT) wears Tether's. ETH has no alias: the WETH
// file is a "WETH" word badge, not the Ethereum diamond, so ETH shows its initials until a real logo is committed.
const logoFileNameBySymbol: Record<string, string> = { usdt0: "usdt" }

function coinLogoPath(assetSymbol: string): string {
  const lowercaseSymbol = assetSymbol.toLowerCase()
  return `${import.meta.env.BASE_URL}coin_logos/${logoFileNameBySymbol[lowercaseSymbol] ?? lowercaseSymbol}.png`
}

export function CoinLogo({ assetSymbol, className }: { assetSymbol: string; className?: string }) {
  return (
    <Avatar className={cn("size-10 after:border-transparent", className)}>
      <AvatarImage src={coinLogoPath(assetSymbol)} alt="" />
      <AvatarFallback className="bg-foreground/[0.06] text-[11px] font-semibold tracking-tight text-foreground/70">
        {assetSymbol.replace(/[^A-Za-z0-9]/g, "").slice(0, 3).toUpperCase()}
      </AvatarFallback>
    </Avatar>
  )
}
