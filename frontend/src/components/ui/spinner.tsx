import { cn } from "cn"
import { Loader2Icon } from "lucide-react"
import { use } from "react"

import { InterfaceLabelsContext } from "@/components/interface_labels_context"

function Spinner({ className, ...props }: React.ComponentProps<"svg">) {
  const loadingLabel = use(InterfaceLabelsContext).loading
  return (
    <Loader2Icon data-slot="spinner" role="status" aria-label={loadingLabel} className={cn("size-4 animate-spin", className)} {...props} />
  )
}

export { Spinner }
