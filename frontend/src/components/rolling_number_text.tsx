// A shown amount that appears at its value and rolls only when it changes (a $5 invoice never flashes "$4.62" while
// counting up from zero, UX pass 2026-10-11), and always lands on
// `formatDisplayText(decimalText)` exactly: the in-between frames are display only (a float, never money math), the
// last frame is the API's string. The text is a Motion value rendered as the child of a motion element, so the
// frames skip React renders (https://motion.dev/docs/react-animation). Reduced motion shows the end value at once.
// Give the wrapper `tabular-nums` so the digits keep their width while they roll.
import { animate, motion, useMotionValue, useReducedMotion } from "motion/react"
import { useEffect, useRef } from "react"

import { isPlainDecimalString } from "@/lib/amount_formatting"
import { numberCountDurationSeconds, numberCountEase } from "@/lib/motion_presets"

function countFractionDigits(decimalText: string | null): number {
  return decimalText?.trim().split(".")[1]?.length ?? 0
}

// null for anything that is not a plain decimal (a missing amount shows as the formatter shows it, without rolling).
function readRollableNumber(decimalText: string | null): number | null {
  return decimalText != null && isPlainDecimalString(decimalText) ? Number(decimalText) : null
}

export function RollingNumberText({
  decimalText,
  formatDisplayText,
  className,
}: {
  decimalText: string | null
  // Turns a plain decimal string ("12.5") into what the page shows ("12.50", "$12.50"); must accept any decimal.
  formatDisplayText(decimalText: string | null): string
  className?: string
}) {
  const prefersReducedMotion = useReducedMotion()
  const finalDisplayText = formatDisplayText(decimalText)
  const shownText = useMotionValue(finalDisplayText)
  // The number on screen right now, so a change mid-roll continues from where the digits are; null until mounted.
  const shownNumber = useRef<number | null>(null)
  // The latest formatter without restarting the roll on every parent render (the payer page renders every second).
  const latestFormatDisplayText = useRef(formatDisplayText)
  useEffect(() => {
    latestFormatDisplayText.current = formatDisplayText
  })

  useEffect(() => {
    const targetNumber = readRollableNumber(decimalText)
    const fromNumber = shownNumber.current
    if (prefersReducedMotion || fromNumber === null || targetNumber === null || fromNumber === targetNumber) {
      shownNumber.current = targetNumber
      shownText.set(finalDisplayText)
      return
    }
    const fractionDigits = countFractionDigits(decimalText)
    const runningRoll = animate(fromNumber, targetNumber, {
      duration: numberCountDurationSeconds,
      ease: numberCountEase,
      onUpdate: (frameNumber) => {
        shownNumber.current = frameNumber
        shownText.set(latestFormatDisplayText.current(frameNumber.toFixed(fractionDigits)))
      },
      onComplete: () => {
        shownNumber.current = targetNumber
        shownText.set(finalDisplayText)
      },
    })
    return () => runningRoll.stop()
  }, [decimalText, finalDisplayText, prefersReducedMotion, shownText])

  return <motion.span className={className}>{shownText}</motion.span>
}
