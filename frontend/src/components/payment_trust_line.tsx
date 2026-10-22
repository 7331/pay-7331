// One quiet line under the coin choice (`/i/` and `/l/`) answering "is this a scam?" (UX pass 2026-10-11): tapping it
// opens three sentences on where the money goes in the shared ResponsiveSheet (a Drawer on a phone, a Dialog from
// 768 px). On EVM the sentences hold because a deposit address is a CREATE2 forwarder with the receiver baked in and no
// owner (PLAN.md section 3); on Monero because the address is a subaddress of the receiver's own wallet and the server
// holds a view key only (PLAN.md section 14).
import { ShieldCheck } from "lucide-react"
import { useState } from "react"

import { ResponsiveSheet } from "@/components/responsive_sheet"
import type { InvoiceChain } from "@/lib/api_types"
import { readPayerStrings } from "@/lib/payer_language"

export function PaymentTrustLine({ chainKind = "evm" }: { chainKind?: InvoiceChain["kind"] }) {
  const [trustSheetIsOpen, setTrustSheetIsOpen] = useState(false)
  const payerStrings = readPayerStrings()
  return (
    <>
      <button
        type="button"
        id="payment_trust_line"
        onClick={() => setTrustSheetIsOpen(true)}
        className="mx-auto mt-4 flex min-h-11 items-center gap-1.5 rounded-lg px-2 text-[13px] text-muted-foreground outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
      >
        <ShieldCheck className="size-3.5" />
        {payerStrings.trustLine}
      </button>
      <ResponsiveSheet
        isOpen={trustSheetIsOpen}
        onOpenChange={setTrustSheetIsOpen}
        title={payerStrings.trustSheetTitle}
        description={payerStrings.trustSheetDescription}
      >
        <div data-testid="payment_trust_sentences" className="flex flex-col gap-3 text-[15px] leading-relaxed">
          {chainKind === "monero" ? (
            <>
              <p>{payerStrings.trustMoneroOwnAddress}</p>
              <p>{payerStrings.trustMoneroStraightToReceiver}</p>
            </>
          ) : (
            <>
              <p>{payerStrings.trustEvmOwnDepositAddress}</p>
              <p>{payerStrings.trustEvmOnlyPassesOn}</p>
            </>
          )}
          <p>{payerStrings.trustNeverHoldsMoney}</p>
        </div>
      </ResponsiveSheet>
    </>
  )
}
