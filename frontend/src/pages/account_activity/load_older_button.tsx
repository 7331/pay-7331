// "Load older" under an `/activity` list: one full-width 44 px tap that reads the next page, the API's refusal under it.
import { ChevronDown } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"
import { touchTargetButtonClassName } from "@/lib/button_size_class_names"
import { cn } from "@/lib/utils"

export function LoadOlderButton({
  id,
  isLoading,
  loadError,
  onLoadOlder,
}: {
  id: string
  isLoading: boolean
  loadError: string
  onLoadOlder: () => void
}) {
  return (
    <div className="mt-3 flex flex-col gap-2">
      <Button id={id} type="button" variant="outline" disabled={isLoading} className={cn(touchTargetButtonClassName, "w-full")} onClick={onLoadOlder}>
        {isLoading ? <Spinner /> : <ChevronDown />} Load older
      </Button>
      {loadError ? <p className="text-center text-sm text-destructive">Can't load older: {loadError}</p> : null}
    </div>
  )
}
