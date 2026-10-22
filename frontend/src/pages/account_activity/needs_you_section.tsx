// `/activity`'s "Needs you" (2026-10-14): the loaded invoices that wait on the account, each one sentence
// (describeInvoiceNeedingAccount) and "Resolve", which opens the invoice's overlay; and a Monero wallet whose run of
// unpaid subaddresses reached the lookahead warning, with the exact widening line and "I set it" (PLAN.md section 14).
// Nothing shows while none does. Its own component so the clock that ends an underpaid invoice's window re-renders only
// this.
import { CircleAlert } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { useState } from "react"
import { toast } from "sonner"

import { MoneroLookaheadCommand } from "@/components/account_monero_wallet_settings"
import { Button } from "@/components/ui/button"
import { Item, ItemActions, ItemContent, ItemDescription, ItemGroup, ItemMedia, ItemTitle } from "@/components/ui/item"
import { Spinner } from "@/components/ui/spinner"
import { widenedMoneroLookaheadMinorCount } from "@/lib/monero_wallet_settings"
import { useCurrentTimeEverySecond } from "@/hooks/use_current_time_every_second"
import { describeError } from "@/lib/api_client"
import type { AccountInvoice, AccountMoneroWallet } from "@/lib/api_types"
import { touchTargetButtonClassName } from "@/lib/button_size_class_names"
import { settleSpring } from "@/lib/motion_presets"
import { describeInvoiceAmount, describeInvoiceNeedingAccount } from "@/pages/account_activity/invoice_descriptions"

export function NeedsYouSection({
  invoices,
  onResolveInvoice,
  moneroWalletsNeedingLookahead,
  onConfirmMoneroLookahead,
}: {
  invoices: AccountInvoice[]
  onResolveInvoice(invoiceId: string): void
  moneroWalletsNeedingLookahead: AccountMoneroWallet[]
  onConfirmMoneroLookahead(chainKey: string): Promise<void>
}) {
  const currentTimeMilliseconds = useCurrentTimeEverySecond()
  const invoicesNeedingAccount = invoices.flatMap((invoice) => {
    const needText = describeInvoiceNeedingAccount(invoice, currentTimeMilliseconds)
    return needText ? [{ invoice, needText }] : []
  })
  return (
    <AnimatePresence initial={false}>
      {invoicesNeedingAccount.length > 0 || moneroWalletsNeedingLookahead.length > 0 ? (
        <motion.section
          key="needs_you"
          aria-labelledby="needs_you_heading"
          initial={{ opacity: 0, height: 0 }}
          animate={{ opacity: 1, height: "auto" }}
          exit={{ opacity: 0, height: 0 }}
          transition={settleSpring}
          className="overflow-hidden"
        >
          <h2 id="needs_you_heading" className="mt-4 mb-2 text-sm font-medium text-muted-foreground">
            Needs you
          </h2>
          <ItemGroup id="needs_you_list" className="gap-0 rounded-2xl border border-warning/40 bg-warning/10">
            {moneroWalletsNeedingLookahead.map((moneroWallet) => (
              <MoneroLookaheadNeedsYouRow key={moneroWallet.chain_key} moneroWallet={moneroWallet} onConfirmMoneroLookahead={onConfirmMoneroLookahead} />
            ))}
            {invoicesNeedingAccount.map(({ invoice, needText }) => (
              <Item key={invoice.id} size="sm" className="min-h-16 rounded-none border-b-warning/30 last:border-b-transparent" data-testid="needs_you_row" data-invoice-id={invoice.id}>
                <ItemMedia variant="icon" className="text-warning">
                  <CircleAlert />
                </ItemMedia>
                <ItemContent className="min-w-0">
                  <ItemTitle className="tabular-nums">{describeInvoiceAmount(invoice)}</ItemTitle>
                  <ItemDescription className="line-clamp-3 text-foreground">{needText}</ItemDescription>
                </ItemContent>
                <ItemActions>
                  <Button
                    type="button"
                    variant="secondary"
                    data-testid="resolve_needs_you_button"
                    className={touchTargetButtonClassName}
                    aria-label={`Resolve the ${describeInvoiceAmount(invoice)} invoice`}
                    onClick={() => onResolveInvoice(invoice.id)}
                  >
                    Resolve
                  </Button>
                </ItemActions>
              </Item>
            ))}
          </ItemGroup>
        </motion.section>
      ) : null}
    </AnimatePresence>
  )
}

// The receiver's wallet scans a fixed number of subaddresses past the last one paid; this many unpaid links in a row
// would hide the next payment from it, and at the limit new XMR links are refused. Once the wider lookahead is confirmed
// there is nothing left to widen: one line says where new links stop, with no command and no button.
function MoneroLookaheadNeedsYouRow({
  moneroWallet,
  onConfirmMoneroLookahead,
}: {
  moneroWallet: AccountMoneroWallet
  onConfirmMoneroLookahead(chainKey: string): Promise<void>
}) {
  const [isConfirming, setIsConfirming] = useState(false)
  const lookaheadIsWidened = moneroWallet.confirmed_lookahead_minor_count >= widenedMoneroLookaheadMinorCount

  async function confirmLookahead() {
    setIsConfirming(true)
    try {
      await onConfirmMoneroLookahead(moneroWallet.chain_key)
      toast.success("Saved. New XMR links go on.")
    } catch (confirmFailure) {
      toast.error(describeError(confirmFailure))
    } finally {
      setIsConfirming(false)
    }
  }

  return (
    <Item size="sm" className="min-h-16 rounded-none border-b-warning/30 last:border-b-transparent" data-testid="needs_you_monero_lookahead_row" data-chain-key={moneroWallet.chain_key}>
      <ItemMedia variant="icon" className="text-warning">
        <CircleAlert />
      </ItemMedia>
      <ItemContent className="min-w-0 gap-2">
        <ItemTitle>Monero wallet</ItemTitle>
        {lookaheadIsWidened ? (
          <ItemDescription className="line-clamp-none text-foreground">
            New XMR links stop after {moneroWallet.confirmed_lookahead_minor_count - 1} unpaid until a payment arrives.
          </ItemDescription>
        ) : (
          <>
            <ItemDescription className="line-clamp-none text-foreground">
              {moneroWallet.unreceived_minor_gap} XMR links since the last paid one. Run this in your wallet so it keeps seeing payments:
            </ItemDescription>
            <MoneroLookaheadCommand id={`needs_you_monero_lookahead_command_${moneroWallet.chain_key}`} />
          </>
        )}
      </ItemContent>
      {lookaheadIsWidened ? null : (
        <ItemActions className="self-end">
          <Button
            type="button"
            variant="secondary"
            data-testid="confirm_monero_lookahead_button"
            className={touchTargetButtonClassName}
            disabled={isConfirming}
            onClick={() => void confirmLookahead()}
          >
            {isConfirming ? <Spinner /> : null}I set it
          </Button>
        </ItemActions>
      )}
    </Item>
  )
}
