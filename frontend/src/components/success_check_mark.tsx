// The success mark: a green disc that pops in with a soft halo and a check that draws itself (Motion; reduced motion
// shows the end state).
import { motion, useReducedMotion } from "motion/react"

import { DrawnCheckPath } from "@/components/drawn_check_path"
import { cn } from "@/lib/utils"

export function SuccessCheckMark({ sizeClassName = "size-20", tone = "success" }: { sizeClassName?: string; tone?: "success" | "muted" }) {
  const prefersReducedMotion = useReducedMotion()
  return (
    <div className={cn("relative grid place-items-center", sizeClassName)} aria-hidden="true">
      <motion.span
        className={cn("absolute inset-0 rounded-full", tone === "success" ? "bg-success/30" : "bg-muted")}
        initial={prefersReducedMotion ? { opacity: 0 } : { opacity: 0.6, scale: 1 }}
        animate={{ opacity: 0, scale: 1.8 }}
        transition={{ duration: 0.9, delay: 0.2, ease: "easeOut" }}
      />
      <motion.span
        className={cn(
          "absolute inset-0 grid place-items-center rounded-full",
          tone === "success" ? "bg-success text-success-foreground" : "bg-muted text-muted-foreground",
        )}
        initial={prefersReducedMotion ? false : { scale: 0.4, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }}
        transition={{ type: "spring", visualDuration: 0.4, bounce: 0.3 }}
      >
        <svg viewBox="0 0 24 24" className="size-1/2" fill="none" stroke="currentColor" strokeWidth={2.8} strokeLinecap="round" strokeLinejoin="round">
          <DrawnCheckPath delaySeconds={0.2} durationSeconds={0.45} />
        </svg>
      </motion.span>
    </div>
  )
}
