// A date or date-and-time field that stays inside its column on iOS Safari. WebKit sizes a temporal input with padding
// wider than `width: 100%` on iOS (https://bugs.webkit.org/show_bug.cgi?id=301648, open as of iOS 26.5), which pushed
// `/settings`' export sheet past a 375 px screen. So the border and padding sit on a wrapper drawn like `Input`, and the
// input inside has none: nothing but `width: 100%` and `min-width: 0` for WebKit to size.
import type { ComponentProps } from "react"

import { cn } from "@/lib/utils"

export function TemporalInput({ className, type, ...inputProps }: ComponentProps<"input"> & { type: "date" | "datetime-local" }) {
  return (
    <div
      className={cn(
        "flex h-12 w-full min-w-0 items-center rounded-xl border border-input bg-transparent px-3 transition-colors focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/50 has-aria-invalid:border-destructive has-aria-invalid:ring-3 has-aria-invalid:ring-destructive/20 dark:bg-input/30",
        className,
      )}
    >
      <input
        type={type}
        data-slot="input"
        className="block h-full w-full min-w-0 appearance-none border-0 bg-transparent p-0 text-base outline-none"
        {...inputProps}
      />
    </div>
  )
}
