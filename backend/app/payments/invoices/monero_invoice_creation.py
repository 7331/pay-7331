"""Where a new XMR invoice is paid (PLAN.md section 14): a fresh subaddress of the account's Monero wallet.

Lock order: the account's invoice-creation advisory lock (``InvoiceRepository.lock_account_invoice_creation``), then
the wallet row (``MoneroWalletService.allocate_invoice_subaddress_minor``); a quantity link's row and a client network's
advisory lock come before both (``create_invoice``). The gap guard counts from the highest minor of the wallet with an
output seen in a block, which only this domain knows (``monero_payments``). The subaddress comes from the sidecar; if
it does not answer, the raised error rolls the minor back with the rest of the transaction.
"""

from app.payments.invoices.new_invoice_rows import InvoicePaymentTarget
from app.payments.invoices.repository import InvoiceRepository
from app.payments.monero_wallets.errors import MoneroWalletChangeConflictError, MoneroWalletMissingError
from app.payments.monero_wallets.service import MoneroWalletService


async def allocate_monero_payment_target(
    account_id: int,
    *,
    chain_key: str,
    confirmations_final: int,
    repository: InvoiceRepository,
    monero_wallet_service: MoneroWalletService,
) -> InvoicePaymentTarget:
    """The next subaddress of the account's active wallet on the chain. ``monero_wallet_missing`` without a wallet,
    ``monero_lookahead_exhausted`` at the gap limit, ``monero_sidecar_unavailable`` when nothing derives it.
    ``confirmations_final`` is the count this invoice's outputs count final at, the receiver's setting or the chain
    default; the invoice keeps it however the setting changes later."""
    await repository.lock_account_invoice_creation(account_id)
    wallet = await monero_wallet_service.find_active_wallet(account_id, chain_key=chain_key)
    if wallet is None:
        raise MoneroWalletMissingError("monero_wallet: set a Monero wallet in settings first")
    last_received_minor = await repository.find_last_received_subaddress_minor(wallet.id)
    allocation = await monero_wallet_service.allocate_invoice_subaddress_minor(
        account_id, chain_key=chain_key, last_received_minor=last_received_minor
    )
    if allocation.monero_wallet_id != wallet.id:
        # The wallet changed between the read and the row lock: the gap was counted on the old one.
        raise MoneroWalletChangeConflictError(
            "monero_wallet: the Monero wallet changed while this invoice was made; try again"
        )
    subaddress = await monero_wallet_service.derive_invoice_subaddress(allocation)
    return InvoicePaymentTarget(
        deposit_address=subaddress,
        destination_address=allocation.primary_address,
        latest_block_number=None,
        monero_wallet_id=allocation.monero_wallet_id,
        subaddress_minor=allocation.subaddress_minor,
        confirmations_final=confirmations_final,
    )
