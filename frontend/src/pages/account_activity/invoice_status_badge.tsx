import { Badge } from "@/components/ui/badge"
import type { InvoiceStatus } from "@/lib/api_types"
import { cn } from "@/lib/utils"

const statusLabels: Record<InvoiceStatus, string> = {
  // "Unpaid", not "Open": the toast's action button sits beside the badges.
  awaiting_asset: "Unpaid",
  awaiting_payment: "Unpaid",
  confirming: "Confirming",
  underpaid: "Underpaid",
  paid: "Paid",
  overpaid: "Overpaid",
  expired: "Expired",
  cancelled: "Cancelled",
}

const statusClassNames: Partial<Record<InvoiceStatus, string>> = {
  paid: "bg-success/15 text-success",
  overpaid: "bg-success/15 text-success",
  confirming: "bg-foreground/10 text-foreground",
  underpaid: "bg-warning/15 text-warning",
}

// `paymentIsSeen`: a Monero payment is in the pool or in a block but not credited yet (the Invoice resource's `seen`),
// so the badge says what the payer page already says instead of "Unpaid".
export function InvoiceStatusBadge({ invoiceStatus, paymentIsSeen = false }: { invoiceStatus: InvoiceStatus; paymentIsSeen?: boolean }) {
  const statusIsUnpaid = invoiceStatus === "awaiting_payment" || invoiceStatus === "awaiting_asset"
  const statusLabel = statusIsUnpaid && paymentIsSeen ? "Seen" : (statusLabels[invoiceStatus] ?? invoiceStatus)
  return (
    <Badge
      variant="secondary"
      className={cn("h-6 rounded-full px-2.5", statusIsUnpaid && paymentIsSeen ? statusClassNames.confirming : statusClassNames[invoiceStatus])}
    >
      {statusLabel}
    </Badge>
  )
}
