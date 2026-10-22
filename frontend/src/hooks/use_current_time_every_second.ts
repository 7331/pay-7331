// The time now, refreshed once a second, for countdowns and for screens that change when a deadline passes.
import { useEffect, useState } from "react"

export function useCurrentTimeEverySecond(): number {
  const [currentTimeMilliseconds, setCurrentTimeMilliseconds] = useState(() => Date.now())
  useEffect(() => {
    const intervalId = window.setInterval(() => setCurrentTimeMilliseconds(Date.now()), 1000)
    return () => window.clearInterval(intervalId)
  }, [])
  return currentTimeMilliseconds
}
