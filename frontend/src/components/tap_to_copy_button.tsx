// A whole-area button that copies an exact string (the API's amount, address or link); "Copied" pops in under it with
// a check that draws itself, plus a short buzz on phones that vibrate, for a moment without moving the layout. Where a
// line sits right under it (the payer's amount and address), its trailing Copy icon turns into the check instead.
import { Copy } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { useEffect, useRef, useState, type ReactNode } from "react"

import { DrawnCheckPath } from "@/components/drawn_check_path"
import { vibrateBriefly } from "@/lib/haptic_feedback"
import { popSpring, pressedScale } from "@/lib/motion_presets"
import { cn } from "@/lib/utils"

export async function copyTextToClipboard(text: string): Promise<void> {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text)
    return
  }
  // Plain http (local testing): a hidden textarea and execCommand.
  const temporaryTextarea = document.createElement("textarea")
  temporaryTextarea.value = text
  temporaryTextarea.setAttribute("readonly", "")
  temporaryTextarea.style.position = "fixed"
  temporaryTextarea.style.opacity = "0"
  document.body.appendChild(temporaryTextarea)
  temporaryTextarea.select()
  document.execCommand("copy")
  temporaryTextarea.remove()
}

export function TapToCopyButton({
  textToCopy,
  accessibleLabel,
  children,
  className,
  id,
  testId,
  feedbackShownBy = "floating_tag",
  onCopy,
  copiedFeedbackText = "Copied",
  copyFailedFeedbackText = "Select it and copy",
}: {
  textToCopy: string
  accessibleLabel: string
  children: ReactNode
  className?: string
  id?: string
  testId?: string
  // "floating_tag": "Copied" pops in under the button; "trailing_icon": a Copy icon after the text turns into a check.
  feedbackShownBy?: "floating_tag" | "trailing_icon"
  // Told of every tap, whether or not the browser let the copy through (the payer page's "I've paid" nudge).
  onCopy?(): void
  // The payer's pages pass their own words; everywhere else stays English.
  copiedFeedbackText?: string
  copyFailedFeedbackText?: string
}) {
  const [copyFeedback, setCopyFeedback] = useState<"" | "copied" | "copy_failed">("")
  const copyFeedbackText = copyFeedback === "copied" ? copiedFeedbackText : copyFeedback === "copy_failed" ? copyFailedFeedbackText : ""
  const resetTimer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(resetTimer.current), [])

  async function copyAndShowFeedback() {
    window.clearTimeout(resetTimer.current)
    onCopy?.()
    try {
      await copyTextToClipboard(textToCopy)
      vibrateBriefly()
      setCopyFeedback("copied")
    } catch {
      setCopyFeedback("copy_failed")
    }
    resetTimer.current = window.setTimeout(() => setCopyFeedback(""), 1600)
  }

  return (
    <motion.button
      type="button"
      whileTap={{ scale: pressedScale }}
      id={id}
      data-testid={testId}
      aria-label={accessibleLabel}
      onClick={copyAndShowFeedback}
      className={cn(
        "relative rounded-2xl outline-none transition-colors focus-visible:ring-3 focus-visible:ring-ring/50 active:bg-muted/70",
        className,
      )}
    >
      {children}
      {feedbackShownBy === "trailing_icon" ? (
        <>
          {/* A Copy icon says the text can be copied; it turns into a drawn check in place, covering nothing. */}
          <span aria-hidden="true" className={cn("inline-grid size-5 shrink-0 place-items-center align-middle", copyFeedback === "copied" ? "text-success" : "text-muted-foreground")}>
            {copyFeedback === "copied" ? (
              <svg viewBox="0 0 24 24" className="size-4" fill="none" stroke="currentColor" strokeWidth={3} strokeLinecap="round" strokeLinejoin="round">
                <DrawnCheckPath durationSeconds={0.25} />
              </svg>
            ) : (
              <Copy className="size-4" />
            )}
          </span>
          <span aria-live="polite" className="sr-only">
            {copyFeedbackText}
          </span>
        </>
      ) : (
      <span aria-live="polite" className="pointer-events-none absolute inset-x-0 -bottom-5 flex justify-center">
        <AnimatePresence>
          {copyFeedback ? (
            <motion.span
              key={copyFeedback}
              initial={{ opacity: 0, y: -4, scale: 0.8 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 2, transition: { duration: 0.15 } }}
              transition={popSpring}
              className="flex items-center gap-1 rounded-full bg-foreground py-0.5 pr-2 pl-1.5 text-[11px] font-medium text-background"
            >
              {copyFeedback === "copied" ? (
                <svg viewBox="0 0 24 24" className="size-3" fill="none" stroke="currentColor" strokeWidth={3.5} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <DrawnCheckPath durationSeconds={0.25} />
                </svg>
              ) : null}
              {copyFeedbackText}
            </motion.span>
          ) : null}
        </AnimatePresence>
      </span>
      )}
    </motion.button>
  )
}
