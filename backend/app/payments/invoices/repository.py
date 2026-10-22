"""Queries for invoices, invoice_payments, invoice_stray_payments, deposit_sweep_events and monero_payments; no
decisions."""

from datetime import datetime

from sqlalchemy import ColumnElement, delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.deposit_factory_api.schemas import DepositSweptEvent
from app.payments.invoices.enums import InvoiceStatus, MoneroCrossCheckState
from app.payments.invoices.models import (
    DepositSweepEvent,
    Invoice,
    InvoicePayment,
    InvoiceStrayPayment,
    MoneroPayment,
)
from app.payments.invoices.types import (
    AttributedPayment,
    AttributedStrayPayment,
    DepositCreationPosition,
    DepositCreationRange,
)

STRAY_PAYMENT_CONFLICT_COLUMNS = [
    InvoiceStrayPayment.invoice_id,
    InvoiceStrayPayment.asset_symbol,
    InvoiceStrayPayment.transaction_hash,
    InvoiceStrayPayment.log_index,
]


def stray_payment_row_of(
    stray_payment: AttributedStrayPayment, *, senders_with_code: set[str], final: bool
) -> dict[str, object]:
    return {
        "invoice_id": stray_payment.invoice_id,
        "asset_symbol": stray_payment.asset_symbol,
        "transaction_hash": stray_payment.transaction_hash,
        "log_index": stray_payment.log_index,
        "transaction_index": stray_payment.transaction_index,
        "sender": stray_payment.sender,
        "sender_has_code": stray_payment.sender is not None and stray_payment.sender in senders_with_code,
        "amount_base_units": stray_payment.amount_base_units,
        "block_number": stray_payment.block_number,
        "block_hash": stray_payment.block_hash,
        "block_timestamp": stray_payment.block_timestamp,
        "final": final,
    }


def deposit_creation_range_conditions(
    chain_key: str, *, creation_range: DepositCreationRange
) -> list[ColumnElement[bool]]:
    # An invoice still waiting for its payer's pick of a chain has no deposit to read (PLAN.md section 15).
    conditions: list[ColumnElement[bool]] = [Invoice.chain_key == chain_key, Invoice.deposit_address.is_not(None)]
    if creation_range.created_at_or_after is not None:
        conditions.append(Invoice.created_at >= creation_range.created_at_or_after)
    if creation_range.created_before is not None:
        conditions.append(Invoice.created_at < creation_range.created_before)
    if creation_range.closed is True:
        conditions.append(Invoice.closed_at.is_not(None))
    elif creation_range.closed is False:
        conditions.append(Invoice.closed_at.is_(None))
    return conditions


class InvoiceRepository:
    def __init__(self, *, session: AsyncSession) -> None:
        self.session = session

    async def insert_invoice(self, invoice: Invoice) -> Invoice:
        self.session.add(invoice)
        await self.session.flush()
        return invoice

    async def find_invoice(self, invoice_id: str, *, lock: bool = False) -> Invoice | None:
        statement = select(Invoice).where(Invoice.id == invoice_id)
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return await self.session.scalar(statement)

    async def find_account_invoice_by_order_id(self, order_id: str, *, account_id: int) -> Invoice | None:
        """The order's current invoice: one a refresh replaced keeps its order id but is not it (over
        ``ux_invoices_account_id_order_id_current``)."""
        return await self.session.scalar(
            select(Invoice).where(
                Invoice.account_id == account_id,
                Invoice.order_id == order_id,
                Invoice.refreshed_into_invoice_id.is_(None),
            )
        )

    async def invoice_has_money_rows(self, invoice_id: str) -> bool:
        """Whether any payment, stray payment or Monero payment row names the invoice, final or not."""
        for money_row_invoice_id in (
            InvoicePayment.invoice_id,
            InvoiceStrayPayment.invoice_id,
            MoneroPayment.invoice_id,
        ):
            found = await self.session.scalar(
                select(money_row_invoice_id).where(money_row_invoice_id == invoice_id).limit(1)
            )
            if found is not None:
                return True
        return False

    async def find_last_received_subaddress_minor(self, monero_wallet_id: int) -> int:
        """The highest subaddress minor of the wallet's invoices with an output in a block that a second operator
        confirmed (0 when none): the base the receiver's wallet scans its lookahead from (PLAN.md section 14). A pool
        row, a row awaiting its check or a disagreeing one may never reach the receiver's wallet, so it does not move
        the base. Over ``ux_invoices_monero_wallet_id_subaddress_minor`` and ``ix_monero_payments_invoice_id``."""
        last_received_minor = await self.session.scalar(
            select(func.max(Invoice.subaddress_minor))
            .join(MoneroPayment, MoneroPayment.invoice_id == Invoice.id)
            .where(
                Invoice.monero_wallet_id == monero_wallet_id,
                MoneroPayment.block_height.is_not(None),
                MoneroPayment.cross_check_state == MoneroCrossCheckState.CONFIRMED,
            )
        )
        return last_received_minor or 0

    async def exists_deposit_address(self, address: str) -> bool:
        return (await self.session.scalar(select(func.count()).where(Invoice.deposit_address == address)) or 0) > 0

    async def lock_account_invoice_creation(self, account_id: int) -> None:
        """Serialize one account's API-token invoice creations until the transaction ends: the per-account cap holds
        in a burst, and a repeated ``order_id`` finds the first invoice instead of hitting the unique constraint."""
        await self.session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(f"account_invoice_creation:{account_id}", 0)))
        )

    async def count_payment_link_invoices(self, payment_link_ids: list[str]) -> dict[str, tuple[int, int]]:
        """(slots held, paid) per quantity link. A slot is held by every invoice but one that ended with nothing paid:
        `expired` or `cancelled` without a late payment."""
        if not payment_link_ids:
            return {}
        releases_slot = Invoice.status.in_([InvoiceStatus.EXPIRED, InvoiceStatus.CANCELLED]) & Invoice.late_payment.is_(
            False
        )
        rows = await self.session.execute(
            select(
                Invoice.payment_link_id,
                func.count().filter(~releases_slot),
                func.count().filter(Invoice.status.in_([InvoiceStatus.PAID, InvoiceStatus.OVERPAID])),
            )
            .where(Invoice.payment_link_id.in_(payment_link_ids))
            .group_by(Invoice.payment_link_id)
        )
        return {payment_link_id: (slots_held, paid) for payment_link_id, slots_held, paid in rows if payment_link_id}

    async def list_payment_link_invoices(self, payment_link_id: str, *, limit: int) -> list[Invoice]:
        """A quantity link's invoices, newest first."""
        return list(
            await self.session.scalars(
                select(Invoice)
                .where(Invoice.payment_link_id == payment_link_id)
                .order_by(Invoice.created_at.desc(), Invoice.id.desc())
                .limit(limit)
            )
        )

    async def list_invoice_schedule_invoices(self, invoice_schedule_id: str, *, limit: int) -> list[Invoice]:
        """An invoice schedule's invoices, newest period first (``ux_invoices_invoice_schedule_id_period_start``)."""
        return list(
            await self.session.scalars(
                select(Invoice)
                .where(Invoice.invoice_schedule_id == invoice_schedule_id)
                .order_by(Invoice.invoice_schedule_period_start.desc())
                .limit(limit)
            )
        )

    async def invoice_schedule_period_is_billed(self, invoice_schedule_id: str, *, period_start: datetime) -> bool:
        return (
            await self.session.scalar(
                select(Invoice.id).where(
                    Invoice.invoice_schedule_id == invoice_schedule_id,
                    Invoice.invoice_schedule_period_start == period_start,
                )
            )
        ) is not None

    async def list_account_invoices_page(
        self,
        account_id: int,
        *,
        status: InvoiceStatus | None,
        order_id_prefix: str | None,
        after: tuple[datetime, str] | None,
        limit: int,
    ) -> list[Invoice]:
        """The account's invoices, newest first; ``after`` is the (created_at, id) of the last row of the previous
        page. ``order_id_prefix`` is matched literally (``%`` and ``_`` escaped) over
        ``ix_invoices_account_id_order_id_pattern``."""
        statement = (
            select(Invoice)
            .where(Invoice.account_id == account_id)
            .order_by(Invoice.created_at.desc(), Invoice.id.desc())
            .limit(limit)
        )
        if status is not None:
            statement = statement.where(Invoice.status == status)
        if order_id_prefix is not None:
            statement = statement.where(Invoice.order_id.startswith(order_id_prefix, autoescape=True))
        if after is not None:
            statement = statement.where(tuple_(Invoice.created_at, Invoice.id) < tuple_(*after))
        return list(await self.session.scalars(statement))

    async def list_account_invoices_for_export(
        self,
        account_id: int,
        *,
        status: InvoiceStatus | None,
        created_from: datetime | None,
        created_before: datetime | None,
        after: tuple[datetime, str] | None,
        limit: int,
    ) -> list[Invoice]:
        """The account's invoices oldest first, a batch at a time over ``ix_invoices_account_id_created_at_id``;
        ``after`` is the (created_at, id) of the previous batch's last row (the CSV export, 2026-10-21)."""
        statement = (
            select(Invoice)
            .where(Invoice.account_id == account_id)
            .order_by(Invoice.created_at.asc(), Invoice.id.asc())
            .limit(limit)
        )
        if status is not None:
            statement = statement.where(Invoice.status == status)
        if created_from is not None:
            statement = statement.where(Invoice.created_at >= created_from)
        if created_before is not None:
            statement = statement.where(Invoice.created_at < created_before)
        if after is not None:
            statement = statement.where(tuple_(Invoice.created_at, Invoice.id) > tuple_(*after))
        return list(await self.session.scalars(statement))

    async def list_watched_invoices(self, chain_key: str) -> list[Invoice]:
        """Invoices read every tick: not closed and with an asset."""
        return list(
            await self.session.scalars(
                select(Invoice)
                .where(Invoice.chain_key == chain_key, Invoice.closed_at.is_(None), Invoice.asset_symbol.is_not(None))
                .order_by(Invoice.id)
            )
        )

    async def list_unclosed_invoices(self, chain_key: str) -> list[Invoice]:
        return list(
            await self.session.scalars(
                select(Invoice).where(Invoice.chain_key == chain_key, Invoice.closed_at.is_(None)).order_by(Invoice.id)
            )
        )

    async def list_invoice_ids_with_deferred_automatic_refunds(self, chain_key: str) -> list[str]:
        """Invoices whose automatic refund waits for a price (``ix_invoices_chain_key_automatic_refund_deferred``)."""
        return list(
            await self.session.scalars(
                select(Invoice.id)
                .where(Invoice.chain_key == chain_key, Invoice.automatic_refund_deferred_since.is_not(None))
                .order_by(Invoice.id)
            )
        )

    async def lock_invoices(self, invoice_ids: list[str]) -> list[Invoice]:
        """Rows locked in id order (the one lock order of this domain)."""
        if not invoice_ids:
            return []
        return list(
            await self.session.scalars(
                select(Invoice)
                .where(Invoice.id.in_(invoice_ids))
                .order_by(Invoice.id)
                .with_for_update()
                # A row this session already holds is refreshed from the locked read, not kept stale (2026-10-09).
                .execution_options(populate_existing=True)
            )
        )

    async def count_deposits_in_creation_range(self, chain_key: str, *, creation_range: DepositCreationRange) -> int:
        """Invoices on the chain (one deposit each) in ``creation_range``."""
        return (
            await self.session.scalar(
                select(func.count()).where(*deposit_creation_range_conditions(chain_key, creation_range=creation_range))
            )
            or 0
        )

    async def list_deposits_in_creation_order(
        self,
        chain_key: str,
        *,
        creation_range: DepositCreationRange,
        after_position: DepositCreationPosition | None,
        limit: int,
    ) -> list[tuple[str, str, datetime]]:
        """(invoice id, deposit address, created at) of up to ``limit`` invoices on the chain in ``creation_range``,
        in (created_at, id) order after ``after_position`` (keyset on ``ix_invoices_created_at_id``; an unclosed-only
        range reads ``ix_invoices_chain_key_open``; None starts at the first)."""
        query = select(Invoice.id, Invoice.deposit_address, Invoice.created_at).where(
            *deposit_creation_range_conditions(chain_key, creation_range=creation_range)
        )
        if after_position is not None:
            query = query.where(
                tuple_(Invoice.created_at, Invoice.id) > (after_position.created_at, after_position.invoice_id)
            )
        rows = await self.session.execute(query.order_by(Invoice.created_at, Invoice.id).limit(limit))
        return [
            (invoice_id, deposit_address, created_at)
            for invoice_id, deposit_address, created_at in rows
            if deposit_address is not None
        ]

    async def list_deposits_after(
        self, chain_key: str, *, after_invoice_id: str | None, limit: int
    ) -> list[tuple[str, str]]:
        """(invoice id, deposit address) of up to ``limit`` invoices on the chain in id order, after
        ``after_invoice_id`` (keyset on the primary key; None starts at the first)."""
        query = select(Invoice.id, Invoice.deposit_address).where(
            Invoice.chain_key == chain_key, Invoice.deposit_address.is_not(None)
        )
        if after_invoice_id is not None:
            query = query.where(Invoice.id > after_invoice_id)
        rows = await self.session.execute(query.order_by(Invoice.id).limit(limit))
        return [(invoice_id, deposit_address) for invoice_id, deposit_address in rows if deposit_address is not None]

    async def list_invoices_by_deposit(self, chain_key: str, *, deposit_addresses: list[str]) -> list[Invoice]:
        if not deposit_addresses:
            return []
        return list(
            await self.session.scalars(
                select(Invoice).where(Invoice.chain_key == chain_key, Invoice.deposit_address.in_(deposit_addresses))
            )
        )

    async def insert_payments(
        self, payments: list[AttributedPayment], *, senders_with_code: set[str], finalized_block_number: int
    ) -> list[str]:
        """Insert each payment once, final when its block (read by number this tick) is; the ids of the invoices that
        got a new row."""
        if not payments:
            return []
        result = await self.session.execute(
            insert(InvoicePayment)
            .values(
                [
                    {
                        "invoice_id": payment.invoice_id,
                        "transaction_hash": payment.transaction_hash,
                        "log_index": payment.log_index,
                        "transaction_index": payment.transaction_index,
                        "sender": payment.sender,
                        "sender_has_code": payment.sender in senders_with_code,
                        "amount_base_units": payment.amount_base_units,
                        "block_number": payment.block_number,
                        "block_hash": payment.block_hash,
                        "block_timestamp": payment.block_timestamp,
                        "final": payment.block_number <= finalized_block_number,
                    }
                    for payment in payments
                ]
            )
            .on_conflict_do_nothing(
                index_elements=[InvoicePayment.invoice_id, InvoicePayment.transaction_hash, InvoicePayment.log_index]
            )
            .returning(InvoicePayment.invoice_id)
        )
        return list(result.scalars())

    async def list_unfinal_payments_up_to(self, invoice_ids: list[str], *, block_number: int) -> list[InvoicePayment]:
        if not invoice_ids:
            return []
        return list(
            await self.session.scalars(
                select(InvoicePayment).where(
                    InvoicePayment.invoice_id.in_(invoice_ids),
                    InvoicePayment.final.is_(False),
                    InvoicePayment.block_number <= block_number,
                )
            )
        )

    async def mark_payment_final(
        self, payment_id: int, *, block_number: int, block_hash: str, block_timestamp: int
    ) -> None:
        await self.session.execute(
            update(InvoicePayment)
            .where(InvoicePayment.id == payment_id)
            .values(block_number=block_number, block_hash=block_hash, block_timestamp=block_timestamp, final=True)
        )

    async def replace_transaction_payments(
        self,
        invoice_id: str,
        *,
        transaction_hash: str,
        payments: list[AttributedPayment],
        senders_with_code: set[str],
    ) -> None:
        """Make the invoice's rows of one transaction exactly ``payments``, all final: rows at another log index
        go, the rest are written at their canonical block (a row of the same log index is updated in place)."""
        await self.session.execute(
            delete(InvoicePayment).where(
                InvoicePayment.invoice_id == invoice_id,
                InvoicePayment.transaction_hash == transaction_hash,
                InvoicePayment.log_index.not_in([payment.log_index for payment in payments]),
            )
        )
        if not payments:
            return
        statement = insert(InvoicePayment).values(
            [
                {
                    "invoice_id": payment.invoice_id,
                    "transaction_hash": payment.transaction_hash,
                    "log_index": payment.log_index,
                    "transaction_index": payment.transaction_index,
                    "sender": payment.sender,
                    "sender_has_code": payment.sender in senders_with_code,
                    "amount_base_units": payment.amount_base_units,
                    "block_number": payment.block_number,
                    "block_hash": payment.block_hash,
                    "block_timestamp": payment.block_timestamp,
                    "final": True,
                }
                for payment in payments
            ]
        )
        await self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[InvoicePayment.invoice_id, InvoicePayment.transaction_hash, InvoicePayment.log_index],
                set_={
                    column: statement.excluded[column]
                    for column in (
                        "transaction_index",
                        "sender",
                        "sender_has_code",
                        "amount_base_units",
                        "block_number",
                        "block_hash",
                        "block_timestamp",
                        "final",
                    )
                },
            )
        )

    async def list_payments(self, invoice_ids: list[str]) -> list[InvoicePayment]:
        if not invoice_ids:
            return []
        return list(
            await self.session.scalars(
                select(InvoicePayment)
                .where(InvoicePayment.invoice_id.in_(invoice_ids))
                .order_by(InvoicePayment.block_number, InvoicePayment.transaction_index, InvoicePayment.log_index)
            )
        )

    # Stray payments: money in an asset the invoice did not ask for (INC-001, 2026-10-11)

    async def insert_stray_payments(
        self, stray_payments: list[AttributedStrayPayment], *, senders_with_code: set[str], finalized_block_number: int
    ) -> list[str]:
        """Insert each stray payment once, final when its block is; the ids of the invoices that got a new final
        row."""
        if not stray_payments:
            return []
        result = await self.session.execute(
            insert(InvoiceStrayPayment)
            .values(
                [
                    stray_payment_row_of(
                        stray_payment,
                        senders_with_code=senders_with_code,
                        final=stray_payment.block_number <= finalized_block_number,
                    )
                    for stray_payment in stray_payments
                ]
            )
            .on_conflict_do_nothing(index_elements=STRAY_PAYMENT_CONFLICT_COLUMNS)
            .returning(InvoiceStrayPayment.invoice_id, InvoiceStrayPayment.final)
        )
        return sorted({invoice_id for invoice_id, final in result if final})

    async def list_unfinal_stray_payments_up_to(
        self, chain_key: str, *, block_number: int
    ) -> list[tuple[InvoiceStrayPayment, str]]:
        """Unfinal stray rows at or below the block, open or closed invoice alike, each with its deposit address."""
        rows = await self.session.execute(
            select(InvoiceStrayPayment, Invoice.deposit_address)
            .join(Invoice, Invoice.id == InvoiceStrayPayment.invoice_id)
            .where(
                Invoice.chain_key == chain_key,
                Invoice.deposit_address.is_not(None),
                InvoiceStrayPayment.final.is_(False),
                InvoiceStrayPayment.block_number <= block_number,
            )
        )
        return [
            (stray_payment, deposit_address) for stray_payment, deposit_address in rows if deposit_address is not None
        ]

    async def mark_stray_payment_final(
        self, stray_payment_id: int, *, block_number: int, block_hash: str, block_timestamp: int
    ) -> str | None:
        """The row's invoice id, or None when another transaction already settled it."""
        return await self.session.scalar(
            update(InvoiceStrayPayment)
            .where(InvoiceStrayPayment.id == stray_payment_id, InvoiceStrayPayment.final.is_(False))
            .values(block_number=block_number, block_hash=block_hash, block_timestamp=block_timestamp, final=True)
            .returning(InvoiceStrayPayment.invoice_id)
        )

    async def delete_stray_payments(self, stray_payment_ids: list[int]) -> None:
        if stray_payment_ids:
            await self.session.execute(delete(InvoiceStrayPayment).where(InvoiceStrayPayment.id.in_(stray_payment_ids)))

    async def replace_transaction_stray_payments(
        self,
        invoice_id: str,
        *,
        asset_symbol: str,
        transaction_hash: str,
        stray_payments: list[AttributedStrayPayment],
        senders_with_code: set[str],
    ) -> bool:
        """Make the invoice's stray rows of one transaction and asset exactly ``stray_payments``, all final (as
        ``replace_transaction_payments``); True when any row is left."""
        await self.session.execute(
            delete(InvoiceStrayPayment).where(
                InvoiceStrayPayment.invoice_id == invoice_id,
                InvoiceStrayPayment.asset_symbol == asset_symbol,
                InvoiceStrayPayment.transaction_hash == transaction_hash,
                InvoiceStrayPayment.log_index.not_in([stray_payment.log_index for stray_payment in stray_payments]),
            )
        )
        if not stray_payments:
            return False
        statement = insert(InvoiceStrayPayment).values(
            [
                stray_payment_row_of(stray_payment, senders_with_code=senders_with_code, final=True)
                for stray_payment in stray_payments
            ]
        )
        await self.session.execute(
            statement.on_conflict_do_update(
                index_elements=STRAY_PAYMENT_CONFLICT_COLUMNS,
                set_={
                    column: statement.excluded[column]
                    for column in (
                        "transaction_index",
                        "sender",
                        "sender_has_code",
                        "amount_base_units",
                        "block_number",
                        "block_hash",
                        "block_timestamp",
                        "final",
                    )
                },
            )
        )
        return True

    async def list_stray_payments(self, invoice_ids: list[str]) -> list[InvoiceStrayPayment]:
        if not invoice_ids:
            return []
        return list(
            await self.session.scalars(
                select(InvoiceStrayPayment)
                .where(InvoiceStrayPayment.invoice_id.in_(invoice_ids))
                .order_by(
                    InvoiceStrayPayment.block_number,
                    InvoiceStrayPayment.transaction_index,
                    InvoiceStrayPayment.log_index,
                    InvoiceStrayPayment.id,
                )
            )
        )

    # Monero payments (PLAN.md section 14): the sidecar writes the rows, this domain only sets ``credited_at``

    async def list_recently_closed_invoice_ids_with_monero_payments_to_decide(
        self, chain_key: str, *, closed_after: datetime
    ) -> list[str]:
        """Invoices of the chain closed after ``closed_after`` with an uncredited payment row in a block (a late payment
        candidate) or a credited row out of a block or awaiting a new first cross-check (a reorg, possibly re-mined
        before this pass). Over
        ``ix_monero_payments_chain_key_uncredited`` and ``ix_monero_payments_chain_key_credited_uncreditable``, so it
        grows with the rows awaiting a decision, not with closed invoices."""
        uncredited_in_block = MoneroPayment.credited_at.is_(None) & MoneroPayment.block_height.is_not(None)
        credited_uncreditable = MoneroPayment.credited_at.is_not(None) & (
            MoneroPayment.block_height.is_(None) | (MoneroPayment.cross_check_state != MoneroCrossCheckState.CONFIRMED)
        )
        invoice_ids: set[str] = set()
        for row_predicate in (uncredited_in_block, credited_uncreditable):
            invoice_ids.update(
                await self.session.scalars(
                    select(Invoice.id)
                    .join(MoneroPayment, MoneroPayment.invoice_id == Invoice.id)
                    .where(MoneroPayment.chain_key == chain_key, row_predicate, Invoice.closed_at > closed_after)
                    .distinct()
                )
            )
        return sorted(invoice_ids)

    async def list_monero_payments(self, invoice_ids: list[str], *, lock: bool = False) -> list[MoneroPayment]:
        """In chain order (pool rows last, by when they were first seen). ``lock`` (``FOR UPDATE``) after the
        invoices' own locks: the watcher's credit then never interleaves with the sidecar's update of a row."""
        if not invoice_ids:
            return []
        statement = (
            select(MoneroPayment)
            .where(MoneroPayment.invoice_id.in_(invoice_ids))
            .order_by(
                MoneroPayment.block_height.asc().nulls_last(),
                MoneroPayment.first_seen_at,
                MoneroPayment.id,
            )
        )
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return list(await self.session.scalars(statement))

    # Reconciliation reads (2026-10-12)

    async def list_invoices(self, invoice_ids: list[str]) -> list[Invoice]:
        """Unlocked, for reading only."""
        if not invoice_ids:
            return []
        return list(await self.session.scalars(select(Invoice).where(Invoice.id.in_(invoice_ids)).order_by(Invoice.id)))

    async def list_deposit_sweep_transaction_hashes(
        self, chain_key: str, *, deposit_addresses: list[str]
    ) -> list[tuple[str, str]]:
        rows = await self.session.execute(
            select(DepositSweepEvent.deposit_address, DepositSweepEvent.transaction_hash).where(
                DepositSweepEvent.chain_key == chain_key, DepositSweepEvent.deposit_address.in_(deposit_addresses)
            )
        )
        return [(deposit_address, transaction_hash) for deposit_address, transaction_hash in rows]

    async def list_sweep_blocked_invoices(self, chain_key: str) -> list[Invoice]:
        return list(
            await self.session.scalars(
                select(Invoice)
                .where(Invoice.chain_key == chain_key, Invoice.sweep_blocked.is_(True))
                .order_by(Invoice.id)
            )
        )

    async def list_sender_less_stray_payments(self, chain_key: str) -> list[InvoiceStrayPayment]:
        return list(
            await self.session.scalars(
                select(InvoiceStrayPayment)
                .join(Invoice, Invoice.id == InvoiceStrayPayment.invoice_id)
                .where(Invoice.chain_key == chain_key, InvoiceStrayPayment.sender.is_(None))
                .order_by(InvoiceStrayPayment.id)
            )
        )

    async def list_invoices_deferred_before(self, chain_key: str, *, deferred_before: datetime) -> list[Invoice]:
        return list(
            await self.session.scalars(
                select(Invoice)
                .where(Invoice.chain_key == chain_key, Invoice.automatic_refund_deferred_since < deferred_before)
                .order_by(Invoice.id)
            )
        )

    async def insert_sweep_events(self, chain_key: str, *, events: list[DepositSweptEvent]) -> list[str]:
        """Insert each event once (chain, transaction hash, log index); the deposit addresses that got a new row."""
        if not events:
            return []
        result = await self.session.execute(
            insert(DepositSweepEvent)
            .values(
                [
                    {
                        "chain_key": chain_key,
                        "transaction_hash": event.transaction_hash,
                        "log_index": event.log_index,
                        "deposit_address": event.deposit_address,
                        "destination_address": event.destination_address,
                        "token_address": event.token_address,
                        "amount_base_units": event.amount,
                        "block_number": event.block_number,
                        "block_hash": event.block_hash,
                    }
                    for event in events
                ]
            )
            .on_conflict_do_nothing(
                index_elements=[
                    DepositSweepEvent.chain_key,
                    DepositSweepEvent.transaction_hash,
                    DepositSweepEvent.log_index,
                ]
            )
            .returning(DepositSweepEvent.deposit_address)
        )
        return list(set(result.scalars()))

    async def sum_sweep_events_up_to_block(
        self, chain_key: str, *, deposit_addresses: list[str], up_to_block_number: int
    ) -> list[tuple[str, str | None, int]]:
        """(deposit, token or None, total moved) of the stored ``DepositSwept`` events at or before the block, summed
        in Postgres over ``ix_deposit_sweep_events_deposit``."""
        if not deposit_addresses:
            return []
        rows = await self.session.execute(
            select(
                DepositSweepEvent.deposit_address,
                DepositSweepEvent.token_address,
                func.sum(DepositSweepEvent.amount_base_units),
            )
            .where(
                DepositSweepEvent.chain_key == chain_key,
                DepositSweepEvent.deposit_address.in_(deposit_addresses),
                DepositSweepEvent.block_number <= up_to_block_number,
            )
            .group_by(DepositSweepEvent.deposit_address, DepositSweepEvent.token_address)
        )
        return [(deposit_address, token_address, int(total)) for deposit_address, token_address, total in rows]

    async def list_sweep_events(self, chain_key: str, *, deposit_addresses: list[str]) -> list[DepositSweepEvent]:
        if not deposit_addresses:
            return []
        return list(
            await self.session.scalars(
                select(DepositSweepEvent)
                .where(
                    DepositSweepEvent.chain_key == chain_key, DepositSweepEvent.deposit_address.in_(deposit_addresses)
                )
                .order_by(DepositSweepEvent.block_number, DepositSweepEvent.log_index)
            )
        )
