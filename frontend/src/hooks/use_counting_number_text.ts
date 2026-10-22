// For a typed amount (an <input>): counts the shown number from one value to another in whole steps, then hands the
// field back to the real state. Focusing or typing calls stopCounting first, so a count never fights the caret. Reduced motion
// skips the count. Motion's animate() for numbers (https://motion.dev/docs/react-animation); not AnimateNumber,
// which is Motion+ (https://motion.dev/docs/react-animate-number).
import { animate, useReducedMotion, type AnimationPlaybackControls } from "motion/react"
import { useCallback, useEffect, useRef, useState } from "react"

import { numberCountDurationSeconds, numberCountEase } from "@/lib/motion_presets"

export function useCountingNumberText() {
  // null while not counting: the field shows its real value.
  const [countingText, setCountingText] = useState<string | null>(null)
  const runningCount = useRef<AnimationPlaybackControls | null>(null)
  const prefersReducedMotion = useReducedMotion()

  const stopCounting = useCallback(() => {
    runningCount.current?.stop()
    runningCount.current = null
    setCountingText(null)
  }, [])

  const countBetweenNumbers = useCallback(
    (fromNumber: number, toNumber: number) => {
      runningCount.current?.stop()
      runningCount.current = null
      if (prefersReducedMotion || !Number.isFinite(fromNumber) || !Number.isFinite(toNumber) || fromNumber === toNumber) {
        setCountingText(null)
        return
      }
      runningCount.current = animate(fromNumber, toNumber, {
        duration: numberCountDurationSeconds,
        ease: numberCountEase,
        onUpdate: (shownNumber) => setCountingText(String(Math.round(shownNumber))),
        onComplete: () => {
          runningCount.current = null
          setCountingText(null)
        },
      })
    },
    [prefersReducedMotion],
  )

  useEffect(() => () => runningCount.current?.stop(), [])

  return { countingText, countBetweenNumbers, stopCounting }
}
