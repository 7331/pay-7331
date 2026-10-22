// The words on the payer's pages (`/i/` and `/l/`), one object per language (PLAN.md section 10, 2026-10-21). Amounts,
// addresses, coin symbols and network names come in as arguments and are never translated or reformatted here: the
// crypto amount stays the exact formatted API string. Each language file is typed against this
// interface, so a missing or extra sentence fails the typecheck.
import type { InterfaceLabels } from "@/components/interface_labels_context"

// What became of money in a coin the invoice did not ask for (INC-001).
type StrayPaymentOutcome = "being_sent_back" | "was_sent_back" | "goes_to_seller"

// The API refusals a payer can meet, in the payer's words. English keeps `describeError`'s sentences in
// lib/api_client.ts, the one English source, so the English strings carry none of these.
export interface PayerErrorSentences {
  cantReachServer: string
  tooManyTries(waitText: string): string
  waitOneSecond: string
  waitSeconds(secondCount: number): string
  waitOneMinute: string
  waitMinutes(minuteCount: number): string
  requestNotValid: string
  invoiceNotFound: string
  coinAlreadyPicked: string
  expiredBeforeCoinPicked: string
  invoiceWasCancelled: string
  refundUnderwayNoMorePayments: string
  cantGetNewPrice: string
  pickCoinFirst: string
  invoiceChangedReload: string
  paymentLinkNotFound: string
  soldOut: string
  paymentLinkClosed: string
  refundReceiverPaidDirectly: string
  refundOnceTimeToPayIsUp: string
  refundAlreadyUnderway: string
  nothingToRefund: string
  refundOfferGone: string
  refundOfferUsed: string
  refundSignatureNotFromPayer: string
  pricesUnavailable: string
  serverCantFinish: string
  paymentsNotSetUp: string
  networkNotAvailable: string
  coinNotAvailable: string
  moneroUnavailable: string
  networkCantDoThis: string
  notHereAnymore: string
  notAllowed: string
  changedInTheMeantime: string
  somethingBrokeOnOurSide: string
  somethingWentWrong: string
}

export interface PayerStrings {
  // Loading and broken links.
  makeYourOwnInvoice: string
  thisLinkDoesNotWork: string
  checkYouCopiedTheWholeLink: string
  checkYouHaveTheWholeLink: string
  connectionProblemStillTrying(problemText: string): string

  // Pieces several screens share.
  readWholeNote: string
  showLessOfNote: string
  needHelp: string
  contactTheSeller: string
  latePaymentWentToReceiver: string
  sweepHeldUp: string
  refundRowBackTo: string
  viewRefundOnExplorer: string
  refundStatusQueued: string
  refundStatusSending: string
  refundStatusConfirming: string
  refundStatusSent: string
  refundStatusFailed: string
  refundStatusChecking: string
  copiedFeedback: string
  selectItAndCopyFeedback: string

  // Status headings (the e2e reads the English ones).
  statusChooseHowToPay: string
  statusExpired: string
  statusTimesUp: string
  statusAwaitingPayment: string
  statusConfirming: string
  statusRefundHeldUp: string
  statusRefundOnItsWay: string
  statusPriceExpired: string
  statusUnderpaid: string
  statusPaid: string
  statusRefunded: string
  statusCancelled: string
  statusSeenOnMonero: string
  statusConfirmingCount(confirmationCount: number, confirmationsFinal: number): string

  // The coin choice (`/i/` and `/l/`).
  payWithGroupLabel: string
  payAmountOnChain(amountText: string, assetSymbol: string, chainName: string): string
  payWithCoinOnChain(assetSymbol: string, chainName: string): string
  sendOnChainOnly(chainName: string): string
  sendOnChainOnlyNoSwitching(chainName: string): string
  back: string
  trustLine: string
  trustSheetTitle: string
  trustSheetDescription: string
  trustMoneroOwnAddress: string
  trustMoneroStraightToReceiver: string
  trustEvmOwnDepositAddress: string
  trustEvmOnlyPassesOn: string
  trustNeverHoldsMoney: string

  // The send screen.
  copyAddress: string
  addressCopied: string
  waitingForWallet: string
  waitingForConfirmation: string
  sendAgain: string
  payWithWallet: string
  openInWallet: string
  priceHeldFor: string
  timeLeftToPay: string
  countdownTextBefore: string
  countdownTextAfter: string
  notSeenAfterTwoMinutes: string
  noWalletOpened(isMoneroInvoice: boolean): string
  sentItTapIvePaid: string
  ivePaid: string
  copyAmountLabel(amountText: string): string
  copyAddressLabel(addressText: string): string
  underpaidArrivedSendMore(receivedText: string, sendText: string, assetSymbol: string, isMoneroInvoice: boolean): string
  onlySendCoinOnChain(assetSymbol: string, chainName: string): string
  paymentQrAlt(amountText: string, assetSymbol: string): string
  addressOnlyQrAlt: string
  waitingForYourPayment: string
  showQrAnyway: string
  qrHoldsOnlyTheAddress: string
  useAddressOnlyQr: string
  showFullQr: string

  // The wallet and refund actions.
  confirmPaymentInWallet: string
  sentPageUpdatesItself: string
  restoredSendOnItsWay(chainName: string): string
  restoredSendNotMined(chainName: string): string
  restoredSendFailed(chainName: string): string
  restoredSendOtherAddress: string
  restoredSendUnknown(chainName: string): string
  restoredSendDidNotPay: string
  checkingLastPayment(chainName: string): string
  couldntCopyUseAddressBar: string
  refundHowToWithoutWallet: string
  copyLink: string
  linkCopied: string
  getMyMoneyBack: string
  connectWalletThatSent: string
  signRefundRequest: string
  walletDidNotPayInvoice(connectedAccount: string, senderList: string): string
  noBrowserWallet: string
  walletSharedNoAccount: string
  browserWalletCantPayOn(chainName: string): string
  walletDoesNotKnowChain(chainName: string, chainId: number): string
  noDepositAddressYet: string
  nothingLeftToPay: string
  walletDeclined: string
  walletNotEnoughFunds: string
  walletUnknownNetwork: string
  walletRequestAlreadyOpen: string
  walletNotLetIn: string
  walletNotConnected: string
  walletCantSendFromPage: string
  walletReportedError: string

  // Confirming, paid, closed and a lapsed price.
  confirmingReceived(amountText: string): string
  finalInAFewSeconds: string
  usuallyFinalWithinMinutes(minuteCount: number): string
  moneroInPool(minuteCount: number): string
  moneroFinalAnyMoment(amountText: string): string
  moneroFinalInMinutes(amountText: string, minuteCount: number): string
  backToShop: string
  sellerAcceptedWhatArrived: string
  viewPayment: string
  refreshPrice: string
  priceLapsedLine(receivedText: string, assetSymbol: string): string
  priceLapsedNextStepRefreshOrGetBack: string
  priceLapsedNextStepRefreshOrAskSeller: string
  priceLapsedNextStepRefresh: string
  underpaidRefundFrom(dateText: string): string
  underpaidRefundCantBeChecked: string
  checkingLastSecondPayment: string
  linkExpiredAskForNew: string
  yourAmountWentBackTo(amountText: string, recipientText: string): string
  yourAmountGoingBackTo(amountText: string, recipientText: string): string
  refundHeldUpSellerFollowsUp: string
  receiverCancelledInvoice: string
  invoiceClosed: string
  moneyOnItsWayBack: string
  refundHeldUpDontSendMore: string
  yourAmountGoingBackDontSendMore(amountText: string, recipientText: string): string
  walletsThatSentIt: string
  extraWentBackTo(amountText: string, recipientText: string): string
  extraGoingBackByItself(amountText: string, recipientText: string): string
  extraHeldUp: string
  askSellerForExtra: string
  extraGoesBackByItself: string
  extraWentBack: string
  extraTooSmall: string
  extraBackOncePricesReadable: string
  strayPaymentLine(receivedText: string, invoiceAssetSymbol: string | null, outcome: StrayPaymentOutcome): string

  // The "I've paid" sheet.
  checkYourPayment: string
  weLookAtChain(chainName: string): string
  checkingChain(chainName: string): string
  transactionHashLabel: string
  transactionIdLabel: string
  clearTransactionHash: string
  clearTransactionId: string
  paste: string
  examplePlaceholder(exampleText: string): string
  transactionHashHelp: string
  transactionIdHelp: string
  checkThisTransaction: string
  done: string
  clipboardNotAllowed: string
  checkFoundCounted(amountText: string): string
  checkFoundConfirming(amountText: string, chainName: string, usualFinalityMinutes: number | null): string
  checkFoundConfirmingMonero(amountText: string, confirmationsFinal: number | null): string
  checkYourPaymentFallback: string
  checkAfterClose(amountText: string): string
  checkWrongCoinRecorded(amountText: string): string
  checkWrongCoinConfirming(amountText: string, invoiceAssetSymbol: string, chainName: string): string
  checkUnlistedToken: string
  checkOtherAddress: string
  checkOtherAddressMonero: string
  checkTransactionFailed(chainName: string): string
  checkTransactionFailedMonero: string
  checkTransactionNotMined(chainName: string): string
  checkTransactionUnknown(chainName: string): string
  checkTransactionUnknownMonero: string
  checkHashMalformed(chainName: string): string
  checkHashMalformedMonero: string
  checkNothingFound(chainName: string): string
  checkNothingFoundMonero: string

  // A quantity link's `/l/`.
  slotsLeft(slotsLeft: number, quantity: number): string
  youStartedPaying: string
  yourInvoiceIsWaiting: string
  goToYourInvoice: string
  startOver: string
  allTakenForNow: string
  allTakenForNowLine: string
  soldOut: string
  allArePaidFor(quantity: number): string
  linkClosed: string
  linkClosedLine: string
  checkAgain: string

  // Refusals whose account-page sentence (describeError) speaks to the maker, so every language, English too,
  // says them to the payer: the per-network cap on unpaid invoices (too_many_open_invoices), the gateway-wide cap on
  // public invoices waiting for payment (public_invoice_capacity_reached), and a seller's Monero wallet that can't
  // take another invoice (monero_wallet_missing, monero_lookahead_exhausted).
  tooManyOpenInvoices: string
  tooManyPeoplePayingNow: string
  linkCantTakePaymentsNow: string
  // What the shared components say on their own (sandbox strip, dialog close, spinner), given to them on payer pages.
  interfaceLabels: InterfaceLabels
  // Null in English only (see PayerErrorSentences).
  errors: PayerErrorSentences | null
}

// Every language but English: its own sentence for each refusal, never English's null.
export type TranslatedPayerStrings = PayerStrings & { errors: PayerErrorSentences }
