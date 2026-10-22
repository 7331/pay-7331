// The filled pill behind the picked option of a segmented row ($1/$2/$5/$10): render it inside the picked option only,
// with one `layoutId` per row, and Motion slides it from the old option to the new one
// (https://motion.dev/docs/react-layout-animations). Reduced motion moves it without the slide (MotionConfig).
// The option needs `relative` and its label `relative` so the text stays above the pill. The pill sits a few pixels
// inside the option with its radius in `style` (`borderRadius`, 10 px unless the option is round), which Motion keeps
// undistorted while the pill stretches.
import { motion } from "motion/react"

import { settleSpring } from "@/lib/motion_presets"
import { cn } from "@/lib/utils"

export function SlidingSelectionPill({
  layoutId,
  borderRadius = 10,
  className,
}: {
  layoutId: string
  borderRadius?: number
  className?: string
}) {
  return (
    <motion.span
      layoutId={layoutId}
      aria-hidden="true"
      transition={settleSpring}
      style={{ borderRadius }}
      className={cn("absolute inset-[3px] bg-foreground", className)}
    />
  )
}
