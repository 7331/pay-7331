"""Queries for outgoing_operations, outgoing_attempts, operator_nonces, operator_key_states and refund_offers; no
decisions."""

from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.settlements.enums import OperationKind, OperationStatus
from app.payments.settlements.models import (
    OperatorKeyState,
    OperatorNonce,
    OutgoingAttempt,
    OutgoingOperation,
    RefundOffer,
)
from app.payments.settlements.types import OPERATOR_KEY_FROZEN_FAILURE_REASON

ACTIVE_STATUSES = [status for status in OperationStatus if status.is_active]
UNRESOLVED_SENDING_STATUSES = [OperationStatus.PENDING, OperationStatus.BROADCAST, OperationStatus.MINED]
KEY_OPERATION_KINDS = [kind for kind in OperationKind if kind.is_key_operation]


class SettlementRepository:
    def __init__(self, *, session: AsyncSession) -> None:
        self.session = session

    async def insert_operation(self, operation: OutgoingOperation) -> OutgoingOperation:
        self.session.add(operation)
        await self.session.flush()
        return operation

    async def find_active_operation(self, invoice_id: str, *, kind: OperationKind) -> OutgoingOperation | None:
        return await self.session.scalar(
            select(OutgoingOperation)
            .where(
                OutgoingOperation.invoice_id == invoice_id,
                OutgoingOperation.kind == kind,
                OutgoingOperation.status.in_(ACTIVE_STATUSES),
            )
            .limit(1)
        )

    async def find_active_static_deposit_sweep(self, static_deposit_address_id: str) -> OutgoingOperation | None:
        return await self.session.scalar(
            select(OutgoingOperation)
            .where(
                OutgoingOperation.static_deposit_address_id == static_deposit_address_id,
                OutgoingOperation.kind == OperationKind.STATIC_DEPOSIT_SWEEP,
                OutgoingOperation.status.in_(ACTIVE_STATUSES),
            )
            .limit(1)
        )

    async def list_static_deposit_address_ids_with_active_sweep(
        self, static_deposit_address_ids: list[str]
    ) -> set[str]:
        return {
            static_deposit_address_id
            for static_deposit_address_id in await self.session.scalars(
                select(OutgoingOperation.static_deposit_address_id).where(
                    OutgoingOperation.static_deposit_address_id.in_(static_deposit_address_ids),
                    OutgoingOperation.kind == OperationKind.STATIC_DEPOSIT_SWEEP,
                    OutgoingOperation.status.in_(ACTIVE_STATUSES),
                )
            )
            if static_deposit_address_id is not None
        }

    async def has_final_operation(self, invoice_id: str, *, kind: OperationKind) -> bool:
        operation_id = await self.session.scalar(
            select(OutgoingOperation.id)
            .where(
                OutgoingOperation.invoice_id == invoice_id,
                OutgoingOperation.kind == kind,
                OutgoingOperation.status == OperationStatus.FINAL,
            )
            .limit(1)
        )
        return operation_id is not None

    async def list_operations_for_invoices(self, invoice_ids: list[str]) -> list[OutgoingOperation]:
        return list(
            await self.session.scalars(
                select(OutgoingOperation)
                .where(OutgoingOperation.invoice_id.in_(invoice_ids))
                .order_by(OutgoingOperation.id)
            )
        )

    async def list_active_operations(self, chain_key: str) -> list[OutgoingOperation]:
        return list(
            await self.session.scalars(
                select(OutgoingOperation)
                .where(OutgoingOperation.chain_key == chain_key, OutgoingOperation.status.in_(ACTIVE_STATUSES))
                .order_by(OutgoingOperation.id)
            )
        )

    async def list_operations_with_status(
        self, status: OperationStatus, *, kind: OperationKind
    ) -> list[OutgoingOperation]:
        return list(
            await self.session.scalars(
                select(OutgoingOperation)
                .where(OutgoingOperation.kind == kind, OutgoingOperation.status == status)
                .order_by(OutgoingOperation.id)
            )
        )

    # Reconciliation reads (2026-10-12)

    async def list_unresolved_operations_created_before(
        self, chain_key: str, *, created_before: datetime
    ) -> list[OutgoingOperation]:
        """Invoice operations still ``pending``, ``broadcast`` or ``mined`` made before ``created_before`` (a refund
        waiting for float is not one: it waits on purpose). Key operations have their own check."""
        return list(
            await self.session.scalars(
                select(OutgoingOperation)
                .where(
                    OutgoingOperation.chain_key == chain_key,
                    OutgoingOperation.invoice_id.is_not(None),
                    OutgoingOperation.status.in_(UNRESOLVED_SENDING_STATUSES),
                    OutgoingOperation.created_at < created_before,
                )
                .order_by(OutgoingOperation.id)
            )
        )

    async def list_unresolved_static_deposit_sweeps_created_before(
        self, chain_key: str, *, created_before: datetime
    ) -> list[OutgoingOperation]:
        """Static deposit sweeps (no invoice, not a key operation) still sending, made before ``created_before``."""
        return list(
            await self.session.scalars(
                select(OutgoingOperation)
                .where(
                    OutgoingOperation.chain_key == chain_key,
                    OutgoingOperation.kind == OperationKind.STATIC_DEPOSIT_SWEEP,
                    OutgoingOperation.status.in_(UNRESOLVED_SENDING_STATUSES),
                    OutgoingOperation.created_at < created_before,
                )
                .order_by(OutgoingOperation.id)
            )
        )

    async def list_invoice_ids_with_active_sweep(self, invoice_ids: list[str]) -> set[str]:
        return {
            invoice_id
            for invoice_id in await self.session.scalars(
                select(OutgoingOperation.invoice_id).where(
                    OutgoingOperation.invoice_id.in_(invoice_ids),
                    OutgoingOperation.kind == OperationKind.SWEEP,
                    OutgoingOperation.status.in_(ACTIVE_STATUSES),
                )
            )
            if invoice_id is not None
        }

    async def count_operations_by_invoice_kind_and_status(
        self, chain_key: str, *, statuses: list[OperationStatus]
    ) -> list[tuple[str, OperationKind, OperationStatus, int]]:
        """A frozen key's sweep, ended for another key, is left out: it blocks nothing and records no event."""
        rows = await self.session.execute(
            select(OutgoingOperation.invoice_id, OutgoingOperation.kind, OutgoingOperation.status, func.count())
            .where(
                OutgoingOperation.chain_key == chain_key,
                OutgoingOperation.invoice_id.is_not(None),
                OutgoingOperation.status.in_(statuses),
                OutgoingOperation.failure_reason.is_distinct_from(OPERATOR_KEY_FROZEN_FAILURE_REASON),
            )
            .group_by(OutgoingOperation.invoice_id, OutgoingOperation.kind, OutgoingOperation.status)
        )
        return [(invoice_id or "", kind, status, count) for invoice_id, kind, status, count in rows]

    async def find_operation_by_transaction_hash(self, transaction_hash: str) -> OutgoingOperation | None:
        return await self.session.scalar(
            select(OutgoingOperation).where(OutgoingOperation.transaction_hash == transaction_hash).limit(1)
        )

    async def get_operation(self, operation_id: int, *, lock: bool = False) -> OutgoingOperation:
        statement = select(OutgoingOperation).where(OutgoingOperation.id == operation_id)
        if lock:
            statement = statement.with_for_update()
        return (await self.session.scalars(statement)).one()

    async def find_operation(self, operation_id: int, *, lock: bool = False) -> OutgoingOperation | None:
        statement = select(OutgoingOperation).where(OutgoingOperation.id == operation_id)
        if lock:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)

    async def sum_in_flight_refunds(self, chain_key: str, *, asset_symbol: str, excluding_operation_id: int) -> int:
        """Refunds already signed but not mined: the float at ``latest`` does not show them yet."""
        amounts = await self.session.scalars(
            select(OutgoingOperation.amount_base_units).where(
                OutgoingOperation.chain_key == chain_key,
                OutgoingOperation.kind == OperationKind.REFUND,
                OutgoingOperation.asset_symbol == asset_symbol,
                OutgoingOperation.status == OperationStatus.BROADCAST,
                OutgoingOperation.id != excluding_operation_id,
            )
        )
        return sum(amount or 0 for amount in amounts)

    async def insert_attempt(self, attempt: OutgoingAttempt) -> OutgoingAttempt:
        self.session.add(attempt)
        await self.session.flush()
        return attempt

    async def list_attempts(self, operation_id: int) -> list[OutgoingAttempt]:
        return list(
            await self.session.scalars(
                select(OutgoingAttempt).where(OutgoingAttempt.operation_id == operation_id).order_by(OutgoingAttempt.id)
            )
        )

    async def list_attempts_of_operations(self, operation_ids: list[int]) -> list[OutgoingAttempt]:
        return list(
            await self.session.scalars(
                select(OutgoingAttempt)
                .where(OutgoingAttempt.operation_id.in_(operation_ids))
                .order_by(OutgoingAttempt.id)
            )
        )

    async def lock_operator_nonce(self, chain_key: str, *, operator_address: str) -> OperatorNonce | None:
        return await self.session.scalar(
            select(OperatorNonce)
            .where(OperatorNonce.chain_key == chain_key, OperatorNonce.operator_address == operator_address)
            .with_for_update()
        )

    async def insert_operator_nonce(self, nonce_row: OperatorNonce) -> OperatorNonce:
        self.session.add(nonce_row)
        await self.session.flush()
        return nonce_row

    async def insert_operator_nonce_if_missing(self, chain_key: str, *, operator_address: str, next_nonce: int) -> None:
        await self.session.execute(
            insert(OperatorNonce)
            .values(chain_key=chain_key, operator_address=operator_address, next_nonce=next_nonce)
            .on_conflict_do_nothing(index_elements=["chain_key", "operator_address"])
        )

    async def raise_operator_next_nonce(
        self, chain_key: str, *, operator_address: str, minimum_next_nonce: int
    ) -> None:
        """``next_nonce = GREATEST(next_nonce, minimum)``: a row older than its key's state row (main's allocator
        absorbed by-hand sends at ``pending``) is adopted at the chain's count, never moved back."""
        await self.session.execute(
            update(OperatorNonce)
            .where(OperatorNonce.chain_key == chain_key, OperatorNonce.operator_address == operator_address)
            .values(next_nonce=func.greatest(OperatorNonce.next_nonce, minimum_next_nonce))
        )

    async def read_next_nonces(self, chain_key: str, *, operator_addresses: list[str]) -> dict[str, int]:
        rows = await self.session.execute(
            select(OperatorNonce.operator_address, OperatorNonce.next_nonce).where(
                OperatorNonce.chain_key == chain_key, OperatorNonce.operator_address.in_(operator_addresses)
            )
        )
        return dict(rows.tuples().all())

    async def list_active_operation_nonces(self, chain_key: str, *, operator_address: str) -> set[int]:
        """The nonces the key's active operations hold: the runner's, never abandoned."""
        nonces = await self.session.scalars(
            select(OutgoingOperation.nonce).where(
                OutgoingOperation.chain_key == chain_key,
                OutgoingOperation.operator_address == operator_address,
                OutgoingOperation.status.in_(ACTIVE_STATUSES),
            )
        )
        return {nonce for nonce in nonces if nonce is not None}

    async def read_highest_attempt_fees_at_nonce(
        self, chain_key: str, *, operator_address: str, nonce: int
    ) -> tuple[int, int] | None:
        """(max fee, tip), each the highest of every stored attempt of the key at ``nonce``; None without one."""
        row = (
            await self.session.execute(
                select(func.max(OutgoingAttempt.max_fee_per_gas), func.max(OutgoingAttempt.max_priority_fee_per_gas))
                .join(OutgoingOperation, OutgoingOperation.id == OutgoingAttempt.operation_id)
                .where(
                    OutgoingOperation.chain_key == chain_key,
                    OutgoingOperation.operator_address == operator_address,
                    OutgoingOperation.nonce == nonce,
                )
            )
        ).one()
        if row[0] is None or row[1] is None:
            return None
        return int(row[0]), int(row[1])

    # Operator keys (2026-10-13)

    async def list_operator_key_states(self, chain_key: str | None = None) -> list[OperatorKeyState]:
        statement = select(OperatorKeyState).order_by(OperatorKeyState.chain_key, OperatorKeyState.id)
        if chain_key is not None:
            statement = statement.where(OperatorKeyState.chain_key == chain_key)
        return list(await self.session.scalars(statement))

    async def find_operator_key_state(
        self, chain_key: str, *, operator_address: str, lock: bool = False
    ) -> OperatorKeyState | None:
        statement = select(OperatorKeyState).where(
            OperatorKeyState.chain_key == chain_key, OperatorKeyState.operator_address == operator_address
        )
        if lock:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)

    async def insert_operator_key_state_if_missing(self, key_state: OperatorKeyState) -> bool:
        """True when this call inserted the row (``RETURNING id`` is empty on a conflict)."""
        inserted_id = await self.session.scalar(
            insert(OperatorKeyState)
            .values(
                chain_key=key_state.chain_key,
                operator_address=key_state.operator_address,
                role=key_state.role,
                status=key_state.status,
                reason=key_state.reason,
                changed_at=key_state.changed_at,
            )
            .on_conflict_do_nothing(index_elements=["chain_key", "operator_address"])
            .returning(OperatorKeyState.id)
        )
        return inserted_id is not None

    async def count_active_operations_by_operator(self, chain_key: str) -> dict[str, int]:
        rows = await self.session.execute(
            select(OutgoingOperation.operator_address, func.count())
            .where(OutgoingOperation.chain_key == chain_key, OutgoingOperation.status.in_(ACTIVE_STATUSES))
            .group_by(OutgoingOperation.operator_address)
        )
        return dict(rows.tuples().all())

    async def list_key_operations(
        self,
        chain_key: str,
        *,
        statuses: list[OperationStatus],
        created_before: datetime | None = None,
        updated_since: datetime | None = None,
    ) -> list[OutgoingOperation]:
        """Top-ups, treasury returns and Safe creations (no invoice) in ``statuses``."""
        statement = (
            select(OutgoingOperation)
            .where(
                OutgoingOperation.chain_key == chain_key,
                OutgoingOperation.kind.in_(KEY_OPERATION_KINDS),
                OutgoingOperation.status.in_(statuses),
            )
            .order_by(OutgoingOperation.id)
        )
        if created_before is not None:
            statement = statement.where(OutgoingOperation.created_at < created_before)
        if updated_since is not None:
            statement = statement.where(OutgoingOperation.updated_at >= updated_since)
        return list(await self.session.scalars(statement))

    async def list_final_native_treasury_return_addresses(self, chain_key: str) -> set[str]:
        """Keys whose native treasury return went final: each sends its POL back once (what is left is dust)."""
        return set(
            await self.session.scalars(
                select(OutgoingOperation.operator_address).where(
                    OutgoingOperation.chain_key == chain_key,
                    OutgoingOperation.kind == OperationKind.TREASURY_RETURN,
                    OutgoingOperation.token_address.is_(None),
                    OutgoingOperation.status == OperationStatus.FINAL,
                )
            )
        )

    async def delete_expired_unconsumed_offers(self, invoice_id: str, *, sender_address: str, now: datetime) -> None:
        await self.session.execute(
            delete(RefundOffer).where(
                RefundOffer.invoice_id == invoice_id,
                RefundOffer.sender_address == sender_address,
                RefundOffer.consumed_at.is_(None),
                RefundOffer.expires_at <= now,
            )
        )

    async def find_newest_live_offer(
        self, invoice_id: str, *, sender_address: str, now: datetime
    ) -> RefundOffer | None:
        return await self.session.scalar(
            select(RefundOffer)
            .where(
                RefundOffer.invoice_id == invoice_id,
                RefundOffer.sender_address == sender_address,
                RefundOffer.consumed_at.is_(None),
                RefundOffer.expires_at > now,
            )
            .order_by(RefundOffer.expires_at.desc())
            .limit(1)
        )

    async def insert_refund_offer(self, refund_offer: RefundOffer) -> RefundOffer:
        self.session.add(refund_offer)
        await self.session.flush()
        return refund_offer

    async def consume_refund_offer(self, nonce: str, *, invoice_id: str, consumed_at: datetime) -> RefundOffer | None:
        """``UPDATE ... WHERE consumed_at IS NULL``: of two requests with one nonce, only one gets the row."""
        result = await self.session.scalars(
            update(RefundOffer)
            .where(
                RefundOffer.nonce == nonce,
                RefundOffer.invoice_id == invoice_id,
                RefundOffer.consumed_at.is_(None),
                RefundOffer.expires_at > consumed_at,
            )
            .values(consumed_at=consumed_at)
            .returning(RefundOffer)
        )
        return result.one_or_none()

    async def find_refund_offer(self, nonce: str, *, invoice_id: str) -> RefundOffer | None:
        return await self.session.scalar(
            select(RefundOffer).where(RefundOffer.nonce == nonce, RefundOffer.invoice_id == invoice_id)
        )
