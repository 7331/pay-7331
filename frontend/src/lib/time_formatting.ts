export function millisecondsUntil(isoTimestamp: string): number {
  return new Date(isoTimestamp).getTime() - Date.now()
}

// 754000 ms reads "12:34"; over an hour reads "1:02:03".
export function formatCountdown(remainingMilliseconds: number): string {
  const totalSeconds = Math.max(0, Math.floor(remainingMilliseconds / 1000))
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  const twoDigits = (value: number) => String(value).padStart(2, "0")
  return hours > 0 ? `${hours}:${twoDigits(minutes)}:${twoDigits(seconds)}` : `${minutes}:${twoDigits(seconds)}`
}

export function formatLocalDateTime(isoTimestamp: string): string {
  return new Date(isoTimestamp).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })
}

// "17:42" today, "Oct 8" before.
export function formatShortTime(isoTimestamp: string): string {
  const moment = new Date(isoTimestamp)
  const isToday = moment.toDateString() === new Date().toDateString()
  return isToday
    ? moment.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
    : moment.toLocaleDateString(undefined, { month: "short", day: "numeric" })
}

// "17:42", whatever the day: a row under its day's heading.
export function formatClockTime(isoTimestamp: string): string {
  return new Date(isoTimestamp).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
}

// A list's day heading: "Today", "Yesterday", then "Mon, Oct 12" (with the year once it is another year's).
export function formatDayHeading(isoTimestamp: string): string {
  const moment = new Date(isoTimestamp)
  const today = new Date()
  const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1)
  if (moment.toDateString() === today.toDateString()) return "Today"
  if (moment.toDateString() === yesterday.toDateString()) return "Yesterday"
  return moment.toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    year: moment.getFullYear() === today.getFullYear() ? undefined : "numeric",
  })
}
