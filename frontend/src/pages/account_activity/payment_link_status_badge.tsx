import { Badge } from "@/components/ui/badge"
import type { PaymentLinkStatus } from "@/lib/api_types"
import { cn } from "@/lib/utils"

const statusLabels: Record<PaymentLinkStatus, string> = {
  open: "Selling",
  all_taken: "All taken",
  sold_out: "Sold out",
  closed: "Closed",
}

const statusClassNames: Partial<Record<PaymentLinkStatus, string>> = {
  sold_out: "bg-success/15 text-success",
  all_taken: "bg-foreground/10 text-foreground",
}

export function PaymentLinkStatusBadge({ paymentLinkStatus }: { paymentLinkStatus: PaymentLinkStatus }) {
  return (
    <Badge variant="secondary" className={cn("h-6 rounded-full px-2.5", statusClassNames[paymentLinkStatus])}>
      {statusLabels[paymentLinkStatus] ?? paymentLinkStatus}
    </Badge>
  )
}
