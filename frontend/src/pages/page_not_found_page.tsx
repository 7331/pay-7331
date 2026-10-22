import { Link } from "react-router"

import { BrandMark } from "@/components/brand_mark"
import { PageColumn } from "@/components/page_column"
import { Button } from "@/components/ui/button"
import { primaryActionButtonClassName } from "@/lib/button_size_class_names"
import { cn } from "@/lib/utils"

export function PageNotFoundPage() {
  return (
    <PageColumn
      topBar={
        <header className="flex min-h-14 items-center">
          <BrandMark />
        </header>
      }
      footer={
        <Button asChild className={cn(primaryActionButtonClassName, "w-full")}>
          <Link to="/">Make an invoice</Link>
        </Button>
      }
    >
      <section className="flex flex-1 flex-col items-center justify-center gap-2 pb-16 text-center">
        <h1 className="text-3xl font-semibold tracking-tight">Nothing here</h1>
        <p className="text-[15px] text-muted-foreground">This page doesn't exist.</p>
      </section>
    </PageColumn>
  )
}
