"""ReconciliationService: what needs the operator on one chain, read from Postgres, Redis and the chain (2026-10-12).

Read only: it never changes a row and never sends a transaction. ``python -m app.cli reconcile`` runs every check;
the watcher runs ``run_alert_checks`` (the cheap ones) every few ticks and tells the operator account about what they
find (``operator_alerts_service.py``). Each check returns one summary line and its findings:

- watcher progress: the finalized head against the last one the watcher applied (``chain_cursors``), and, for
  ``reconcile``, how long ago its last completed tick was (the Redis heartbeat).
- price feeds: each Chainlink feed's ``latestRoundData`` ``updatedAt`` (read as the price code reads it,
  https://docs.chain.link/data-feeds/api-reference#latestrounddata) against its own
  ``price_feed_heartbeat_seconds`` (the asset's, which is the chain's unless the asset sets one) plus a grace minute;
  a feed that reverts or answers nonsense.
- outgoing operations: sweeps and refunds still pending, broadcast or mined after ``STUCK_OPERATION_AGE``; refunds
  ``needs_owner`` and ``waiting_for_float``; invoices whose sweep failed (``sweep_blocked``); the same for static
  deposit addresses (``static_deposit_reconciliation.py``, PLAN.md section 17).
- operator float: per asset, the refunds waiting for float against what the operator holds at ``latest``.
- unapplied outcomes: per invoice, operations whose outcome records an event (a failed sweep, a final, needs_owner or
  waiting refund) against those events; fewer events than operations means an outcome was committed on its operation
  but never applied to the invoice. Several failed sweeps on one invoice hint at a lost block too.
- settlement consistency: a failed sweep on an invoice that is not ``sweep_blocked`` and has had no later final sweep
  and no ``DepositSwept`` since (the block was lost and nothing will sweep it); an underpaid invoice whose final
  refunds in its own asset moved at least what it received (the refund's cancel was missed).
- stray money with no sender (``owner_settles``).
- automatic refunds deferred for want of a price for longer than ``DEFERRED_REFUND_MAXIMUM_AGE``.
- WAL archiving (``wal_archiving_check_service.py``): Postgres archiving its WAL off the box failing or stalled while
  ``archive_mode`` is on; not about the chain, so every chain's run reads the same server.
- deposit balances: every deposit ever made, in every configured asset, at the finalized block (Multicall3, as the
  rescan reads them); money at or above the asset's minimum sweep on an invoice whose payment window is over and
  that has no active sweep. ``reconcile`` only: it reads every deposit.
- static deposit balances: the same over every static deposit address (``static_deposit_reconciliation.py``).
  ``reconcile`` only.
"""

from datetime import UTC, datetime, timedelta
from typing import Self

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.evm_rpc_api.client import (
    EvmRpcEndpoint,
    decode_uint256,
    encode_native_balance_call,
    encode_token_balance_call,
)
from app.infrastructure.evm_rpc_api.errors import EvmExecutionRevertedError, EvmRpcUnavailableError
from app.infrastructure.evm_rpc_api.schemas import BlockHeads, Multicall3Call, Multicall3Result
from app.payments.account_events.service import AccountEventService
from app.payments.chain_watcher.heartbeat_service import (
    WATCHER_HEARTBEAT_MAXIMUM_AGE_SECONDS,
    WatcherHeartbeatService,
)
from app.payments.chain_watcher.models import CursorStream
from app.payments.chain_watcher.price_feed_reconciliation import check_price_feeds
from app.payments.chain_watcher.repository import ChainCursorRepository
from app.payments.chain_watcher.static_deposit_reconciliation import (
    check_static_deposit_balances,
    describe_holdings,
    find_static_deposit_sweep_findings,
)
from app.payments.chain_watcher.types import ReconciliationCheckResult, ReconciliationFinding
from app.payments.chain_watcher.wal_archiving_check_service import WalArchivingCheckService
from app.payments.chains.types import AssetConfiguration, ChainConfiguration, EvmChainConfiguration
from app.payments.invoices.enums import InvoiceEventType, InvoiceStatus
from app.payments.invoices.reconciliation_service import InvoiceReconciliationService
from app.payments.invoices.types import InvoiceSettlementState
from app.payments.invoices.wiring import InvoiceWiring
from app.payments.settlements.enums import OperationKind, OperationStatus
from app.payments.settlements.service import SettlementService
from app.payments.settlements.types import OperationValue
from app.payments.static_deposit_addresses.readings_service import StaticDepositReadingsService
from app.shared.amounts import format_base_units
from app.shared.durations import describe_seconds

# Past it the watcher is not keeping up (catching up after a stall, or failing): 5 minutes of Polygon blocks (150), or
# twice the chain's expected finality where that is longer, since `finalized` moves in jumps (Arbitrum's, of minutes of
# blocks, can land between a tick's read and this check; PLAN.md section 15).
WATCHER_BEHIND_MAXIMUM_SECONDS = 300
# A sweep or refund normally goes final within a few minutes after its block is finalized; fee bumps and the 5-minute
# unproven-end wait included.
STUCK_OPERATION_AGE = timedelta(minutes=30)
DEFERRED_REFUND_MAXIMUM_AGE = timedelta(minutes=30)
# Deposits per page of the balance read (each page is one aggregate3 per 500 calls).
DEPOSITS_PER_BALANCE_PAGE = 1_000
# The event each resolved operation records on its invoice (``ChainReadingsService.apply_settlement_outcomes``).
OUTCOME_EVENT_TYPES: dict[tuple[OperationKind, OperationStatus], InvoiceEventType] = {
    (OperationKind.SWEEP, OperationStatus.FAILED): InvoiceEventType.SWEEP_FAILED,
    (OperationKind.REFUND, OperationStatus.FINAL): InvoiceEventType.REFUNDED,
    (OperationKind.REFUND, OperationStatus.NEEDS_OWNER): InvoiceEventType.REFUND_NEEDS_OWNER,
    (OperationKind.REFUND, OperationStatus.WAITING_FOR_FLOAT): InvoiceEventType.REFUND_WAITING_FOR_FLOAT,
}


def balance_call_of(asset: AssetConfiguration, *, holder_address: str) -> Multicall3Call:
    if asset.token_address is None:
        return encode_native_balance_call(holder_address)
    return encode_token_balance_call(asset.token_address, holder_address=holder_address)


def balance_of(result: Multicall3Result) -> int | None:
    return decode_uint256(result.return_data) if result.succeeded else None


def find_unapplied_failed_sweep_finding(
    invoice_id: str,
    *,
    state: InvoiceSettlementState | None,
    operations: list[OperationValue],
    sweep_transactions_by_deposit: dict[str, set[str]],
) -> ReconciliationFinding | None:
    """A failed sweep on an invoice that is not ``sweep_blocked`` and has had no later sweep and no ``DepositSwept``
    since: the failure looks unapplied. None for a blocked or a since swept one."""
    if state is None or state.sweep_blocked:
        return None  # A blocked sweep is the outgoing operations check's.
    last_failed_sweep = max(
        (
            operation
            for operation in operations
            if operation.kind == OperationKind.SWEEP
            and operation.status == OperationStatus.FAILED
            and not operation.is_sweep_for_another_key
        ),
        key=lambda operation: operation.operation_id,
    )
    later_sweeps = [
        operation
        for operation in operations
        if operation.kind == OperationKind.SWEEP and operation.operation_id > last_failed_sweep.operation_id
    ]
    # A DepositSwept from a transaction that is not an earlier sweep's: someone swept after the failure.
    earlier_sweep_transactions = {
        operation.transaction_hash
        for operation in operations
        if operation.kind == OperationKind.SWEEP
        and operation.operation_id <= last_failed_sweep.operation_id
        and operation.transaction_hash is not None
    }
    later_sweep_events = sweep_transactions_by_deposit.get(state.deposit_address, set()) - (earlier_sweep_transactions)
    if later_sweeps or later_sweep_events:
        return None
    return ReconciliationFinding(
        problem_key=f"sweep_failed_unblocked:{invoice_id}",
        alert_kind=None,
        invoice_id=invoice_id,
        message=f"Sweep {last_failed_sweep.operation_id} of {invoice_id} failed, but the invoice is not "
        "sweep_blocked and nothing swept its deposit since: the failure looks unapplied; check the "
        f"deposit, then `sweep-invoice {invoice_id}`.",
        columns={
            "invoice": invoice_id,
            "problem": "failed sweep, not blocked",
            "detail": f"sweep {last_failed_sweep.operation_id} failed",
        },
    )


def find_underpaid_refunded_in_full_finding(
    invoice_id: str,
    *,
    chain: EvmChainConfiguration,
    state: InvoiceSettlementState | None,
    operations: list[OperationValue],
) -> ReconciliationFinding | None:
    """An underpaid invoice whose final refunds in its own asset moved at least what it received: the refund's cancel
    looks missed. None otherwise."""
    if state is None or state.status != InvoiceStatus.UNDERPAID.value or state.received_base_units <= 0:
        return None
    refunded_base_units = sum(
        operation.moved_base_units if operation.moved_base_units is not None else operation.amount_base_units or 0
        for operation in operations
        if operation.kind == OperationKind.REFUND
        and operation.status == OperationStatus.FINAL
        and operation.asset_symbol == state.asset_symbol
    )
    if refunded_base_units < state.received_base_units:
        return None
    asset = chain.find_asset(state.asset_symbol or "")
    decimals = asset.decimals if asset else 0
    return ReconciliationFinding(
        problem_key=f"underpaid_refunded:{invoice_id}",
        alert_kind=None,
        invoice_id=invoice_id,
        message=f"{invoice_id} is still underpaid, but its final refunds moved "
        f"{format_base_units(refunded_base_units, decimals=decimals)} of the "
        f"{format_base_units(state.received_base_units, decimals=decimals)} {state.asset_symbol} it "
        "received: the refund's cancel looks missed.",
        columns={
            "invoice": invoice_id,
            "problem": "underpaid, refunded in full",
            "detail": f"{format_base_units(refunded_base_units, decimals=decimals)} of "
            f"{format_base_units(state.received_base_units, decimals=decimals)} {state.asset_symbol}",
        },
    )


class ReconciliationService:
    def __init__(
        self,
        *,
        wiring: InvoiceWiring,
        heartbeat_service: WatcherHeartbeatService,
        cursor_repository: ChainCursorRepository,
        invoice_reconciliation_service: InvoiceReconciliationService,
        settlement_service: SettlementService,
        account_event_service: AccountEventService,
        wal_archiving_check_service: WalArchivingCheckService,
        static_deposit_readings_service: StaticDepositReadingsService,
    ) -> None:
        self.wiring = wiring
        self.heartbeat_service = heartbeat_service
        self.cursor_repository = cursor_repository
        self.invoice_reconciliation_service = invoice_reconciliation_service
        self.settlement_service = settlement_service
        self.account_event_service = account_event_service
        self.wal_archiving_check_service = wal_archiving_check_service
        self.static_deposit_readings_service = static_deposit_readings_service

    @classmethod
    def on_session(cls, session: AsyncSession, *, redis: Redis, wiring: InvoiceWiring) -> Self:
        return cls(
            wiring=wiring,
            heartbeat_service=WatcherHeartbeatService.on_redis(redis),
            cursor_repository=ChainCursorRepository(session=session),
            invoice_reconciliation_service=InvoiceReconciliationService.on_session(session),
            settlement_service=SettlementService.on_session(
                session, operator_key_addresses=wiring.operator_key_addresses
            ),
            account_event_service=AccountEventService.on_session(session),
            wal_archiving_check_service=WalArchivingCheckService.on_session(session),
            static_deposit_readings_service=StaticDepositReadingsService.on_session(
                session, redis=redis, wiring=wiring
            ),
        )

    async def run_every_check(self, chain: ChainConfiguration) -> list[ReconciliationCheckResult]:
        """``reconcile``: every check, the deposit balance read over every deposit included. A Monero chain has the
        price feeds and the database here; ``monero_reconciliation.py`` checks its scanner (PLAN.md section 14)."""
        if not isinstance(chain, EvmChainConfiguration):
            return [await self.check_price_feeds(chain), await self.wal_archiving_check_service.check_wal_archiving()]
        return [
            await self.check_watcher_progress(chain, include_heartbeat=True),
            await self.check_price_feeds(chain),
            await self.check_outgoing_operations(chain),
            await self.check_operator_float(chain),
            await self.check_unapplied_outcomes(chain),
            await self.check_settlement_consistency(chain),
            await self.check_sender_less_stray_payments(chain),
            await self.check_deferred_automatic_refunds(chain),
            await self.wal_archiving_check_service.check_wal_archiving(),
            await self.check_deposit_balances(chain),
            await check_static_deposit_balances(
                chain,
                rpc_client=self.wiring.rpc_clients[chain.key],
                static_deposit_readings_service=self.static_deposit_readings_service,
                chain_unreadable_finding=self.chain_unreadable_finding(chain, check_name="static_deposit_balances"),
            ),
        ]

    async def run_alert_checks(self, chain: EvmChainConfiguration) -> list[ReconciliationCheckResult]:
        """The watcher's: a few reads each, nothing that grows with the deposit count. Its own heartbeat is left out
        (a watcher that runs this just ticked; the container healthcheck covers one that stopped)."""
        return [
            await self.check_watcher_progress(chain, include_heartbeat=False),
            await self.check_price_feeds(chain),
            await self.check_outgoing_operations(chain),
            await self.check_operator_float(chain),
            await self.wal_archiving_check_service.check_wal_archiving(),
        ]

    # Watcher progress

    async def check_watcher_progress(
        self, chain: EvmChainConfiguration, *, include_heartbeat: bool
    ) -> ReconciliationCheckResult:
        findings: list[ReconciliationFinding] = []
        summary_parts: list[str] = []
        try:
            block_heads = await self.wiring.rpc_clients[chain.key].call_with_failover(
                lambda endpoint: endpoint.read_block_heads()
            )
        except EvmRpcUnavailableError:
            block_heads = None
            findings.append(self.chain_unreadable_finding(chain, check_name="watcher_progress"))
        applied_block_number = (await self.cursor_repository.read_cursors(chain.key)).get(CursorStream.FINALIZED_HEAD)
        if block_heads is not None:
            finalized_number = block_heads.finalized.number
            if applied_block_number is None:
                summary_parts.append(f"finalized head {finalized_number}, no reading applied yet")
            else:
                blocks_behind = finalized_number - applied_block_number
                summary_parts.append(
                    f"finalized head {finalized_number}, applied {applied_block_number} ({blocks_behind} behind)"
                )
                maximum_seconds_behind = max(WATCHER_BEHIND_MAXIMUM_SECONDS, 2 * chain.expected_finality_seconds)
                if blocks_behind * chain.seconds_per_block > maximum_seconds_behind:
                    findings.append(
                        ReconciliationFinding(
                            problem_key=f"watcher_behind:{chain.key}",
                            alert_kind="watcher_behind",
                            invoice_id=None,
                            message=f"The {chain.name} watcher is {blocks_behind} blocks behind the finalized head.",
                            columns={
                                "chain": chain.key,
                                "applied": str(applied_block_number),
                                "finalized": str(finalized_number),
                                "behind": str(blocks_behind),
                            },
                        )
                    )
        if include_heartbeat:
            try:
                seconds_since_tick = await self.heartbeat_service.find_seconds_since_last_tick(chain.key)
            except RedisError:
                seconds_since_tick = None
            summary_parts.append(
                "no completed tick in the heartbeat"
                if seconds_since_tick is None
                else f"last completed tick {describe_seconds(seconds_since_tick)} ago"
            )
            if seconds_since_tick is None or seconds_since_tick > WATCHER_HEARTBEAT_MAXIMUM_AGE_SECONDS:
                findings.append(
                    ReconciliationFinding(
                        problem_key=f"watcher_heartbeat:{chain.key}",
                        alert_kind=None,
                        invoice_id=None,
                        message=f"The {chain.name} watcher has not completed a tick for over "
                        f"{WATCHER_HEARTBEAT_MAXIMUM_AGE_SECONDS} s.",
                        columns={
                            "chain": chain.key,
                            "last tick": "none"
                            if seconds_since_tick is None
                            else f"{describe_seconds(seconds_since_tick)} ago",
                        },
                    )
                )
        return ReconciliationCheckResult(
            check_name="watcher_progress", summary="; ".join(summary_parts) or "chain unreadable", findings=findings
        )

    def chain_unreadable_finding(self, chain: EvmChainConfiguration, *, check_name: str) -> ReconciliationFinding:
        return ReconciliationFinding(
            problem_key=f"chain_unreadable:{chain.key}:{check_name}",
            alert_kind=None,
            invoice_id=None,
            message=f"No {chain.name} RPC endpoint answered; this check read nothing.",
            columns={"chain": chain.key, "check": check_name, "state": "no endpoint answered"},
        )

    # Price feeds

    async def check_price_feeds(self, chain: ChainConfiguration) -> ReconciliationCheckResult:
        return await check_price_feeds(chain, chainlink_client=self.wiring.chainlink_clients[chain.key])

    # Outgoing operations

    async def check_outgoing_operations(self, chain: EvmChainConfiguration) -> ReconciliationCheckResult:
        now = datetime.now(UTC)
        # A mined operation waits for its block to be finalized, so the chain's finality is added to the normal age.
        stuck_after = STUCK_OPERATION_AGE + timedelta(seconds=chain.expected_finality_seconds)
        stuck_operations = await self.settlement_service.list_unresolved_operations_created_before(
            chain.key, created_before=now - stuck_after
        )
        refunds_needing_owner = [
            refund
            for refund in await self.settlement_service.list_refunds_needing_owner()
            if refund.chain_key == chain.key
        ]
        sweep_blocked_invoices = await self.invoice_reconciliation_service.list_sweep_blocked_invoices(chain.key)
        (
            static_deposit_findings,
            stuck_static_deposit_sweep_count,
            sweep_blocked_static_deposit_count,
        ) = await find_static_deposit_sweep_findings(
            chain,
            settlement_service=self.settlement_service,
            static_deposit_readings_service=self.static_deposit_readings_service,
            now=now,
            stuck_after=stuck_after,
        )
        findings: list[ReconciliationFinding] = []
        for operation in stuck_operations:
            minutes = int((now - operation.created_at).total_seconds() // 60)
            findings.append(
                ReconciliationFinding(
                    problem_key=f"operation_stuck:{operation.operation_id}",
                    alert_kind="operation_stuck",
                    invoice_id=operation.invoice_id,
                    message=f"{operation.kind.value.capitalize()} {operation.operation_id} of {operation.invoice_id} "
                    f"is still {operation.status.value} after {minutes} minutes.",
                    columns={
                        "operation": str(operation.operation_id),
                        "kind": operation.kind.value,
                        "invoice": operation.invoice_id,
                        "status": operation.status.value,
                        "age": f"{minutes} min",
                    },
                )
            )
        for refund in refunds_needing_owner:
            asset = chain.find_asset(refund.asset_symbol)
            amount_text = format_base_units(refund.amount_base_units, decimals=asset.decimals if asset else 0)
            waiting = refund.status == OperationStatus.WAITING_FOR_FLOAT.value
            findings.append(
                ReconciliationFinding(
                    problem_key=f"refund_{refund.status}:{refund.operation_id}",
                    # A refund waiting for float is alerted per asset by the float check.
                    alert_kind=None if waiting else "refund_needs_owner",
                    invoice_id=refund.invoice_id,
                    message=f"Refund {refund.operation_id} of {refund.invoice_id} ({amount_text} "
                    f"{refund.asset_symbol}) "
                    + (
                        "waits for float."
                        if waiting
                        else "ended unproven: check the chain, then `refunds mark-paid` or `refunds mark-unpaid`."
                    ),
                    columns={
                        "operation": str(refund.operation_id),
                        "kind": "refund",
                        "invoice": refund.invoice_id,
                        "status": refund.status,
                        "amount": f"{amount_text} {refund.asset_symbol}",
                        "recipient": refund.recipient_address,
                    },
                )
            )
        findings.extend(
            ReconciliationFinding(
                problem_key=f"sweep_failed:{invoice.invoice_id}",
                alert_kind="sweep_failed",
                invoice_id=invoice.invoice_id,
                message=f"A sweep of {invoice.invoice_id} failed; its money stays in the deposit until "
                f"`sweep-invoice {invoice.invoice_id}` (a token frozen by its issuer: add `--skip-token <SYMBOL>`).",
                columns={"operation": "", "kind": "sweep", "invoice": invoice.invoice_id, "status": "sweep_blocked"},
            )
            for invoice in sweep_blocked_invoices
        )
        findings.extend(static_deposit_findings)
        return ReconciliationCheckResult(
            check_name="outgoing_operations",
            summary=f"{len(stuck_operations) + stuck_static_deposit_sweep_count} unresolved past "
            f"{int(stuck_after.total_seconds() // 60)} min, {len(refunds_needing_owner)} refunds for the owner, "
            f"{len(sweep_blocked_invoices) + sweep_blocked_static_deposit_count} sweeps blocked",
            findings=findings,
        )

    # Operator float

    async def check_operator_float(self, chain: EvmChainConfiguration) -> ReconciliationCheckResult:
        # Refunds are paid by the refund key alone; the sweepers' gas is the operator key check's.
        key_addresses = self.wiring.operator_key_addresses.get(chain.key)
        operator_address = key_addresses.refund_key_address if key_addresses is not None else None
        needed_by_symbol: dict[str, int] = {}
        waiting_count_by_symbol: dict[str, int] = {}
        for refund in await self.settlement_service.list_refunds_needing_owner():
            if refund.chain_key != chain.key or refund.status != OperationStatus.WAITING_FOR_FLOAT.value:
                continue
            needed_by_symbol[refund.asset_symbol] = needed_by_symbol.get(refund.asset_symbol, 0) + (
                refund.amount_base_units
            )
            waiting_count_by_symbol[refund.asset_symbol] = waiting_count_by_symbol.get(refund.asset_symbol, 0) + 1
        if operator_address is None:
            return ReconciliationCheckResult(
                check_name="operator_float",
                summary="no operator key in this process's settings; float not read",
                findings=[
                    ReconciliationFinding(
                        problem_key=f"float_unknown:{chain.key}:{symbol}",
                        alert_kind=None,
                        invoice_id=None,
                        message=f"{count} refunds wait for float in {symbol}.",
                        columns={"asset": symbol, "waiting refunds": str(count)},
                    )
                    for symbol, count in waiting_count_by_symbol.items()
                ],
            )
        assets = [chain.native_asset, *(chain.find_asset(symbol) for symbol in sorted(needed_by_symbol))]
        read_assets = list({asset.symbol: asset for asset in assets if asset is not None}.values())
        balance_calls = [balance_call_of(asset, holder_address=operator_address) for asset in read_assets]

        async def read_operator_balances(endpoint: EvmRpcEndpoint) -> list[Multicall3Result]:
            return await endpoint.aggregate3(balance_calls, block_hash=None)

        try:
            results = await self.wiring.rpc_clients[chain.key].call_with_failover(read_operator_balances)
        except (EvmRpcUnavailableError, EvmExecutionRevertedError):
            return ReconciliationCheckResult(
                check_name="operator_float",
                summary=f"operator {operator_address}: balances not readable",
                findings=[self.chain_unreadable_finding(chain, check_name="operator_float")],
            )
        held_by_symbol = {asset.symbol: balance_of(result) for asset, result in zip(read_assets, results, strict=True)}
        native = chain.native_asset
        native_held = held_by_symbol.get(native.symbol)
        findings: list[ReconciliationFinding] = []
        for symbol, needed in sorted(needed_by_symbol.items()):
            asset = chain.find_asset(symbol)
            decimals = asset.decimals if asset is not None else 0
            held = held_by_symbol.get(symbol)
            if held is not None and held >= needed:
                continue
            needed_text = f"{format_base_units(needed, decimals=decimals)} {symbol}"
            held_text = "unreadable" if held is None else f"{format_base_units(held, decimals=decimals)} {symbol}"
            findings.append(
                ReconciliationFinding(
                    problem_key=f"float_low:{chain.key}:{symbol}",
                    alert_kind="float_low",
                    invoice_id=None,
                    message=f"{waiting_count_by_symbol[symbol]} refunds on {chain.name} wait for float: they need "
                    f"{needed_text}, the operator holds {held_text}. Top up the operator wallet.",
                    columns={
                        "asset": symbol,
                        "waiting refunds": str(waiting_count_by_symbol[symbol]),
                        "needed": needed_text,
                        "held": held_text,
                    },
                )
            )
        native_text = (
            "unreadable"
            if native_held is None
            else f"{format_base_units(native_held, decimals=native.decimals)} {native.symbol}"
        )
        return ReconciliationCheckResult(
            check_name="operator_float",
            summary=f"operator {operator_address} holds {native_text}; "
            f"{sum(waiting_count_by_symbol.values())} refunds waiting for float",
            findings=findings,
        )

    # Outcomes committed on an operation but never applied to its invoice

    async def check_unapplied_outcomes(self, chain: EvmChainConfiguration) -> ReconciliationCheckResult:
        operation_counts = [
            count
            for count in await self.settlement_service.count_operations_by_invoice(
                chain.key, statuses=sorted({status for _, status in OUTCOME_EVENT_TYPES})
            )
            if (count.kind, count.status) in OUTCOME_EVENT_TYPES
        ]
        compared_invoice_ids = sorted({count.invoice_id for count in operation_counts})
        # An invoice with no event at all has no event history to compare (production's first invoices, 2026-10-09,
        # predate the kept events); a lost outcome shows on an invoice whose other events are there.
        invoice_ids_with_events = await self.account_event_service.list_invoice_ids_with_events(compared_invoice_ids)
        event_counts = await self.account_event_service.count_invoice_events(
            compared_invoice_ids,
            event_types=[event_type.value for event_type in OUTCOME_EVENT_TYPES.values()],
        )
        findings: list[ReconciliationFinding] = []
        for count in sorted(operation_counts, key=lambda count: (count.invoice_id, count.kind, count.status)):
            event_type = OUTCOME_EVENT_TYPES[count.kind, count.status].value
            recorded = event_counts.get((count.invoice_id, event_type), 0)
            if recorded >= count.count or count.invoice_id not in invoice_ids_with_events:
                continue
            several_failed_sweeps = count.kind == OperationKind.SWEEP and count.count > 1
            findings.append(
                ReconciliationFinding(
                    problem_key=f"outcome_unapplied:{count.invoice_id}:{event_type}",
                    alert_kind=None,
                    invoice_id=count.invoice_id,
                    message=f"{count.invoice_id} has {count.count} {count.kind.value} {count.status.value} but "
                    f"{recorded} `{event_type}` events: the outcome looks unapplied to the invoice."
                    + (
                        " Several failed sweeps on one invoice also hint at a lost block."
                        if several_failed_sweeps
                        else ""
                    ),
                    columns={
                        "invoice": count.invoice_id,
                        "operations": f"{count.count} {count.kind.value} {count.status.value}",
                        "events": f"{recorded} {event_type}",
                    },
                )
            )
        return ReconciliationCheckResult(
            check_name="unapplied_outcomes",
            summary=f"{sum(count.count for count in operation_counts)} resolved operations on "
            f"{len(compared_invoice_ids)} invoices; {len(compared_invoice_ids) - len(invoice_ids_with_events)} "
            "with no event history skipped",
            findings=findings,
        )

    # Settlement consistency

    async def check_settlement_consistency(self, chain: EvmChainConfiguration) -> ReconciliationCheckResult:
        counts = await self.settlement_service.count_operations_by_invoice(
            chain.key, statuses=[OperationStatus.FAILED, OperationStatus.FINAL]
        )
        invoice_ids_with_failed_sweep = sorted(
            {
                count.invoice_id
                for count in counts
                if count.kind == OperationKind.SWEEP and count.status == OperationStatus.FAILED
            }
        )
        invoice_ids_with_final_refund = sorted(
            {
                count.invoice_id
                for count in counts
                if count.kind == OperationKind.REFUND and count.status == OperationStatus.FINAL
            }
        )
        invoice_ids = sorted({*invoice_ids_with_failed_sweep, *invoice_ids_with_final_refund})
        states_by_invoice = {
            state.invoice_id: state
            for state in await self.invoice_reconciliation_service.list_settlement_states(invoice_ids)
        }
        operations_by_invoice: dict[str, list[OperationValue]] = {}
        for operation in await self.settlement_service.list_operations_for_invoices(invoice_ids):
            operations_by_invoice.setdefault(operation.invoice_id, []).append(operation)
        sweep_transactions_by_deposit = await self.invoice_reconciliation_service.list_deposit_sweep_transaction_hashes(
            chain.key,
            deposit_addresses=[
                states_by_invoice[invoice_id].deposit_address
                for invoice_id in invoice_ids_with_failed_sweep
                if invoice_id in states_by_invoice
            ],
        )
        findings: list[ReconciliationFinding] = []
        for invoice_id in invoice_ids_with_failed_sweep:
            failed_sweep_finding = find_unapplied_failed_sweep_finding(
                invoice_id,
                state=states_by_invoice.get(invoice_id),
                operations=operations_by_invoice.get(invoice_id, []),
                sweep_transactions_by_deposit=sweep_transactions_by_deposit,
            )
            if failed_sweep_finding is not None:
                findings.append(failed_sweep_finding)
        for invoice_id in invoice_ids_with_final_refund:
            underpaid_refunded_finding = find_underpaid_refunded_in_full_finding(
                invoice_id,
                chain=chain,
                state=states_by_invoice.get(invoice_id),
                operations=operations_by_invoice.get(invoice_id, []),
            )
            if underpaid_refunded_finding is not None:
                findings.append(underpaid_refunded_finding)
        return ReconciliationCheckResult(
            check_name="settlement_consistency",
            summary=f"{len(invoice_ids_with_failed_sweep)} invoices with a failed sweep, "
            f"{len(invoice_ids_with_final_refund)} with a final refund",
            findings=findings,
        )

    # Stray money with no sender

    async def check_sender_less_stray_payments(self, chain: EvmChainConfiguration) -> ReconciliationCheckResult:
        rows = await self.invoice_reconciliation_service.list_sender_less_stray_payments(chain.key)
        findings: list[ReconciliationFinding] = []
        for row in rows:
            asset = chain.find_asset(row.asset_symbol)
            amount_text = f"{format_base_units(row.amount_base_units, decimals=asset.decimals if asset else 0)} "
            amount_text += row.asset_symbol
            findings.append(
                ReconciliationFinding(
                    problem_key=f"stray_without_sender:{row.stray_payment_id}",
                    alert_kind=None,
                    invoice_id=row.invoice_id,
                    message=f"{amount_text} reached {row.invoice_id} with no sender (block {row.block_number}): "
                    "settle it with the payer, or prove its transaction with `record-stray-payment-from-transaction`.",
                    columns={
                        "row": str(row.stray_payment_id),
                        "invoice": row.invoice_id,
                        "amount": amount_text,
                        "block": str(row.block_number),
                        "final": "yes" if row.final else "no",
                    },
                )
            )
        return ReconciliationCheckResult(
            check_name="stray_without_sender", summary=f"{len(rows)} stray rows with no sender", findings=findings
        )

    # Automatic refunds waiting for a price

    async def check_deferred_automatic_refunds(self, chain: EvmChainConfiguration) -> ReconciliationCheckResult:
        now = datetime.now(UTC)
        deferred = await self.invoice_reconciliation_service.list_automatic_refunds_deferred_before(
            chain.key, deferred_before=now - DEFERRED_REFUND_MAXIMUM_AGE
        )
        findings = [
            ReconciliationFinding(
                problem_key=f"refund_deferred:{refund.invoice_id}",
                alert_kind=None,
                invoice_id=refund.invoice_id,
                message=f"{refund.invoice_id}'s automatic refund has waited for a price for "
                f"{describe_seconds((now - refund.deferred_since).total_seconds())}.",
                columns={
                    "invoice": refund.invoice_id,
                    "deferred since": refund.deferred_since.isoformat(),
                    "waited": describe_seconds((now - refund.deferred_since).total_seconds()),
                },
            )
            for refund in deferred
        ]
        return ReconciliationCheckResult(
            check_name="deferred_automatic_refunds",
            summary=f"{len(deferred)} deferred longer than {int(DEFERRED_REFUND_MAXIMUM_AGE.total_seconds() // 60)} "
            "min",
            findings=findings,
        )

    # Deposit balances

    async def check_deposit_balances(self, chain: EvmChainConfiguration) -> ReconciliationCheckResult:
        deposits = []
        after_invoice_id: str | None = None
        while True:
            page = await self.invoice_reconciliation_service.list_deposits_after(
                chain.key, after_invoice_id=after_invoice_id, limit=DEPOSITS_PER_BALANCE_PAGE
            )
            deposits.extend(page)
            if len(page) < DEPOSITS_PER_BALANCE_PAGE:
                break
            after_invoice_id = page[-1].invoice_id
        calls = [
            balance_call_of(asset, holder_address=deposit.deposit_address)
            for deposit in deposits
            for asset in chain.assets
        ]

        async def read_finalized_balances(endpoint: EvmRpcEndpoint) -> tuple[BlockHeads, list[Multicall3Result]]:
            # Heads and balances from one endpoint: another may not know the pinned block hash.
            block_heads = await endpoint.read_block_heads()
            return block_heads, await endpoint.aggregate3(calls, block_hash=block_heads.finalized.hash)

        try:
            block_heads, results = await self.wiring.rpc_clients[chain.key].call_with_failover(read_finalized_balances)
        except (EvmRpcUnavailableError, EvmExecutionRevertedError):
            return ReconciliationCheckResult(
                check_name="deposit_balances",
                summary=f"{len(deposits)} deposits, balances not readable",
                findings=[self.chain_unreadable_finding(chain, check_name="deposit_balances")],
            )
        held_by_invoice: dict[str, dict[str, int]] = {}
        unreadable_invoice_ids: list[str] = []
        asset_count = len(chain.assets)
        for index, deposit in enumerate(deposits):
            for asset, result in zip(
                chain.assets, results[index * asset_count : (index + 1) * asset_count], strict=True
            ):
                balance = balance_of(result)
                if balance is None:
                    unreadable_invoice_ids.append(deposit.invoice_id)
                elif balance > 0 and balance >= asset.minimum_sweep_base_units:
                    held_by_invoice.setdefault(deposit.invoice_id, {})[asset.symbol] = balance
        invoice_ids = sorted(held_by_invoice)
        states = await self.invoice_reconciliation_service.list_settlement_states(invoice_ids)
        swept_invoice_ids = await self.settlement_service.list_invoice_ids_with_active_sweep(invoice_ids)
        findings: list[ReconciliationFinding] = []
        for state in states:
            if not state.payment_window_over or state.invoice_id in swept_invoice_ids:
                continue
            held_text = describe_holdings(chain, held_by_symbol=held_by_invoice[state.invoice_id])
            findings.append(
                ReconciliationFinding(
                    problem_key=f"deposit_unswept:{state.invoice_id}",
                    alert_kind=None,
                    invoice_id=state.invoice_id,
                    message=f"{state.invoice_id} ({state.status}) holds {held_text} at the finalized block and no "
                    f"sweep is under way: `sweep-invoice {state.invoice_id}`.",
                    columns={
                        "invoice": state.invoice_id,
                        "status": state.status,
                        "holds": held_text,
                        "sweep blocked": "yes" if state.sweep_blocked else "no",
                    },
                )
            )
        findings.extend(
            ReconciliationFinding(
                problem_key=f"deposit_unreadable:{invoice_id}",
                alert_kind=None,
                invoice_id=invoice_id,
                message=f"A balance of {invoice_id}'s deposit could not be read.",
                columns={"invoice": invoice_id, "status": "", "holds": "unreadable", "sweep blocked": ""},
            )
            for invoice_id in sorted(set(unreadable_invoice_ids))
        )
        return ReconciliationCheckResult(
            check_name="deposit_balances",
            summary=f"{len(deposits)} deposits in {asset_count} assets at finalized block "
            f"{block_heads.finalized.number}; {len(invoice_ids)} hold at least a minimum sweep",
            findings=findings,
        )
