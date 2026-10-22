// A link on one line: the head shrinks into an ellipsis, the tail (the end of the invoice id) stays whole, so a link
// never breaks mid-id. The text content is still the whole link, for screen readers, a copy by selection and the e2e.
import { cn } from "@/lib/utils"

// The tail kept whole after the ellipsis: enough of the invoice id to tell two links apart.
const linkTailLength = 8

export function OneLineLinkText({ linkUrl, id, testId, className }: { linkUrl: string; id?: string; testId?: string; className?: string }) {
  const linkWithoutScheme = linkUrl.replace(/^https?:\/\//, "")
  const schemePrefix = linkUrl.slice(0, linkUrl.length - linkWithoutScheme.length)
  const linkHead = linkUrl.slice(0, linkUrl.length - linkTailLength)
  const linkTail = linkUrl.slice(-linkTailLength)
  return (
    <span id={id} data-testid={testId} className={cn("flex w-full min-w-0 font-mono text-[13px] text-muted-foreground", className)}>
      <span className="truncate">
        <span className="sr-only">{schemePrefix}</span>
        {linkHead.slice(schemePrefix.length)}
      </span>
      <span className="shrink-0">{linkTail}</span>
    </span>
  )
}
