// True where a `PageColumn`'s content and footer should rise in on mount while its top bar stays still (the payer page,
// which swaps screens by remounting them).
import { createContext } from "react"

export const PageContentRisesInContext = createContext(false)
