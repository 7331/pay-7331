// The few timings every micro-interaction shares (motion rule): fast, never blocking input.
// Springs use Motion's visualDuration and bounce (https://motion.dev/docs/react-transitions#spring).

// A number counting to its new value: quick out, gentle landing, like the old vanilla page's count.
export const numberCountEase = [0.16, 1, 0.3, 1] as const
export const numberCountDurationSeconds = 0.45

// Pills, badges and the "Copied" tag: a small overshoot.
export const popSpring = { type: "spring", visualDuration: 0.3, bounce: 0.4 } as const

// Screens, rows and buttons sliding in: no overshoot.
export const settleSpring = { type: "spring", visualDuration: 0.3, bounce: 0.1 } as const

// Something the finger pressed: shrinks a touch while held.
export const pressedScale = 0.97
