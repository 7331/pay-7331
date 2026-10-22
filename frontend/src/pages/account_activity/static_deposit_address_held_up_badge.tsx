// "Held up" on a customer address while a sweep of its money failed and the operator moves it by hand (PLAN.md
// section 17); nothing otherwise, since an address is always on.
import { Badge } from "@/components/ui/badge"
import type { StaticDepositAddress } from "@/lib/api_types"

export function StaticDepositAddressHeldUpBadge({ staticDepositAddress }: { staticDepositAddress: StaticDepositAddress }) {
  if (!staticDepositAddress.sweep_blocked) return null
  return (
    <Badge variant="secondary" className="h-6 rounded-full bg-destructive/15 px-2.5 text-destructive">
      Held up
    </Badge>
  )
}
