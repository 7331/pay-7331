// Asking for money back from the page (PLAN.md section 6): the wallet that sent it connects, the server gives each
// sender a message to sign (`POST refund-offer`), the wallet signs it with personal_sign, and `POST refund-requests`
// takes the signature. Only the paying address can sign for its money; nothing moves by the signature itself.
import { fetchApiJson } from "@/lib/api_client"
import type { PublicInvoice, RefundOfferItem } from "@/lib/api_types"
import { requestWalletAccount, signTextWithWallet } from "@/lib/browser_wallet"
import { readPayerStrings } from "@/lib/payer_language"
import { publicInvoiceApiPath } from "@/hooks/use_invoice_polling"

// The invoice with its refund recorded, or why the connected wallet can't ask. A success carries no message: the
// screen after it says where the money goes.
export type RefundRequestResult = { updatedInvoice: PublicInvoice; refusalText: null } | { updatedInvoice: null; refusalText: string }

export async function requestRefundWithBrowserWallet(
  invoiceId: string,
  onProgress: (progressText: string) => void,
): Promise<RefundRequestResult> {
  const payerStrings = readPayerStrings()
  onProgress(payerStrings.connectWalletThatSent)
  const connectedAccount = await requestWalletAccount()
  const refundOfferResponse = await fetchApiJson<{ offers: RefundOfferItem[] }>(publicInvoiceApiPath(invoiceId, "/refund-offer"), {
    method: "POST",
  })
  const offers = refundOfferResponse.offers ?? []
  const matchingOffer = offers.find((offer) => offer.sender.toLowerCase() === connectedAccount.toLowerCase())
  if (!matchingOffer) {
    return {
      updatedInvoice: null,
      refusalText: payerStrings.walletDidNotPayInvoice(connectedAccount, offers.map((offer) => offer.sender).join(", ")),
    }
  }
  onProgress(payerStrings.signRefundRequest)
  const signature = await signTextWithWallet(matchingOffer.message, connectedAccount)
  const updatedInvoice = await fetchApiJson<PublicInvoice>(publicInvoiceApiPath(invoiceId, "/refund-requests"), {
    method: "POST",
    body: { nonce: matchingOffer.nonce, signature },
  })
  return { updatedInvoice, refusalText: null }
}
