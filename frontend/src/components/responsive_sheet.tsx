// A Drawer on a phone, a Dialog from 768 px (shadcn's responsive pattern, https://ui.shadcn.com/docs/components/drawer),
// controlled by the caller, with its own title for screen readers. `/settings` opens one per row, `/` uses it for the
// accepted coins and the invoice options. The description is one short line, only where it says what happens before
// the user acts; without one the content drops `aria-describedby`, as Radix asks
// (https://www.radix-ui.com/primitives/docs/components/dialog#description).
import type { ReactNode } from "react"

import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Drawer, DrawerContent, DrawerDescription, DrawerHeader, DrawerTitle } from "@/components/ui/drawer"
import { useMediaQuery } from "@/hooks/use_media_query"

// shadcn's `md` breakpoint, as in its responsive Dialog/Drawer example and the account sheet.
const wideScreenMediaQuery = "(min-width: 768px)"

export function ResponsiveSheet({
  isOpen,
  onOpenChange,
  title,
  description,
  sheetId,
  testId,
  children,
}: {
  isOpen: boolean
  onOpenChange: (nextIsOpen: boolean) => void
  title: string
  description?: ReactNode
  // The id and test id on the sheet's content, for the e2e.
  sheetId?: string
  testId?: string
  children: ReactNode
}) {
  const isWideScreen = useMediaQuery(wideScreenMediaQuery)
  const describedByProps = description ? {} : { "aria-describedby": undefined }
  if (isWideScreen) {
    return (
      <Dialog open={isOpen} onOpenChange={onOpenChange}>
        <DialogContent id={sheetId} data-testid={testId} {...describedByProps} className="max-h-[90dvh] gap-0 overflow-y-auto rounded-3xl p-7 sm:max-w-[440px]">
          <DialogHeader className="pb-4 text-left">
            <DialogTitle className="text-2xl font-semibold tracking-tight">{title}</DialogTitle>
            {description ? <DialogDescription className="text-[15px]">{description}</DialogDescription> : null}
          </DialogHeader>
          {children}
        </DialogContent>
      </Dialog>
    )
  }
  return (
    <Drawer open={isOpen} onOpenChange={onOpenChange}>
      <DrawerContent id={sheetId} data-testid={testId} {...describedByProps}>
        <div className="mx-auto w-full max-w-[440px] overflow-y-auto px-5 pb-[max(env(safe-area-inset-bottom),1.25rem)]">
          <DrawerHeader className="px-0 pt-5 pb-4 text-left md:text-left">
            <DrawerTitle className="text-2xl font-semibold tracking-tight">{title}</DrawerTitle>
            {description ? <DrawerDescription className="text-[15px]">{description}</DrawerDescription> : null}
          </DrawerHeader>
          {children}
        </div>
      </DrawerContent>
    </Drawer>
  )
}
