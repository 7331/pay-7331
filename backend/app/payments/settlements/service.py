"""SettlementService: the request-side half of settlements, on one session (PLAN.md sections 5 and 6).

It records sweep and refund operations (the watcher's ``SettlementRunner`` sends them), lists them for the invoice
resource, and owns refund offers: the exact ``personal_sign`` text, its one-time nonce, and the recovery of the signer
with eth-account (EIP-191 version 0x45, https://eips.ethereum.org/EIPS/eip-191). The owner settles a ``needs_owner``
refund here too, from the CLI (PLAN.md section 6).
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
import secrets
from typing import Self

from eth_abi import decode
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak, to_checksum_address
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.evm_rpc_api.client import EvmRpcClient, EvmRpcEndpoint
from app.infrastructure.evm_rpc_api.errors import EvmRpcUnavailableError
from app.infrastructure.evm_rpc_api.schemas import BlockHeads, MinedTransaction, NativeTransfer, TransactionReceipt
from app.payments.chains.types import AssetConfiguration, EvmChainConfiguration
from app.payments.settlements.enums import OperationKind, OperationStatus
from app.payments.settlements.errors import (
    RefundAttemptMinedError,
    RefundNotNeedingOwnerError,
    RefundOfferNotFoundError,
    RefundOfferUsedError,
    RefundPaymentNotProvenError,
    RefundSignatureInvalidError,
    RefundUnpaidNotProvenError,
)
from app.payments.settlements.log_events import RefundSettledByOwner
from app.payments.settlements.models import OutgoingOperation, RefundOffer
from app.payments.settlements.operator_key_service import OperatorKeyService
from app.payments.settlements.operator_keys import OperatorKeyAddresses
from app.payments.settlements.repository import SettlementRepository
from app.payments.settlements.types import (
    OperationCount,
    OperationValue,
    RefundNeedingOwner,
    RefundOfferDraft,
    RefundOfferValue,
    SettlementOutcome,
    UnresolvedStaticDepositSweep,
)
from app.shared import logging as log
from app.shared.amounts import format_base_units

logger = log.get_logger(__name__)

REFUND_OFFER_LIFETIME = timedelta(minutes=15)
# The refunds the owner may end by hand: an unproven end, and one waiting for float that may never be sendable (a
# blacklisted token recipient, a reverting account; no nonce is used yet, 2026-10-09).
OWNER_SETTLEABLE_REFUND_STATUSES = (OperationStatus.NEEDS_OWNER, OperationStatus.WAITING_FOR_FLOAT)
# A live offer with this much left is handed back instead of a new one, so a caller cannot void the nonce a payer is
# signing; a newer one never deletes it either (2026-10-09).
REFUND_OFFER_REUSE_MINIMUM_REMAINING = timedelta(minutes=5)
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
TRANSFER_TOPIC = "0x" + keccak(text="Transfer(address,address,uint256)").hex()


def operation_value_of(operation: OutgoingOperation) -> OperationValue:
    """An invoice operation's read type; the key operations (no invoice) never become one."""
    if operation.invoice_id is None:
        raise ValueError(f"{operation.kind.value} {operation.id} belongs to no invoice")
    return OperationValue(
        operation_id=operation.id,
        invoice_id=operation.invoice_id,
        kind=operation.kind,
        status=operation.status,
        transaction_hash=operation.transaction_hash,
        recipient_address=operation.recipient_address,
        asset_symbol=operation.asset_symbol,
        amount_base_units=operation.amount_base_units,
        moved_base_units=operation.moved_base_units,
        failure_reason=operation.failure_reason,
        created_at=operation.created_at,
        skipped_token_addresses=tuple(operation.skipped_token_addresses),
    )


def moved_by_refund(operation: OutgoingOperation, *, receipt: TransactionReceipt) -> int:
    """A native refund moved its value when the receipt succeeded; a token refund moved what its Transfer log says."""
    if operation.token_address is None:
        return operation.amount_base_units or 0
    moved = 0
    for log_entry in receipt.logs:
        if log_entry.address != operation.token_address or len(log_entry.topics) != 3:
            continue
        if log_entry.topics[0] != TRANSFER_TOPIC:
            continue
        if to_checksum_address("0x" + log_entry.topics[2][-40:]) == operation.recipient_address:
            (amount,) = decode(["uint256"], bytes.fromhex(log_entry.data.removeprefix("0x")))
            moved += amount
    return moved


def moved_to_recipient(
    operation: OutgoingOperation, *, transaction: NativeTransfer, receipt: TransactionReceipt
) -> int:
    """What a transaction someone else sent moved to the refund's recipient: a direct native transfer's value, or
    the token's Transfer logs to the recipient."""
    if operation.token_address is not None:
        return moved_by_refund(operation, receipt=receipt)
    return transaction.value if transaction.recipient == operation.recipient_address else 0


def refund_offer_value_of(refund_offer: RefundOffer) -> RefundOfferValue:
    return RefundOfferValue(
        invoice_id=refund_offer.invoice_id,
        nonce=refund_offer.nonce,
        sender_address=refund_offer.sender_address,
        asset_symbol=refund_offer.asset_symbol,
        amount_base_units=refund_offer.amount_base_units,
        message=refund_offer.message,
        expires_at=refund_offer.expires_at,
    )


def build_refund_message(
    *,
    public_base_url: str,
    chain: EvmChainConfiguration,
    invoice_id: str,
    deposit_address: str,
    asset: AssetConfiguration,
    amount_base_units: int,
    recipient_address: str,
    nonce: str,
    expires_at: datetime,
) -> str:
    """The text the sender signs (PLAN.md section 6): origin, chain id, deposit, token, amount in base units and as
    a decimal, recipient, nonce and expiry. Signing it proves the sender holds the key of the sending address."""
    token_line = asset.token_address or f"{ZERO_ADDRESS} (native {asset.symbol})"
    return "\n".join(
        [
            "pay-7331 refund request",
            f"Origin: {public_base_url}",
            f"Chain ID: {chain.chain_id}",
            f"Invoice: {invoice_id}",
            f"Deposit address: {deposit_address}",
            f"Token: {token_line}",
            f"Amount: {amount_base_units} base units ({format_base_units(amount_base_units, decimals=asset.decimals)} {asset.symbol})",
            f"Recipient: {recipient_address}",
            f"Nonce: {nonce}",
            f"Expires: {expires_at.isoformat().replace('+00:00', 'Z')}",
        ]
    )


def recover_personal_sign_signer(message: str, *, signature: str) -> str:
    try:
        return to_checksum_address(Account.recover_message(encode_defunct(text=message), signature=signature))
    except Exception as exc:  # eth-account raises several errors for a malformed signature
        raise RefundSignatureInvalidError("the signature could not be read") from exc


class SettlementService:
    def __init__(
        self, *, repository: SettlementRepository, operator_key_addresses: dict[str, OperatorKeyAddresses]
    ) -> None:
        self.repository = repository
        # The operator keys per chain key; only the addresses are needed here (the watcher alone signs).
        self.operator_key_addresses = operator_key_addresses

    @classmethod
    def on_session(cls, session: AsyncSession, *, operator_key_addresses: dict[str, OperatorKeyAddresses]) -> Self:
        return cls(repository=SettlementRepository(session=session), operator_key_addresses=operator_key_addresses)

    async def choose_sweep_key_address(self, chain_key: str) -> str:
        """The sweeper a new sweep goes to (``operator_keys.choose_sweep_key_address``). The zero address when this
        process has no key for the chain or none is usable: the runner then picks one when it signs."""
        addresses = self.operator_key_addresses.get(chain_key)
        if addresses is None:
            return ZERO_ADDRESS
        chosen = await OperatorKeyService(repository=self.repository).choose_sweep_key_address(
            chain_key, addresses=addresses
        )
        return chosen or ZERO_ADDRESS

    async def request_sweep(
        self,
        invoice_id: str,
        *,
        chain_key: str,
        deposit_address: str,
        invoice_salt: bytes,
        destination_address: str,
        skipped_token_addresses: Sequence[str] = (),
    ) -> bool:
        """Record a sweep unless one is still active for the invoice; True when a new one was recorded. A sweep never
        lists its ``skipped_token_addresses``, even when the deposit holds them (``sweep-invoice --skip-token``)."""
        if await self.repository.find_active_operation(invoice_id, kind=OperationKind.SWEEP) is not None:
            return False
        await self.repository.insert_operation(
            OutgoingOperation(
                kind=OperationKind.SWEEP,
                status=OperationStatus.PENDING,
                invoice_id=invoice_id,
                chain_key=chain_key,
                operator_address=await self.choose_sweep_key_address(chain_key),
                deposit_address=deposit_address,
                invoice_salt=invoice_salt,
                destination_address=destination_address,
                skipped_token_addresses=list(skipped_token_addresses),
            )
        )
        return True

    async def request_static_deposit_sweep(
        self,
        static_deposit_address_id: str,
        *,
        chain_key: str,
        deposit_address: str,
        deposit_salt: bytes,
        destination_address: str,
        skipped_token_addresses: Sequence[str] = (),
    ) -> bool:
        """Record a static deposit address's sweep (PLAN.md section 17) unless one is still active for it; True when
        a new one was recorded. It is signed and sent exactly as an invoice's sweep."""
        if await self.repository.find_active_static_deposit_sweep(static_deposit_address_id) is not None:
            return False
        await self.repository.insert_operation(
            OutgoingOperation(
                kind=OperationKind.STATIC_DEPOSIT_SWEEP,
                status=OperationStatus.PENDING,
                static_deposit_address_id=static_deposit_address_id,
                chain_key=chain_key,
                operator_address=await self.choose_sweep_key_address(chain_key),
                deposit_address=deposit_address,
                invoice_salt=deposit_salt,
                destination_address=destination_address,
                skipped_token_addresses=list(skipped_token_addresses),
            )
        )
        return True

    async def list_static_deposit_address_ids_with_active_sweep(
        self, static_deposit_address_ids: list[str]
    ) -> set[str]:
        if not static_deposit_address_ids:
            return set()
        return await self.repository.list_static_deposit_address_ids_with_active_sweep(static_deposit_address_ids)

    def refund_key_address_of(self, chain_key: str) -> str:
        addresses = self.operator_key_addresses.get(chain_key)
        return addresses.refund_key_address if addresses is not None else ZERO_ADDRESS

    async def has_active_sweep(self, invoice_id: str) -> bool:
        return await self.repository.find_active_operation(invoice_id, kind=OperationKind.SWEEP) is not None

    async def has_final_sweep(self, invoice_id: str) -> bool:
        """The gateway's own sweep of this invoice went final (a third party's sweep is not one)."""
        return await self.repository.has_final_operation(invoice_id, kind=OperationKind.SWEEP)

    async def record_refund(
        self,
        invoice_id: str,
        *,
        chain_key: str,
        asset: AssetConfiguration,
        recipient_address: str,
        amount_base_units: int,
    ) -> OperationValue:
        """A pending refund of ``amount_base_units`` to ``recipient_address``. Never to the zero address (a mint's
        sender, which no token takes a transfer to; 2026-10-13): every caller filters it, and this refuses it too."""
        if recipient_address.lower() == ZERO_ADDRESS:
            raise ValueError("a refund cannot go to the zero address")
        operation = await self.repository.insert_operation(
            OutgoingOperation(
                kind=OperationKind.REFUND,
                status=OperationStatus.PENDING,
                invoice_id=invoice_id,
                chain_key=chain_key,
                operator_address=self.refund_key_address_of(chain_key),
                recipient_address=recipient_address,
                asset_symbol=asset.symbol,
                token_address=asset.token_address,
                amount_base_units=amount_base_units,
            )
        )
        return operation_value_of(operation)

    async def list_operations_for_invoices(self, invoice_ids: list[str]) -> list[OperationValue]:
        if not invoice_ids:
            return []
        return [
            operation_value_of(operation)
            for operation in await self.repository.list_operations_for_invoices(invoice_ids)
        ]

    # Reconciliation reads: the operator's `reconcile` and the watcher's operator alerts (2026-10-12)

    async def list_unresolved_operations_created_before(
        self, chain_key: str, *, created_before: datetime
    ) -> list[OperationValue]:
        return [
            operation_value_of(operation)
            for operation in await self.repository.list_unresolved_operations_created_before(
                chain_key, created_before=created_before
            )
        ]

    async def list_unresolved_static_deposit_sweeps_created_before(
        self, chain_key: str, *, created_before: datetime
    ) -> list[UnresolvedStaticDepositSweep]:
        return [
            UnresolvedStaticDepositSweep(
                operation_id=operation.id,
                static_deposit_address_id=operation.static_deposit_address_id,
                status=operation.status,
                created_at=operation.created_at,
            )
            for operation in await self.repository.list_unresolved_static_deposit_sweeps_created_before(
                chain_key, created_before=created_before
            )
            if operation.static_deposit_address_id is not None
        ]

    async def list_invoice_ids_with_active_sweep(self, invoice_ids: list[str]) -> set[str]:
        return await self.repository.list_invoice_ids_with_active_sweep(invoice_ids) if invoice_ids else set()

    async def count_operations_by_invoice(
        self, chain_key: str, *, statuses: list[OperationStatus]
    ) -> list[OperationCount]:
        return [
            OperationCount(invoice_id=invoice_id, kind=kind, status=status, count=count)
            for invoice_id, kind, status, count in await self.repository.count_operations_by_invoice_kind_and_status(
                chain_key, statuses=statuses
            )
        ]

    async def create_refund_offers(
        self,
        invoice_id: str,
        *,
        public_base_url: str,
        chain: EvmChainConfiguration,
        deposit_address: str,
        asset: AssetConfiguration,
        drafts: list[RefundOfferDraft],
    ) -> list[RefundOfferValue]:
        """The sender's live offer while it has time left and the amount still holds, else a new one; a live offer is
        never deleted, so another caller cannot void the one a payer is signing (PLAN.md section 6). Two live offers
        cannot pay twice: the refund rechecks the sender's refundable amount under the invoice lock."""
        now = datetime.now(UTC)
        expires_at = (now + REFUND_OFFER_LIFETIME).replace(microsecond=0)
        offers: list[RefundOfferValue] = []
        for draft in drafts:
            await self.repository.delete_expired_unconsumed_offers(
                invoice_id, sender_address=draft.sender_address, now=now
            )
            live_offer = await self.repository.find_newest_live_offer(
                invoice_id, sender_address=draft.sender_address, now=now
            )
            if (
                live_offer is not None
                and live_offer.amount_base_units == draft.amount_base_units
                and live_offer.asset_symbol == asset.symbol
                and live_offer.expires_at - now >= REFUND_OFFER_REUSE_MINIMUM_REMAINING
            ):
                offers.append(refund_offer_value_of(live_offer))
                continue
            nonce = secrets.token_hex(16)
            refund_offer = await self.repository.insert_refund_offer(
                RefundOffer(
                    invoice_id=invoice_id,
                    nonce=nonce,
                    sender_address=draft.sender_address,
                    asset_symbol=asset.symbol,
                    amount_base_units=draft.amount_base_units,
                    message=build_refund_message(
                        public_base_url=public_base_url,
                        chain=chain,
                        invoice_id=invoice_id,
                        deposit_address=deposit_address,
                        asset=asset,
                        amount_base_units=draft.amount_base_units,
                        recipient_address=draft.sender_address,
                        nonce=nonce,
                        expires_at=expires_at,
                    ),
                    expires_at=expires_at,
                )
            )
            offers.append(refund_offer_value_of(refund_offer))
        return offers

    async def consume_signed_refund_offer(self, invoice_id: str, *, nonce: str, signature: str) -> RefundOfferValue:
        """Check the signature against the offer's text and sender, then consume the nonce on this session's
        transaction. The caller rechecks the refundable amount and records the refund in the same transaction."""
        refund_offer = await self.repository.find_refund_offer(nonce, invoice_id=invoice_id)
        if refund_offer is None:
            raise RefundOfferNotFoundError("no refund offer with that nonce")
        signer_address = recover_personal_sign_signer(refund_offer.message, signature=signature)
        if signer_address != refund_offer.sender_address:
            raise RefundSignatureInvalidError("the signature is not from the address that sent the payment")
        consumed = await self.repository.consume_refund_offer(
            nonce, invoice_id=invoice_id, consumed_at=datetime.now(UTC)
        )
        if consumed is None:
            raise RefundOfferUsedError("this refund offer was already used or has expired; ask for a new one")
        return refund_offer_value_of(consumed)

    # The owner's settling of an unproven refund end (PLAN.md section 6)

    async def list_refunds_needing_owner(self) -> list[RefundNeedingOwner]:
        refunds: list[RefundNeedingOwner] = []
        operations = [
            operation
            for status in OWNER_SETTLEABLE_REFUND_STATUSES
            for operation in await self.repository.list_operations_with_status(status, kind=OperationKind.REFUND)
        ]
        for operation in operations:
            attempts = await self.repository.list_attempts(operation.id)
            refunds.append(
                RefundNeedingOwner(
                    operation_id=operation.id,
                    # A refund always has its invoice (``OperationKind.has_invoice``).
                    invoice_id=operation.invoice_id or "",
                    status=operation.status.value,
                    chain_key=operation.chain_key,
                    operator_address=operation.operator_address,
                    nonce=operation.nonce,
                    recipient_address=operation.recipient_address or "",
                    asset_symbol=operation.asset_symbol or "",
                    amount_base_units=operation.amount_base_units or 0,
                    attempt_transaction_hashes=[attempt.transaction_hash for attempt in attempts],
                    created_at=operation.created_at,
                )
            )
        return refunds

    async def lock_refund_needing_owner(self, operation_id: int) -> OutgoingOperation:
        operation = await self.repository.find_operation(operation_id, lock=True)
        if (
            operation is None
            or operation.kind != OperationKind.REFUND
            or operation.status not in OWNER_SETTLEABLE_REFUND_STATUSES
        ):
            raise RefundNotNeedingOwnerError(
                f"operation {operation_id} is not a refund waiting for the owner or for float"
            )
        return operation

    async def mark_refund_paid_by_owner(
        self, operation_id: int, *, transaction_hash: str, rpc_clients: dict[str, EvmRpcClient]
    ) -> SettlementOutcome:
        """``final`` with the transaction the owner found, once the chain shows it succeeded, is finalized and moved
        exactly the reserved amount to the recorded recipient. Any sender will do (the owner may have paid it from
        another wallet); one transaction settles one operation."""
        operation = await self.lock_refund_needing_owner(operation_id)
        settled_elsewhere = await self.repository.find_operation_by_transaction_hash(transaction_hash)
        if settled_elsewhere is not None and settled_elsewhere.id != operation.id:
            raise RefundPaymentNotProvenError("that transaction already settles another operation")
        rpc_client = rpc_clients[operation.chain_key]

        async def read_payment(
            endpoint: EvmRpcEndpoint,
        ) -> tuple[TransactionReceipt | None, NativeTransfer | None, BlockHeads]:
            (receipt,) = await endpoint.read_transaction_receipts([transaction_hash])
            return receipt, await endpoint.read_mined_transaction(transaction_hash), await endpoint.read_block_heads()

        receipt, transaction, block_heads = await rpc_client.call_with_failover(read_payment)
        if receipt is None or transaction is None:
            raise RefundPaymentNotProvenError("the chain does not show that transaction as mined")
        if not receipt.succeeded:
            raise RefundPaymentNotProvenError("that transaction reverted")
        if receipt.block_number > block_heads.finalized.number:
            raise RefundPaymentNotProvenError("that transaction is not finalized yet; try again in a minute")
        moved = moved_to_recipient(operation, transaction=transaction, receipt=receipt)
        if moved != operation.amount_base_units:
            raise RefundPaymentNotProvenError(
                f"that transaction moved {moved} base units to {operation.recipient_address}, "
                f"not the {operation.amount_base_units} reserved"
            )
        operation.status = OperationStatus.FINAL
        operation.transaction_hash = receipt.transaction_hash
        operation.block_number = receipt.block_number
        operation.block_hash = receipt.block_hash
        operation.moved_base_units = moved
        await self.repository.session.flush()
        log.emit(
            logger,
            event=RefundSettledByOwner(
                operation_id=operation.id,
                status=operation.status.value,
                transaction_hash=receipt.transaction_hash,
                nonce_taken_by_transaction_hash=None,
            ),
        )
        return SettlementOutcome(operation=operation_value_of(operation))

    async def mark_refund_unpaid_by_owner(
        self,
        operation_id: int,
        *,
        nonce_taking_transaction_hash: str | None,
        rpc_clients: dict[str, EvmRpcClient],
    ) -> SettlementOutcome:
        """``failed``, which frees the reserved amount so the sender can ask again.

        A refund that signed an attempt needs positive proof that its nonce went to another transaction (see
        ``prove_nonce_taken_by_other_transaction``): a pruned endpoint shows no receipt for an old transaction, so "no
        endpoint shows a receipt" alone could free an amount that was paid (2026-10-09). A ``waiting_for_float``
        refund that never signed one used no nonce, so nothing can mine and it is freed without proof; the row lock
        keeps the watcher from signing one meanwhile (it rechecks the status under the same lock). The older checks
        stay as extra guards: refused while the nonce is unused or any endpoint shows a receipt for an attempt."""
        operation = await self.lock_refund_needing_owner(operation_id)
        rpc_client = rpc_clients[operation.chain_key]
        attempt_hashes = [attempt.transaction_hash for attempt in await self.repository.list_attempts(operation.id)]
        if operation.nonce is None and not attempt_hashes:
            if nonce_taking_transaction_hash is not None:
                raise RefundUnpaidNotProvenError(
                    "this refund never signed an attempt, so it used no nonce; run mark-unpaid without --nonce-taken-by"
                )
        elif nonce_taking_transaction_hash is None:
            raise RefundUnpaidNotProvenError(
                f"this refund signed attempts at nonce {operation.nonce}; pass --nonce-taken-by with the finalized "
                "operator transaction that used that nonce"
            )
        else:
            await self.prove_nonce_taken_by_other_transaction(
                operation,
                transaction_hash=nonce_taking_transaction_hash,
                attempt_hashes=attempt_hashes,
                rpc_client=rpc_client,
            )
        if operation.nonce is not None:
            latest_nonce = await rpc_client.call_with_failover(
                lambda endpoint: endpoint.read_transaction_count(operation.operator_address, block_tag="latest")
            )
            if latest_nonce <= operation.nonce:
                raise RefundAttemptMinedError(f"nonce {operation.nonce} is not used yet; an attempt may still mine")
        # An endpoint that does not answer is no proof the refund did not pay, so it is skipped and two must answer
        # (one on a one-endpoint chain), the runner's rule; a dead endpoint no longer blocks it (2026-10-09, live).
        endpoints_without_receipt = 0
        for endpoint in rpc_client.endpoints if attempt_hashes else []:
            try:
                receipts = await endpoint.read_transaction_receipts(attempt_hashes)
            except EvmRpcUnavailableError:
                continue
            for receipt in receipts:
                if receipt is not None:
                    raise RefundAttemptMinedError(
                        f"attempt {receipt.transaction_hash} mined in block {receipt.block_number}; "
                        "settle it with mark-paid"
                    )
            endpoints_without_receipt += 1
        if attempt_hashes and endpoints_without_receipt < min(2, len(rpc_client.endpoints)):
            raise RefundUnpaidNotProvenError(
                f"only {endpoints_without_receipt} RPC endpoints answered the receipt check; try again in a minute"
            )
        operation.status = OperationStatus.FAILED
        operation.failure_reason = "owner_marked_unpaid"
        await self.repository.session.flush()
        log.emit(
            logger,
            event=RefundSettledByOwner(
                operation_id=operation.id,
                status=operation.status.value,
                transaction_hash=None,
                nonce_taken_by_transaction_hash=nonce_taking_transaction_hash,
            ),
        )
        return SettlementOutcome(operation=operation_value_of(operation))

    async def prove_nonce_taken_by_other_transaction(
        self,
        operation: OutgoingOperation,
        *,
        transaction_hash: str,
        attempt_hashes: list[str],
        rpc_client: EvmRpcClient,
    ) -> None:
        """Refuse unless the chain shows a finalized transaction from the operator at the refund's nonce that is none
        of its attempts. Every attempt shares that nonce, so once another transaction holds it at a finalized block,
        no attempt can ever mine (2026-10-09). Reverted is fine (the nonce is used either way); moving anything to the
        recipient is not, since that may be the owner's own payment of this refund (mark-paid settles that)."""
        if operation.nonce is None:
            raise RefundUnpaidNotProvenError("this refund has signed attempts but no recorded nonce; settle it by hand")
        if transaction_hash in attempt_hashes:
            raise RefundUnpaidNotProvenError(
                "that is one of this refund's own attempts; name the other transaction that took its nonce"
            )

        async def read_nonce_taking_transaction(
            endpoint: EvmRpcEndpoint,
        ) -> tuple[MinedTransaction | None, TransactionReceipt | None, BlockHeads]:
            (receipt,) = await endpoint.read_transaction_receipts([transaction_hash])
            return await endpoint.read_mined_transaction(transaction_hash), receipt, await endpoint.read_block_heads()

        transaction, receipt, block_heads = await rpc_client.call_with_failover(read_nonce_taking_transaction)
        if transaction is None or receipt is None:
            raise RefundUnpaidNotProvenError(
                "the chain does not show that transaction as mined (an endpoint may have pruned an old one)"
            )
        if transaction.transaction_hash.lower() in attempt_hashes:
            raise RefundUnpaidNotProvenError("that is one of this refund's own attempts")
        if transaction.sender != operation.operator_address:
            raise RefundUnpaidNotProvenError(
                f"that transaction was sent by {transaction.sender}, not the operator {operation.operator_address}"
            )
        if transaction.nonce != operation.nonce:
            raise RefundUnpaidNotProvenError(
                f"that transaction has nonce {transaction.nonce}, not the refund's nonce {operation.nonce}"
            )
        if transaction.block_number > block_heads.finalized.number:
            raise RefundUnpaidNotProvenError("that transaction is not finalized yet; try again in a minute")
        moved = moved_to_recipient(operation, transaction=transaction, receipt=receipt)
        if moved > 0:
            raise RefundUnpaidNotProvenError(
                f"that transaction moved {moved} base units to {operation.recipient_address}; if it paid this refund, "
                "settle it with mark-paid"
            )
