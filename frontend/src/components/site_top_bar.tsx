import type { ReactNode } from "react"
import { Link } from "react-router"

import { AccountSheetTrigger } from "@/components/account_sheet"
import { BrandMark } from "@/components/brand_mark"

// The bar over the account pages: the brand, and "Sign in" or "@username" opening the account sheet.
export function SiteTopBar({ trailing }: { trailing?: ReactNode }) {
  return (
    <header className="flex min-h-14 items-center justify-between gap-3">
      <BrandMark />
      <div className="flex items-center gap-1">
        <Link to="/docs" className="flex min-h-11 items-center rounded-lg px-2 text-sm text-muted-foreground hover:text-foreground">
          Docs
        </Link>
        {trailing}
        <AccountSheetTrigger />
      </div>
    </header>
  )
}
