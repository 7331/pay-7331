import { Badge } from "@/components/ui/badge"
import type { InvoiceScheduleStatus } from "@/lib/api_types"
import { cn } from "@/lib/utils"

const statusLabels: Record<InvoiceScheduleStatus, string> = {
  active: "On",
  paused: "Paused",
  cancelled: "Cancelled",
  completed: "Done",
}

const statusClassNames: Partial<Record<InvoiceScheduleStatus, string>> = {
  active: "bg-success/15 text-success",
  paused: "bg-warning/15 text-warning",
}

export function InvoiceScheduleStatusBadge({ invoiceScheduleStatus }: { invoiceScheduleStatus: InvoiceScheduleStatus }) {
  return (
    <Badge variant="secondary" className={cn("h-6 rounded-full px-2.5", statusClassNames[invoiceScheduleStatus])}>
      {statusLabels[invoiceScheduleStatus] ?? invoiceScheduleStatus}
    </Badge>
  )
}
