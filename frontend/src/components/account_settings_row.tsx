// One `/settings` row (2026-10-14): the setting's name, its current value on the right, and a tap that opens its sheet.
// Rows sit in a bordered ItemGroup; each draws its own divider below, since the Item's transparent border would hide
// the group's `divide-y`.
import { ChevronRight } from "lucide-react"
import type { ReactNode } from "react"

import { Item, ItemActions, ItemContent, ItemTitle } from "@/components/ui/item"
import { cn } from "@/lib/utils"

export function AccountSettingsRow({
  id,
  title,
  valueText,
  valueIsMonospace = false,
  valueTone = "default",
  onOpen,
}: {
  id: string
  title: string
  valueText: ReactNode
  valueIsMonospace?: boolean
  // "problem": something stopped (a webhook gone or refused), in the destructive colour.
  valueTone?: "default" | "problem"
  onOpen(): void
}) {
  return (
    <Item asChild size="sm" className="min-h-14 rounded-none border-b-border first:rounded-t-2xl last:rounded-b-2xl last:border-b-transparent">
      <button id={id} type="button" onClick={onOpen} className="w-full text-left">
        <ItemContent className="flex-none">
          <ItemTitle className="text-[15px]">{title}</ItemTitle>
        </ItemContent>
        <ItemActions className="min-w-0 flex-1 justify-end gap-1.5">
          <span
            data-testid={`${id}_value`}
            className={cn(
              "min-w-0 truncate text-[15px]",
              valueIsMonospace && "font-mono",
              valueTone === "problem" ? "text-destructive" : "text-muted-foreground",
            )}
          >
            {valueText}
          </span>
          <ChevronRight aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
        </ItemActions>
      </button>
    </Item>
  )
}
