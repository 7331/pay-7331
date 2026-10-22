// The creator's share overlay over the invoice they just made (PLAN.md section 10): the amount and note, the receiver
// and coins ("To 0x6244…53Db · POL, USDC, USDT"), the link as a QR you tap to copy,
// the link on one line (middle ellipsis, the copy takes the whole string), "Share link" (the Web Share API where the
// browser has it, else a copy), "Make another" and "Done". A bottom Drawer on a phone, a Dialog from 768 px like the
// account sheet (shadcn's responsive pattern, https://ui.shadcn.com/docs/components/drawer). Both are modal, so
// nothing on the payer page under it takes a tap while it is open: the one-way coin choice stays untouched.
import { Copy, Plus, Share } from "lucide-react"
import { QRCodeSVG } from "qrcode.react"
import { useEffect, useRef, useState } from "react"

import { OneLineLinkText } from "@/components/one_line_link_text"
import { TapToCopyButton, copyTextToClipboard } from "@/components/tap_to_copy_button"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Drawer, DrawerContent, DrawerDescription, DrawerHeader, DrawerTitle } from "@/components/ui/drawer"
import type { CreatedInvoiceShare } from "@/hooks/use_created_invoice_share"
import { useMediaQuery } from "@/hooks/use_media_query"
import { shortenHexString } from "@/lib/amount_formatting"
import { primaryActionButtonClassName, touchTargetButtonClassName } from "@/lib/button_size_class_names"
import { cn } from "@/lib/utils"

const shareOverlayTitle = "Your link is ready"
// Coins named on the receiver line before the rest become "+N".
const coinsNamedOnReceiverLine = 3

// "To 0x6244…53Db · POL, USDC, USDT": where the money goes and in what, so a wrong paste or a missed coin is caught
// before the link goes out (2026-10-11).
function ReceiverAndCoinsLine({ receiverAddress, acceptedSymbols }: { receiverAddress: string; acceptedSymbols: string[] }) {
  if (!receiverAddress) return null
  const namedSymbols = acceptedSymbols.slice(0, coinsNamedOnReceiverLine)
  const unnamedCoinCount = acceptedSymbols.length - namedSymbols.length
  return (
    <p id="share_receiver_line" className="text-sm text-muted-foreground">
      To <span className="font-mono text-foreground" title={receiverAddress}>{shortenHexString(receiverAddress, 4)}</span>
      {namedSymbols.length > 0 ? ` · ${namedSymbols.join(", ")}${unnamedCoinCount > 0 ? ` +${unnamedCoinCount}` : ""}` : null}
    </p>
  )
}

export function CreatorShareOverlay({
  createdInvoiceShare,
  isOpen,
  onOpenChange,
  onMakeAnother,
}: {
  createdInvoiceShare: CreatedInvoiceShare
  isOpen: boolean
  onOpenChange(isOpen: boolean): void
  onMakeAnother(): void
}) {
  const isWideScreen = useMediaQuery("(min-width: 768px)")
  const descriptionContent = (
    <>
      <span className="font-medium text-foreground tabular-nums">{createdInvoiceShare.amountText}</span>
      {createdInvoiceShare.noteText ? ` · ${createdInvoiceShare.noteText}` : null}
    </>
  )
  const receiverAndCoinsLine = (
    <ReceiverAndCoinsLine
      receiverAddress={createdInvoiceShare.receiverAddress ?? ""}
      acceptedSymbols={createdInvoiceShare.acceptedSymbols ?? []}
    />
  )
  const shareContents = (
    <CreatorShareContents createdInvoiceShare={createdInvoiceShare} onDone={() => onOpenChange(false)} onMakeAnother={onMakeAnother} />
  )

  if (isWideScreen) {
    return (
      <Dialog open={isOpen} onOpenChange={onOpenChange}>
        <DialogContent data-testid="creator_share_overlay" className="sm:max-w-[400px]">
          <DialogHeader className="text-center">
            <DialogTitle className="text-xl font-semibold tracking-tight">{shareOverlayTitle}</DialogTitle>
            <DialogDescription className="line-clamp-1 text-[15px]">{descriptionContent}</DialogDescription>
            {receiverAndCoinsLine}
          </DialogHeader>
          {shareContents}
        </DialogContent>
      </Dialog>
    )
  }
  return (
    <Drawer open={isOpen} onOpenChange={onOpenChange}>
      <DrawerContent data-testid="creator_share_overlay">
        <div className="mx-auto flex w-full max-w-[440px] flex-col overflow-y-auto px-5 pb-[max(env(safe-area-inset-bottom),1.25rem)]">
          <DrawerHeader className="px-0 pt-3 pb-4">
            <DrawerTitle className="text-xl font-semibold tracking-tight">{shareOverlayTitle}</DrawerTitle>
            <DrawerDescription className="line-clamp-1 text-[15px]">{descriptionContent}</DrawerDescription>
            {receiverAndCoinsLine}
          </DrawerHeader>
          {shareContents}
        </div>
      </DrawerContent>
    </Drawer>
  )
}

function CreatorShareContents({
  createdInvoiceShare,
  onDone,
  onMakeAnother,
}: {
  createdInvoiceShare: CreatedInvoiceShare
  onDone(): void
  onMakeAnother(): void
}) {
  const { linkUrl, amountText } = createdInvoiceShare
  const [shareFeedback, setShareFeedback] = useState("")
  const shareFeedbackTimer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(shareFeedbackTimer.current), [])
  const canShare = typeof navigator.share === "function"

  async function shareInvoiceLink() {
    if (canShare) {
      try {
        await navigator.share({ title: `Pay ${amountText}`, url: linkUrl })
        return
      } catch (shareError) {
        if ((shareError as Error).name === "AbortError") return
      }
    }
    await copyTextToClipboard(linkUrl).catch(() => undefined)
    window.clearTimeout(shareFeedbackTimer.current)
    setShareFeedback("Link copied")
    shareFeedbackTimer.current = window.setTimeout(() => setShareFeedback(""), 1600)
  }

  return (
    <div className="flex min-w-0 flex-col">
      <TapToCopyButton
        textToCopy={linkUrl}
        accessibleLabel={`Copy the invoice link ${linkUrl}`}
        id="copy_link_button"
        className="flex w-full min-w-0 flex-col items-center gap-3 rounded-3xl border bg-card p-4"
      >
        {/* No entrance motion on the QR beyond the sheet's own: it must scan the moment it shows. */}
        <span id="invoice_link_qr" className="rounded-2xl bg-white p-3">
          <QRCodeSVG value={linkUrl} size={168} level="M" marginSize={0} title={`QR code for ${linkUrl}`} />
        </span>
        <OneLineLinkText linkUrl={linkUrl} id="invoice_link_text" className="justify-center" />
      </TapToCopyButton>
      <div className="mt-7 flex flex-col gap-2">
        <Button id="share_link_button" className={cn(primaryActionButtonClassName, "w-full")} onClick={() => void shareInvoiceLink()}>
          {canShare ? <Share /> : <Copy />} {shareFeedback || (canShare ? "Share link" : "Copy link")}
        </Button>
        <div className="grid grid-cols-2 gap-2">
          <Button id="make_another_invoice_button" variant="outline" className={cn(touchTargetButtonClassName, "w-full")} onClick={onMakeAnother}>
            <Plus /> Make another
          </Button>
          <Button id="close_share_overlay_button" variant="secondary" className={cn(touchTargetButtonClassName, "w-full")} onClick={onDone}>
            Done
          </Button>
        </div>
      </div>
    </div>
  )
}
