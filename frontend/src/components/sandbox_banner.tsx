import { FlaskConical } from "lucide-react"
import { use } from "react"

import { InterfaceLabelsContext } from "@/components/interface_labels_context"
import { deploymentIsSandbox } from "@/lib/deployment_mode"
import { cn } from "@/lib/utils"

// One quiet strip on every page of the testnet sandbox (PLAN.md section 16), so nobody mistakes it for the real
// gateway or sends it real money; production renders nothing. The text carries the meaning, the icon is decoration.
export function SandboxBanner({ className }: { className?: string }) {
  if (!deploymentIsSandbox) return null
  return (
    <p
      role="note"
      data-testid="sandbox_banner"
      className={cn(
        "flex min-h-9 items-center justify-center gap-2 rounded-xl border border-warning/40 bg-warning/10 px-3 py-1.5 text-[13px] font-medium text-foreground",
        className,
      )}
    >
      <FlaskConical aria-hidden="true" className="size-4 shrink-0 text-warning" />
      {use(InterfaceLabelsContext).sandboxBanner}
    </p>
  )
}
