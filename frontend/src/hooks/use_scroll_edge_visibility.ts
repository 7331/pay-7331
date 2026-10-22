// Whether a scroller has content hidden above or below its view, for PageColumn's hairlines under the top bar and over
// the footer: a row cut off at a pinned bar reads as broken unless the bar's edge says "more under here". Measured on
// scroll and whenever the scroller or its content resizes (a screen swapping state grows or shrinks its content).
import { useEffect, useState } from "react"

type ScrollEdgeVisibility = { contentIsAboveView: boolean; contentIsBelowView: boolean }

const noContentHidden: ScrollEdgeVisibility = { contentIsAboveView: false, contentIsBelowView: false }

export function useScrollEdgeVisibility() {
  const [scroller, setScroller] = useState<HTMLElement | null>(null)
  const [scrollEdgeVisibility, setScrollEdgeVisibility] = useState(noContentHidden)
  useEffect(() => {
    if (!scroller) return
    const measureScrollEdges = () => {
      const contentIsAboveView = scroller.scrollTop > 0
      // A pixel of slack: a fractional scrollHeight can leave scrollTop + clientHeight a hair short at the very end.
      const contentIsBelowView = scroller.scrollTop + scroller.clientHeight < scroller.scrollHeight - 1
      setScrollEdgeVisibility((previous) =>
        previous.contentIsAboveView === contentIsAboveView && previous.contentIsBelowView === contentIsBelowView
          ? previous
          : { contentIsAboveView, contentIsBelowView },
      )
    }
    measureScrollEdges()
    scroller.addEventListener("scroll", measureScrollEdges, { passive: true })
    const resizeObserver = new ResizeObserver(measureScrollEdges)
    resizeObserver.observe(scroller)
    for (const scrollerChild of scroller.children) resizeObserver.observe(scrollerChild)
    return () => {
      scroller.removeEventListener("scroll", measureScrollEdges)
      resizeObserver.disconnect()
    }
  }, [scroller])
  return { setScroller, ...scrollEdgeVisibility }
}
