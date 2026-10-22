// The calm "Confirming" ring: a track, an arc that eases most of the way round while the block is not final yet,
// and a slow breathing glow; the center holds whatever the screen puts there (the amount received). The finished
// state is the paid screen's check, so the arc never claims 100%. Reduced motion shows the arc filled that far, still.
// A Monero invoice counts its confirmations (PLAN.md section 14), so its screen passes how far round the arc stands.
import { motion, useReducedMotion } from "motion/react"
import type { ReactNode } from "react"

import { cn } from "@/lib/utils"

// How far round the arc settles while waiting for `finalized` (seconds on Polygon, PLAN.md section 1).
const waitingArcFraction = 0.72

export function ConfirmingProgressRing({
  children,
  className,
  arcFraction = waitingArcFraction,
}: {
  children?: ReactNode
  className?: string
  // Below 1: the finished state is the paid screen's check.
  arcFraction?: number
}) {
  const prefersReducedMotion = useReducedMotion()
  return (
    <div className={cn("relative grid size-36 place-items-center", className)}>
      <svg viewBox="0 0 144 144" className="absolute inset-0 size-full -rotate-90" aria-hidden="true">
        <circle cx="72" cy="72" r="66" fill="none" strokeWidth="6" className="stroke-foreground/[0.07]" />
        <motion.circle
          cx="72"
          cy="72"
          r="66"
          fill="none"
          strokeWidth="6"
          strokeLinecap="round"
          className="stroke-foreground"
          initial={prefersReducedMotion ? false : { pathLength: 0.04 }}
          animate={{ pathLength: arcFraction }}
          transition={{ duration: 2.4, ease: [0.16, 1, 0.3, 1] }}
        />
      </svg>
      {prefersReducedMotion ? null : (
        <motion.span
          aria-hidden="true"
          className="absolute inset-3 rounded-full bg-foreground/[0.04]"
          animate={{ opacity: [0.4, 1, 0.4] }}
          transition={{ duration: 2.4, repeat: Infinity, ease: "easeInOut" }}
        />
      )}
      <div className="relative">{children}</div>
    </div>
  )
}
