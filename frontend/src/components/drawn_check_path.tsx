// A check mark stroke that draws itself (Motion's pathLength, https://motion.dev/docs/react-svg-animation). Reduced
// motion shows it drawn: MotionConfig's reducedMotion="user" stops transforms only, so the draw is skipped here.
import { motion, useReducedMotion } from "motion/react"

export function DrawnCheckPath({ delaySeconds = 0.05, durationSeconds = 0.3 }: { delaySeconds?: number; durationSeconds?: number }) {
  const prefersReducedMotion = useReducedMotion()
  return (
    <motion.path
      d="M5 12.5l4.5 4.5L19 7.5"
      initial={prefersReducedMotion ? false : { pathLength: 0 }}
      animate={{ pathLength: 1 }}
      transition={{ duration: durationSeconds, delay: delaySeconds, ease: "easeOut" }}
    />
  )
}
