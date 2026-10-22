// A small timer toward a deadline: a still clock face whose wedge shrinks as time runs out, with "12:34 left" beside
// it. No spinning arc, so it never reads as loading. Give it `key={deadlineIso}` so a new deadline starts a full face.
import { useState } from "react"

import { readPayerStrings } from "@/lib/payer_language"
import { formatCountdown, millisecondsUntil } from "@/lib/time_formatting"
import { cn } from "@/lib/utils"

const faceRadius = 8
// The wedge is a stroke as wide as its circle is big: a dash of it is a pie slice, with a gap to the face.
const wedgeRadius = 2.75
const wedgeCircumference = 2 * Math.PI * wedgeRadius

export function CountdownRing({
  deadlineIso,
  currentTimeMilliseconds,
  accessibleLabel,
}: {
  deadlineIso: string
  currentTimeMilliseconds: number
  accessibleLabel: string
}) {
  // The wedge shrinks from what was left when this screen first saw the deadline.
  const [firstRemainingMilliseconds] = useState(() => Math.max(millisecondsUntil(deadlineIso), 1))
  const remainingMilliseconds = Math.max(0, new Date(deadlineIso).getTime() - currentTimeMilliseconds)
  const remainingFraction = Math.min(1, remainingMilliseconds / firstRemainingMilliseconds)
  const isRunningOut = remainingMilliseconds < 120_000
  // "29:59 left" in English, "noch 29:59" in German: the words go either side of the time the e2e reads.
  const payerStrings = readPayerStrings()
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 text-[13px] tabular-nums",
        isRunningOut ? "text-destructive" : "text-muted-foreground",
      )}
      title={accessibleLabel}
    >
      <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true" className="-rotate-90">
        <circle cx="9" cy="9" r={faceRadius} fill="none" strokeWidth="1.5" stroke="currentColor" />
        <circle
          cx="9"
          cy="9"
          r={wedgeRadius}
          fill="none"
          strokeWidth={wedgeRadius * 2}
          stroke="currentColor"
          strokeDasharray={wedgeCircumference}
          strokeDashoffset={wedgeCircumference * (1 - remainingFraction)}
        />
      </svg>
      <span className="sr-only">{accessibleLabel} </span>
      <span>
        {payerStrings.countdownTextBefore ? <span aria-hidden="true">{payerStrings.countdownTextBefore}</span> : null}
        <span data-testid="invoice_countdown" className="font-mono">
          {formatCountdown(remainingMilliseconds)}
        </span>
        {payerStrings.countdownTextAfter ? <span aria-hidden="true">{payerStrings.countdownTextAfter}</span> : null}
      </span>
    </span>
  )
}
