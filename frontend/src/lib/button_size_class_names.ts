// The app's two Button heights beyond shadcn's sizes, passed as `className` to the stock Button (and to AlertDialog's
// buttons, which wrap it); `cn` there merges them over the size's height, padding, radius and text (no
// hand-rolled shadcn, so components/ui/ stays exactly what the shadcn CLI writes).

// The screen's one primary action on a phone: 52 px tall, full width by the caller.
export const primaryActionButtonClassName =
  "h-13 gap-2 rounded-2xl px-5 text-base font-semibold [&_svg:not([class*='size-'])]:size-5"

// A secondary action or a top-bar control that still meets the 44 px tap target.
export const touchTargetButtonClassName = "h-11 gap-1.5 rounded-xl px-3.5 text-[15px] font-medium"
