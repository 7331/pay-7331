"""StaticDepositReadingsService: the watcher's half of static deposit addresses (PLAN.md section 17), on one session.

The chain watcher's ``StaticDepositScanService`` reads balances and Transfer logs; this applies them:

- What an address ever received in a coin at the finalized block is its balance there plus every stored
  ``DepositSwept`` of it in that coin at or before that block (PLAN.md section 4's gross), summed in Postgres. The
  rise over what its payment rows hold is new money (``find_rises``, read only, so the scan knows whose logs to read).
- ``apply_readings`` locks the addresses (``FOR UPDATE``), works the rises out again, and records each as payment
  rows: the coin's unrecorded Transfer logs into it after its last scanned block, in chain order while they fit, then
  one row with no sender for the rest. Money in a coin the address takes is credited at the chain's price now (a
  pegged stablecoin at the peg rule) with ``static_address.payment_received``; money in another configured coin is
  ``wrong_coin`` with ``static_address.stray_payment``. Every final rise is recorded and announced: the chain cannot
  be told to refuse money, so there is no disable (PLAN.md section 17). Recording money sets ``last_received_at``,
  which keeps the address in the scan's hot tier. An address whose credited coin has no price is left whole for the
  next pass (no price is no verdict).
- Then each address holding at least a coin's ``minimum_sweep_amount`` gets a sweep unless one is active or it is
  ``sweep_blocked``. No float refunds exist here: a static address has no amount due, so no excess.
- ``apply_sweep_outcome`` runs on the settlement runner's resolving transaction: ``final`` records
  ``static_address.swept``; a failure (not a frozen key's) sets ``sweep_blocked`` and records
  ``static_address.sweep_failed``.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Self

from pydantic import JsonValue
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.chains.types import AssetConfiguration, EvmChainConfiguration
from app.payments.invoices.service import InvoiceService
from app.payments.invoices.wiring import InvoiceWiring
from app.payments.price_quotes.types import ChainPrices
from app.payments.settlements.enums import OperationStatus
from app.payments.settlements.types import StaticDepositSweepOutcome
from app.payments.static_deposit_addresses.enums import StaticDepositAddressEventType, UncreditedReason
from app.payments.static_deposit_addresses.errors import StaticDepositAddressNotFoundError
from app.payments.static_deposit_addresses.log_events import (
    StaticDepositPaymentRecorded,
    StaticDepositSweepOutcomeApplied,
    StaticDepositSweepRequestedByHand,
)
from app.payments.static_deposit_addresses.models import StaticDepositAddress, StaticDepositPayment
from app.payments.static_deposit_addresses.repository import StaticDepositAddressRepository
from app.payments.static_deposit_addresses.schemas import StaticDepositAddressSweep
from app.payments.static_deposit_addresses.service import StaticDepositAddressService
from app.payments.static_deposit_addresses.types import (
    ScannedStaticDeposit,
    StaticDepositReading,
    StaticDepositRise,
    StaticDepositRiseSearch,
    StaticDepositTransfer,
)
from app.shared import logging as log
from app.shared.errors import InvalidInputError

logger = log.get_logger(__name__)

# A payment row no Transfer log explains.
UNATTRIBUTED_LOG_INDEX = -1
CENT = Decimal("0.01")


def usd_value_of(amount_base_units: int, *, asset: AssetConfiguration, asset_price_usd: Decimal) -> Decimal:
    return (Decimal(amount_base_units).scaleb(-asset.decimals) * asset_price_usd).quantize(CENT, rounding=ROUND_HALF_UP)


class StaticDepositReadingsService:
    def __init__(self, *, repository: StaticDepositAddressRepository, invoice_service: InvoiceService) -> None:
        self.repository = repository
        self.invoice_service = invoice_service
        self.chains_service = invoice_service.chains_service
        self.settlement_service = invoice_service.settlement_service
        self.account_event_service = invoice_service.account_event_service
        self.presenter = StaticDepositAddressService(repository=repository, invoice_service=invoice_service)

    @classmethod
    def on_session(cls, session: AsyncSession, *, redis: Redis, wiring: InvoiceWiring) -> Self:
        return cls(
            repository=StaticDepositAddressRepository(session=session),
            invoice_service=InvoiceService.on_session(session, redis=redis, wiring=wiring),
        )

    # The scan's pages

    async def count_chain_static_deposit_addresses(self, chain_key: str, *, hot: bool, hot_since: datetime) -> int:
        return await self.repository.count_chain_static_deposit_addresses(chain_key, hot=hot, hot_since=hot_since)

    async def list_scan_page(
        self, chain_key: str, *, hot: bool, hot_since: datetime, after: ScannedStaticDeposit | None, limit: int
    ) -> list[ScannedStaticDeposit]:
        return [
            ScannedStaticDeposit(
                static_deposit_address_id=row.id,
                deposit_address=row.deposit_address,
                created_at=row.created_at,
                last_scanned_block_number=row.last_scanned_block_number,
            )
            for row in await self.repository.list_chain_static_deposit_addresses_in_creation_order(
                chain_key,
                hot=hot,
                hot_since=hot_since,
                after=(after.created_at, after.static_deposit_address_id) if after is not None else None,
                limit=limit,
            )
        ]

    # The operator's checks (chain_watcher/reconciliation_service.py)

    async def list_sweep_blocked_static_deposit_address_ids(self, chain_key: str) -> list[str]:
        return [row.id for row in await self.repository.list_sweep_blocked_static_deposit_addresses(chain_key)]

    async def list_chain_static_deposits_after(
        self, chain_key: str, *, after_static_deposit_address_id: str | None, limit: int
    ) -> list[ScannedStaticDeposit]:
        return [
            ScannedStaticDeposit(
                static_deposit_address_id=row.id,
                deposit_address=row.deposit_address,
                created_at=row.created_at,
                last_scanned_block_number=row.last_scanned_block_number,
                skipped_sweep_token_symbols=tuple(row.skipped_sweep_token_symbols),
            )
            for row in await self.repository.list_chain_static_deposit_addresses_after(
                chain_key, after_id=after_static_deposit_address_id, limit=limit
            )
        ]

    async def list_static_deposit_address_ids_with_active_sweep(
        self, static_deposit_address_ids: list[str]
    ) -> set[str]:
        return await self.settlement_service.list_static_deposit_address_ids_with_active_sweep(
            static_deposit_address_ids
        )

    # Rises

    async def compute_rises(
        self,
        chain: EvmChainConfiguration,
        *,
        static_deposit_addresses: Sequence[StaticDepositAddress],
        readings: list[StaticDepositReading],
        finalized_block_number: int,
    ) -> StaticDepositRiseSearch:
        readings_by_id = {reading.static_deposit_address_id: reading for reading in readings}
        swept_by_deposit_and_token = await self.invoice_service.sum_finalized_deposit_sweeps(
            chain.key,
            deposit_addresses=[row.deposit_address for row in static_deposit_addresses],
            up_to_block_number=finalized_block_number,
        )
        recorded_by_address_and_asset = await self.repository.sum_recorded_by_address_and_asset(
            [row.id for row in static_deposit_addresses]
        )
        rises: list[StaticDepositRise] = []
        gross_below_recorded_keys: set[tuple[str, str]] = set()
        for row in static_deposit_addresses:
            reading = readings_by_id.get(row.id)
            if reading is None:
                continue
            for asset in chain.assets:
                balance = reading.balances_by_symbol.get(asset.symbol)
                if balance is None:
                    continue
                token_key = asset.token_address.lower() if asset.token_address else None
                gross = balance + swept_by_deposit_and_token.get((row.deposit_address.lower(), token_key), 0)
                recorded = recorded_by_address_and_asset.get((row.id, asset.symbol), 0)
                if gross < recorded:
                    gross_below_recorded_keys.add((row.id, asset.symbol))
                    continue
                if gross > recorded:
                    rises.append(
                        StaticDepositRise(
                            static_deposit_address_id=row.id,
                            deposit_address=row.deposit_address,
                            asset_symbol=asset.symbol,
                            token_address=asset.token_address,
                            amount_base_units=gross - recorded,
                            last_scanned_block_number=row.last_scanned_block_number,
                        )
                    )
        return StaticDepositRiseSearch(rises=rises, gross_below_recorded_keys=frozenset(gross_below_recorded_keys))

    async def find_rises(
        self, chain_key: str, *, readings: list[StaticDepositReading], finalized_block_number: int
    ) -> StaticDepositRiseSearch:
        """New money per address and coin, read without locks: which addresses' Transfer logs to read."""
        static_deposit_addresses = await self.repository.list_static_deposit_addresses(
            [reading.static_deposit_address_id for reading in readings]
        )
        return await self.compute_rises(
            self.chains_service.get_evm_chain(chain_key),
            static_deposit_addresses=static_deposit_addresses,
            readings=readings,
            finalized_block_number=finalized_block_number,
        )

    # Applying a slice

    async def apply_readings(
        self,
        chain_key: str,
        *,
        readings: list[StaticDepositReading],
        transfers: list[StaticDepositTransfer],
        finalized_block_number: int,
    ) -> frozenset[tuple[str, str]]:
        """Records the slice's new money and asks for sweeps; answers the (address id, coin) pairs left for want of a
        price."""
        chain = self.chains_service.get_evm_chain(chain_key)
        locked = await self.repository.lock_static_deposit_addresses(
            sorted(reading.static_deposit_address_id for reading in readings)
        )
        rise_search = await self.compute_rises(
            chain, static_deposit_addresses=locked, readings=readings, finalized_block_number=finalized_block_number
        )
        rises_by_id: dict[str, list[StaticDepositRise]] = {}
        for rise in rise_search.rises:
            rises_by_id.setdefault(rise.static_deposit_address_id, []).append(rise)
        prices = await self.invoice_service.find_chain_prices(chain.key) if rise_search.rises else None
        transfers_by_deposit_and_token: dict[tuple[str, str], list[StaticDepositTransfer]] = {}
        for transfer in sorted(transfers, key=lambda transfer: (transfer.block_number, transfer.log_index)):
            key = (transfer.deposit_address.lower(), transfer.token_address.lower())
            transfers_by_deposit_and_token.setdefault(key, []).append(transfer)
        recorded_transfer_keys = await self.repository.list_recorded_transfer_keys(
            [row.id for row in locked],
            transaction_hashes=sorted({transfer.transaction_hash for transfer in transfers}),
        )
        readings_by_id = {reading.static_deposit_address_id: reading for reading in readings}
        credit_deferred_keys: set[tuple[str, str]] = set()
        for row in locked:
            address_rises = rises_by_id.get(row.id, [])
            unpriced_symbols = self.find_unpriced_credited_symbols(row, rises=address_rises, prices=prices)
            if unpriced_symbols:
                credit_deferred_keys.update((row.id, symbol) for symbol in unpriced_symbols)
                continue
            for rise in address_rises:
                await self.record_rise(
                    row,
                    rise=rise,
                    asset=self.chains_service.get_asset(chain.key, symbol=rise.asset_symbol),
                    prices=prices,
                    transfers=self.select_unrecorded_transfers(
                        row,
                        transfers=transfers_by_deposit_and_token.get(
                            (row.deposit_address.lower(), (rise.token_address or "").lower()), []
                        ),
                        recorded_transfer_keys=recorded_transfer_keys,
                    ),
                    finalized_block_number=finalized_block_number,
                )
            if address_rises:
                row.last_received_at = datetime.now(UTC)
            reading = readings_by_id[row.id]
            if len(reading.balances_by_symbol) == len(chain.assets):
                # A coin whose read failed keeps the older block, so its Transfer logs are read from there next time.
                row.last_scanned_block_number = finalized_block_number
            await self.request_sweep_when_held(row, chain=chain, reading=reading)
        await self.repository.session.flush()
        return frozenset(credit_deferred_keys)

    def select_unrecorded_transfers(
        self,
        row: StaticDepositAddress,
        *,
        transfers: list[StaticDepositTransfer],
        recorded_transfer_keys: set[tuple[str, str, int]],
    ) -> list[StaticDepositTransfer]:
        """The Transfers that may explain this rise: after the last scanned block (one at or below it was in a balance
        already recorded) and not on a row yet."""
        return [
            transfer
            for transfer in transfers
            if (row.last_scanned_block_number is None or transfer.block_number > row.last_scanned_block_number)
            and (row.id, transfer.transaction_hash, transfer.log_index) not in recorded_transfer_keys
        ]

    def find_unpriced_credited_symbols(
        self, row: StaticDepositAddress, *, rises: list[StaticDepositRise], prices: ChainPrices | None
    ) -> list[str]:
        """The coins it credits that rose but have no price: then the address waits whole for the next pass."""
        return [
            rise.asset_symbol
            for rise in rises
            if rise.asset_symbol in row.assets and (prices is None or rise.asset_symbol not in prices.asset_prices_usd)
        ]

    async def record_rise(
        self,
        row: StaticDepositAddress,
        *,
        rise: StaticDepositRise,
        asset: AssetConfiguration,
        prices: ChainPrices | None,
        transfers: list[StaticDepositTransfer],
        finalized_block_number: int,
    ) -> None:
        """The rise as rows: the Transfer logs that fit in chain order, then one row with no sender for the rest."""
        uncredited_reason = None if rise.asset_symbol in row.assets else UncreditedReason.WRONG_COIN
        asset_price_usd = prices.asset_prices_usd.get(asset.symbol) if prices is not None else None
        unexplained_base_units = rise.amount_base_units
        payment_parts: list[tuple[int, StaticDepositTransfer | None]] = []
        for transfer in transfers:
            if transfer.amount_base_units > unexplained_base_units:
                break
            payment_parts.append((transfer.amount_base_units, transfer))
            unexplained_base_units -= transfer.amount_base_units
        if unexplained_base_units > 0:
            payment_parts.append((unexplained_base_units, None))
        for amount_base_units, transfer in payment_parts:
            payment = await self.repository.insert_payment(
                StaticDepositPayment(
                    static_deposit_address_id=row.id,
                    asset_symbol=asset.symbol,
                    amount_base_units=amount_base_units,
                    asset_price_usd=asset_price_usd,
                    amount_usd=(
                        usd_value_of(amount_base_units, asset=asset, asset_price_usd=asset_price_usd)
                        if asset_price_usd is not None
                        else None
                    ),
                    credited=uncredited_reason is None,
                    uncredited_reason=uncredited_reason.value if uncredited_reason is not None else None,
                    transaction_hash=transfer.transaction_hash if transfer is not None else None,
                    log_index=transfer.log_index if transfer is not None else UNATTRIBUTED_LOG_INDEX,
                    sender=transfer.sender if transfer is not None else None,
                    block_number=transfer.block_number if transfer is not None else finalized_block_number,
                )
            )
            await self.repository.session.refresh(payment)
            log.emit(
                logger,
                event=StaticDepositPaymentRecorded(
                    static_deposit_address_id=row.id,
                    asset_symbol=asset.symbol,
                    credited=payment.credited,
                    uncredited_reason=payment.uncredited_reason,
                    attributed=transfer is not None,
                ),
            )
            await self.record_event(
                row,
                event_type=(
                    StaticDepositAddressEventType.PAYMENT_RECEIVED
                    if uncredited_reason is None
                    else StaticDepositAddressEventType.STRAY_PAYMENT
                ),
                detail={
                    "payment": self.presenter.present_payment(payment, chain_key=row.chain_key).model_dump(mode="json")
                },
            )

    async def record_event(
        self,
        row: StaticDepositAddress,
        *,
        event_type: StaticDepositAddressEventType,
        detail: dict[str, JsonValue],
    ) -> None:
        """An account event with no invoice; ``data`` is the address (without payments) and the detail. An address
        whose account was deleted records nothing."""
        if row.account_id is None:
            return
        await self.repository.session.flush()
        static_deposit_address_response = await self.presenter.present_with_totals(row, with_payments=False)
        await self.account_event_service.record_account_event(
            None,
            account_id=row.account_id,
            event_type=event_type.value,
            data={"static_deposit_address": static_deposit_address_response.model_dump(mode="json"), **detail},
        )

    async def request_sweep_when_held(
        self, row: StaticDepositAddress, *, chain: EvmChainConfiguration, reading: StaticDepositReading
    ) -> None:
        """A sweep when it holds at least a minimum sweep in a coin its sweeps move: the tokens the operator told it
        to skip stay out of the check and out of the sweep, so one unsweepable token does not block every later
        payment."""
        if row.sweep_blocked:
            return
        skipped_symbols = set(row.skipped_sweep_token_symbols)
        holds_sweepable_money = any(
            reading.balances_by_symbol.get(asset.symbol, 0) >= asset.minimum_sweep_base_units
            for asset in chain.assets
            if asset.symbol not in skipped_symbols
        )
        if holds_sweepable_money:
            await self.settlement_service.request_static_deposit_sweep(
                row.id,
                chain_key=row.chain_key,
                deposit_address=row.deposit_address,
                deposit_salt=row.deposit_salt,
                destination_address=row.destination_address,
                skipped_token_addresses=[
                    asset.token_address
                    for asset in chain.assets
                    if asset.symbol in skipped_symbols and asset.token_address is not None
                ],
            )

    # Sweeps

    async def apply_sweep_outcome(self, outcome: StaticDepositSweepOutcome) -> None:
        """On the runner's resolving transaction: ``final`` tells the account it was swept;
        a failure that was not only a frozen key's blocks further sweeps and tells the account."""
        row = await self.repository.get_static_deposit_address(outcome.static_deposit_address_id, lock=True)
        if row is None or outcome.is_sweep_for_another_key:
            return
        static_deposit_address_sweep = StaticDepositAddressSweep(
            tx_hash=outcome.transaction_hash, status=outcome.status.value
        )
        match outcome.status:
            case OperationStatus.FINAL:
                row.sweep_blocked = False
                event_type = StaticDepositAddressEventType.SWEPT
            case OperationStatus.FAILED | OperationStatus.NEEDS_OWNER:
                row.sweep_blocked = True
                event_type = StaticDepositAddressEventType.SWEEP_FAILED
            case _:
                return
        await self.record_event(
            row, event_type=event_type, detail={"sweep": static_deposit_address_sweep.model_dump(mode="json")}
        )
        log.emit(
            logger,
            event=StaticDepositSweepOutcomeApplied(
                static_deposit_address_id=row.id, operation_id=outcome.operation_id, status=outcome.status.value
            ),
        )

    async def sweep_by_hand(
        self,
        static_deposit_address_id: str,
        *,
        skipped_asset_symbols: Sequence[str],
        clears_skipped_tokens: bool = False,
    ) -> bool:
        """The operator's ``sweep-static-deposit-address``: clears ``sweep_blocked``, adds the ``--skip-token`` coins to
        the tokens every later sweep of it leaves out (``--clear-skipped-tokens`` empties that list first), and asks for
        a sweep without them. False when one is already active."""
        row = await self.repository.get_static_deposit_address(static_deposit_address_id, lock=True)
        if row is None:
            raise StaticDepositAddressNotFoundError("No static deposit address with that id.")
        skipped_assets = [
            self.chains_service.get_asset(row.chain_key, symbol=symbol) for symbol in skipped_asset_symbols
        ]
        if any(asset.token_address is None for asset in skipped_assets):
            raise InvalidInputError("skip-token: the contract always forwards the native coin; it cannot be skipped")
        kept_symbols = set() if clears_skipped_tokens else set(row.skipped_sweep_token_symbols)
        skipped_symbols = kept_symbols | {asset.symbol for asset in skipped_assets}
        chain = self.chains_service.get_evm_chain(row.chain_key)
        row.skipped_sweep_token_symbols = [asset.symbol for asset in chain.assets if asset.symbol in skipped_symbols]
        row.sweep_blocked = False
        recorded = await self.settlement_service.request_static_deposit_sweep(
            row.id,
            chain_key=row.chain_key,
            deposit_address=row.deposit_address,
            deposit_salt=row.deposit_salt,
            destination_address=row.destination_address,
            skipped_token_addresses=[
                asset.token_address
                for asset in chain.assets
                if asset.symbol in skipped_symbols and asset.token_address is not None
            ],
        )
        log.emit(logger, event=StaticDepositSweepRequestedByHand(static_deposit_address_id=row.id, recorded=recorded))
        return recorded
