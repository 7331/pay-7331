// A short buzz on phones that have it (Android Chrome; iOS Safari has no Vibration API), nothing elsewhere
// (https://developer.mozilla.org/en-US/docs/Web/API/Navigator/vibrate).
export function vibrateBriefly(): void {
  if (typeof navigator.vibrate === "function") navigator.vibrate(12)
}
