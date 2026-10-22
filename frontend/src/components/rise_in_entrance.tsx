// Fades and slides its children up a few pixels as they mount (the old pages' `.rise`), e.g. a footer button that
// appears once something is picked. Input works from the first frame. Reduced motion keeps only the fade
// (MotionConfig reducedMotion="user", https://motion.dev/docs/react-accessibility).
import { motion } from "motion/react"
import type { ReactNode } from "react"

import { settleSpring } from "@/lib/motion_presets"

export function RiseInEntrance({ children, className, delaySeconds = 0 }: { children: ReactNode; className?: string; delaySeconds?: number }) {
  return (
    <motion.div
      className={className}
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ ...settleSpring, delay: delaySeconds }}
    >
      {children}
    </motion.div>
  )
}
