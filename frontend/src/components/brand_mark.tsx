import { Link } from "react-router"

export function BrandMark({ linksHome = true }: { linksHome?: boolean }) {
  const brandText = (
    <span className="text-[17px] font-semibold tracking-tight">
      pay<span className="px-0.5 text-muted-foreground">·</span>7331
    </span>
  )
  if (!linksHome) return brandText
  return (
    <Link to="/" className="-mx-2 flex min-h-11 items-center rounded-lg px-2">
      {brandText}
    </Link>
  )
}
