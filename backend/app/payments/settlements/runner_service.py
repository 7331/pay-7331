"""SettlementRunner: the watcher's half of settlements, one pass per chain tick (PLAN.md section 5, step 6).

A pass first reads what its steps share, batched, so its RPC work does not grow as serial calls per operation
(``pass_readings.py``): one query for the active operations and the attempts of the ``broadcast`` ones, one for the
chain's operator key states, then one JSON-RPC batch (chunks of the endpoint's ``json_rpc_batch_size``) for the
receipt of every ``broadcast`` attempt and of every ``mined`` operation whose block is final, then, only when a step
needs them, one batch for the nonce at ``latest`` of each key with a ``broadcast`` operation lacking a receipt, and
the node's priority fee suggestion once. A ``mined`` operation whose block is above the finalized head reads nothing
at all, not even its row. A failing batch is read again one request at a time, so a failing read fails only the
steps that need it.

Each operation is signed by the operator key its ``operator_address`` names; which key signs, and what a frozen
key's operations do, is in ``operator_keys.py``'s docstring (2026-10-13).

Each active operation then moves at most one step per pass, in its own transaction, on its row read again (one whose
status changed since the pass's reads waits for the next pass):

- ``pending`` (and ``waiting_for_float``): estimate gas first (a sweep or key operation that reverts fails here,
  before a nonce is spent; a refund the float cannot cover waits), then in one transaction take the next nonce from
  the key's ``operator_nonces`` row, sign, and store the attempt (raw transaction and hash) as ``broadcast``. Only
  after that commit is the attempt sent, so a crash between the two leaves a stored attempt the next pass rebroadcasts.
  The reads that decide money or a nonce stay this operation's own and fresh: the held tokens, the float, the estimate
  and the key's pending nonce count right before the nonce is taken. A pending count above the row's ``next_nonce``
  means the key was used outside this system: it is frozen in that transaction and nothing is signed.
- ``broadcast``: the pass's receipts of every attempt. One receipt means ``mined`` (exactly one attempt at a nonce
  can mine). None while the key's nonce at ``latest`` has not passed ours: rebroadcast the newest attempt, and once it
  is older than ``fee_bump_after_seconds`` sign a replacement at the same nonce with both fee caps raised by at least
  12.5%, up to the chain's ``maximum_fee_per_gas_gwei``, and its gas limit estimated anew (a rollup's estimate moves
  with the parent chain's posting fee; ``attempt_replacement.py``), after reading the key's ``latest`` nonce again (the
  pass's is older). A replacement that cannot be signed anew (its key left the configuration, its treasury Safe
  changed, its call no longer estimates, its key cannot pay the new limit) rebroadcasts the newest attempt instead.
  None after the nonce passed ours: neither, since no attempt
  at a used nonce can mine; once the newest attempt is ``NONCE_TAKEN_ELSEWHERE_AFTER`` old, every endpoint (the
  tick's one again too) is asked afresh for the receipts and the operation ends unproven. The receipts are read
  before the nonce, as before, so a transaction mined between the two reads only looks like a used nonce, and the
  pass's reads can only be older than the chain: an attempt rebroadcast or bumped after it mined is refused as
  already in. An operation is never retried at a new nonce while unresolved.
- ``mined``: once its block is finalized and the pass's receipt still has its block hash, ``final`` (a successful
  receipt) or ``failed`` (reverted). What moved comes from the receipt.

An invoice operation's end (``final``, ``failed``, ``needs_owner``) and its start of ``waiting_for_float`` commit in
one transaction with their effect on the invoice and its events, through the injected ``SettlementOutcomeApplier``
(``ChainReadingsService.apply_settlement_outcomes``, a domain above this one): ``session.begin()`` commits both or
rolls both back (https://docs.sqlalchemy.org/en/20/orm/session_transaction.html), so a crash or an error between them
leaves the operation as it was and the next pass resolves it again; no outcome is lost or applied twice (2026-10-12).
A static deposit sweep's end does the same with its address (``outcome_appliers.py``). A key operation has no invoice
and no effect. Lock order: the operation row, then its invoice row, as the owner's CLI settles a refund; nothing
locks an invoice and then an operation, so the two cannot deadlock
(https://www.postgresql.org/docs/current/explicit-locking.html#LOCKING-DEADLOCKS).

Transactions, fees and gas limits per kind: ``operation_transactions.py``.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from eth_account.signers.local import LocalAccount
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.deposit_factory_api.client import DepositFactoryApiClient
from app.infrastructure.evm_rpc_api.client import EvmRpcClient, EvmRpcEndpoint
from app.infrastructure.evm_rpc_api.errors import (
    EvmExecutionRevertedError,
    EvmInsufficientFundsError,
    EvmRpcUnavailableError,
)
from app.infrastructure.evm_rpc_api.schemas import BlockHeads, TransactionReceipt
from app.payments.chains.service import ChainsService
from app.payments.chains.types import EvmChainConfiguration
from app.payments.settlements.attempt_replacement import estimate_replacement_gas_limit
from app.payments.settlements.configuration import read_e2e_fail_after_operation_resolve
from app.payments.settlements.enums import OperationKind, OperationStatus, OperatorKeyRole, OperatorKeyStatus
from app.payments.settlements.errors import (
    E2eInjectedResolveFailureError,
    PassReadUnavailableError,
    ReplacementRefusedError,
    TreasuryNotConfiguredError,
)
from app.payments.settlements.log_events import (
    FeeBumpSkippedForRebroadcast,
    OperationBroadcast,
    OperationBroadcastFailed,
    OperationFeeBumped,
    OperationMined,
    OperationReassignedToKey,
    OperationResolved,
    OperationStepFailed,
    OperationWaitingForFloat,
    OperationWaitingForUsableKey,
    OperatorGasShort,
    OperatorKeyNonceUsedElsewhere,
    ReceiptEndpointSkipped,
)
from app.payments.settlements.models import OperatorNonce, OutgoingAttempt, OutgoingOperation
from app.payments.settlements.operation_transactions import (
    NATIVE_TRANSFER_GAS,
    OperationTransactionBuilder,
    SettlementPassReadings,
    fee_caps_of,
    gas_limit_of,
    key_covers_value_and_gas,
    key_operation_refusal_of,
    replacement_fee_caps_of,
    sign_operation_attempt,
    stored_call_of,
    waits_for_finality,
)
from app.payments.settlements.operator_key_service import OperatorKeyService
from app.payments.settlements.operator_keys import OperatorKeyRing, choose_sweep_key_address
from app.payments.settlements.outcome_appliers import (
    SettlementOutcomeApplier,
    StaticDepositSweepOutcomeApplier,
    static_deposit_sweep_outcome_of,
)
from app.payments.settlements.pass_readings import SIGNING_STATUSES, read_settlement_pass_readings
from app.payments.settlements.refund_float import float_covers_refund
from app.payments.settlements.repository import SettlementRepository
from app.payments.settlements.service import moved_by_refund, operation_value_of
from app.payments.settlements.types import (
    OPERATOR_GAS_SHORT_FAILURE_REASON,
    OPERATOR_KEY_FROZEN_FAILURE_REASON,
    SettlementOutcome,
)
from app.shared import logging as log
from app.shared.logging import LogContext, bound_log_context

logger = log.get_logger(__name__)

# No receipt for any attempt while the key's nonce at `latest` moved past ours: something else used the nonce.
NONCE_TAKEN_ELSEWHERE_AFTER = timedelta(minutes=5)

type SessionFactory = Callable[[], AsyncSession]


class SettlementRunner:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        chains_service: ChainsService,
        operator_key_rings: dict[str, OperatorKeyRing],
        factory_clients: dict[str, DepositFactoryApiClient],
        rpc_clients: dict[str, EvmRpcClient],
        apply_settlement_outcome: SettlementOutcomeApplier,
        apply_static_deposit_sweep_outcome: StaticDepositSweepOutcomeApplier,
    ) -> None:
        self.session_factory = session_factory
        self.apply_settlement_outcome = apply_settlement_outcome
        self.apply_static_deposit_sweep_outcome = apply_static_deposit_sweep_outcome
        self.e2e_failing_resolutions = {
            chain.key: read_e2e_fail_after_operation_resolve(chain.key) for chain in chains_service.evm_chains
        }
        self.chains_service = chains_service
        self.operator_key_rings = operator_key_rings
        self.transaction_builder = OperationTransactionBuilder(
            factory_clients=factory_clients, operator_key_rings=operator_key_rings
        )
        # Every endpoint per chain: an unproven end is decided only after each one is asked for the receipts.
        self.rpc_clients = rpc_clients
        # Passes in a row whose step failed, per active operation (in memory: a restart starts the count again). Since
        # an outcome commits with its effect, an effect that keeps failing leaves the operation unresolved and only
        # this count shows it; the watcher's operator alerts read it (2026-10-12).
        self.consecutive_step_failures_by_operation: dict[int, tuple[str, int]] = {}

    async def advance_operations(self, chain_key: str, *, endpoint: EvmRpcEndpoint, block_heads: BlockHeads) -> None:
        """One step for every active operation of the chain; each resolution is applied to its invoice as it commits.
        The pass's shared reads come first (``pass_readings.py``); a read that fails fails only the steps needing it."""
        async with self.session_factory() as session:
            repository = SettlementRepository(session=session)
            operations = await repository.list_active_operations(chain_key)
            broadcast_operation_ids = [
                operation.id for operation in operations if operation.status == OperationStatus.BROADCAST
            ]
            attempts = await repository.list_attempts_of_operations(broadcast_operation_ids)
            key_states = await OperatorKeyService(repository=repository).list_key_states(chain_key)
        attempts_by_operation_id: dict[int, list[OutgoingAttempt]] = {}
        for attempt in attempts:
            attempts_by_operation_id.setdefault(attempt.operation_id, []).append(attempt)
        chain = self.chains_service.get_evm_chain(chain_key)
        active_operation_ids = {operation.id for operation in operations}
        self.consecutive_step_failures_by_operation = {
            operation_id: failures
            for operation_id, failures in self.consecutive_step_failures_by_operation.items()
            if failures[0] != chain_key or operation_id in active_operation_ids
        }
        operations_to_step: list[OutgoingOperation] = []
        for operation in operations:
            if waits_for_finality(operation, finalized_block_number=block_heads.finalized.number):
                self.consecutive_step_failures_by_operation.pop(operation.id, None)
            else:
                operations_to_step.append(operation)
        readings = await read_settlement_pass_readings(
            operations_to_step,
            chain=chain,
            endpoint=endpoint,
            block_heads=block_heads,
            attempts_by_operation_id=attempts_by_operation_id,
            key_status_by_address={key_state.operator_address: key_state.status for key_state in key_states},
        )
        for pass_operation in operations_to_step:
            operation_id = pass_operation.id
            with bound_log_context(LogContext(operation_id=operation_id)):
                try:
                    await self.advance_operation(
                        pass_operation,
                        chain=chain,
                        endpoint=endpoint,
                        readings=readings,
                        attempts=attempts_by_operation_id.get(operation_id, []),
                    )
                except Exception:
                    # One operation failing (an endpoint error, a bug, applying its outcome) must not stall the ones
                    # after it (2026-10-09); its transaction rolled back whole, so it is retried next pass.
                    log.exception(logger, event=OperationStepFailed(operation_id=operation_id))
                    _, failures = self.consecutive_step_failures_by_operation.get(operation_id, (chain_key, 0))
                    self.consecutive_step_failures_by_operation[operation_id] = (chain_key, failures + 1)
                    continue
                self.consecutive_step_failures_by_operation.pop(operation_id, None)

    def find_operations_failing_repeatedly(self, chain_key: str, *, minimum_failures: int) -> dict[int, int]:
        """Operation id to failed passes in a row, for those of the chain at ``minimum_failures`` or more."""
        return {
            operation_id: failures
            for operation_id, (operation_chain_key, failures) in self.consecutive_step_failures_by_operation.items()
            if operation_chain_key == chain_key and failures >= minimum_failures
        }

    async def advance_operation(
        self,
        pass_operation: OutgoingOperation,
        *,
        chain: EvmChainConfiguration,
        endpoint: EvmRpcEndpoint,
        readings: SettlementPassReadings,
        attempts: list[OutgoingAttempt],
    ) -> None:
        """One step on the operation's row read again. A status changed since the pass's reads (the owner's CLI
        settled it meanwhile) has nothing read for it: the next pass steps it."""
        async with self.session_factory() as session:
            operation = await SettlementRepository(session=session).get_operation(pass_operation.id)
        if operation.status != pass_operation.status:
            return
        match operation.status:
            case OperationStatus.PENDING | OperationStatus.WAITING_FOR_FLOAT:
                await self.send_first_attempt(operation, chain=chain, endpoint=endpoint, readings=readings)
            case OperationStatus.BROADCAST:
                await self.reconcile_broadcast(
                    operation, attempts=attempts, chain=chain, endpoint=endpoint, readings=readings
                )
            case OperationStatus.MINED:
                await self.finalize_mined(operation, readings=readings)
            case OperationStatus.FINAL | OperationStatus.FAILED | OperationStatus.NEEDS_OWNER:
                pass

    async def choose_signing_account(
        self, operation: OutgoingOperation, *, chain: EvmChainConfiguration, readings: SettlementPassReadings
    ) -> LocalAccount | None:
        """The key that signs this unsigned operation (module docstring); None when it waits or was just failed."""
        ring = self.operator_key_rings[chain.key]

        def usable(address: str) -> bool:
            return ring.find_account(address) is not None and readings.is_recorded_active_key(address)

        match operation.kind:
            case OperationKind.SWEEP | OperationKind.STATIC_DEPOSIT_SWEEP:
                if usable(operation.operator_address) and (
                    operation.operator_address in ring.addresses.sweep_candidate_addresses
                ):
                    return ring.find_account(operation.operator_address)
                async with self.session_factory() as session:
                    counts = await SettlementRepository(session=session).count_active_operations_by_operator(chain.key)
                chosen = choose_sweep_key_address(
                    ring.addresses,
                    unusable_addresses={address for address in ring.addresses.all_addresses if not usable(address)},
                    active_operation_counts=counts,
                )
                if chosen is not None and chosen != operation.operator_address:
                    log.emit(
                        logger,
                        event=OperationReassignedToKey(
                            operation_id=operation.id,
                            previous_operator_address=operation.operator_address,
                            operator_address=chosen,
                        ),
                    )
                if chosen is not None:
                    return ring.find_account(chosen)
            case OperationKind.REFUND | OperationKind.TREASURY_SAFE_CREATION:
                if usable(ring.addresses.refund_key_address):
                    return ring.refund_account
            case OperationKind.TOP_UP | OperationKind.TREASURY_RETURN:
                failure_reason = key_operation_refusal_of(operation, ring=ring, readings=readings)
                if failure_reason is not None:
                    await self.resolve(operation, status=OperationStatus.FAILED, failure_reason=failure_reason)
                    return None
                return ring.find_account(operation.operator_address)
        log.emit(
            logger,
            event=OperationWaitingForUsableKey(
                operation_id=operation.id, kind=operation.kind.value, operator_address=operation.operator_address
            ),
        )
        return None

    async def send_first_attempt(
        self,
        operation: OutgoingOperation,
        *,
        chain: EvmChainConfiguration,
        endpoint: EvmRpcEndpoint,
        readings: SettlementPassReadings,
    ) -> None:
        operator = await self.choose_signing_account(operation, chain=chain, readings=readings)
        if operator is None:
            return
        try:
            target_address, call_data, value = await self.transaction_builder.transaction_call_of(
                operation, chain=chain, endpoint=endpoint
            )
        except TreasuryNotConfiguredError:
            await self.resolve(operation, status=OperationStatus.FAILED, failure_reason="treasury_not_configured")
            return
        max_fee_per_gas, max_priority_fee_per_gas = fee_caps_of(chain, readings=readings)
        # Before the estimate: a node estimates a native transfer beyond the balance as an error, not a revert.
        if operation.kind == OperationKind.REFUND and not await float_covers_refund(
            session_factory=self.session_factory,
            operation=operation,
            endpoint=endpoint,
            operator_address=operator.address,
            gas_cost=NATIVE_TRANSFER_GAS * max_fee_per_gas,
        ):
            await self.mark_waiting_for_float(operation)
            return
        try:
            estimated_gas = await endpoint.estimate_gas(
                sender_address=operator.address, target_address=target_address, call_data=call_data, value=value
            )
        except EvmInsufficientFundsError:
            if operation.kind == OperationKind.REFUND:
                await self.mark_waiting_for_float(operation)
                return
            if operation.kind in {OperationKind.TOP_UP, OperationKind.TREASURY_RETURN}:
                # The key decides again next minute (and alerts when it cannot pay a top-up's gas).
                await self.resolve(
                    operation, status=OperationStatus.FAILED, failure_reason=OPERATOR_GAS_SHORT_FAILURE_REASON
                )
                return
            # The key cannot pay a sweep's gas: it waits, it does not fail (the money is safe in the deposit).
            log.emit(
                logger,
                event=OperatorGasShort(
                    operation_id=operation.id, chain_key=chain.key, operator_address=operator.address
                ),
            )
            return
        except EvmExecutionRevertedError:
            if operation.kind == OperationKind.REFUND:
                # A refund reverts in estimation when the float is short (a token transfer beyond the balance).
                await self.mark_waiting_for_float(operation)
                return
            # A sweep that cannot go through, a top-up beyond the allowance or the Safe's balance: no nonce spent.
            await self.resolve(
                operation, status=OperationStatus.FAILED, failure_reason=f"{operation.kind.value}_reverts"
            )
            return
        gas_limit = gas_limit_of(operation, chain=chain, call_data=call_data, estimated_gas=estimated_gas)
        if (
            operation.kind == OperationKind.TREASURY_RETURN
            and operation.token_address is None
            and not await key_covers_value_and_gas(
                endpoint,
                operator_address=operator.address,
                value=value,
                gas_cost=gas_limit * chain.maximum_fee_per_gas_wei,
            )
        ):
            # The reserve its amount left no longer pays this gas limit at the fee cap (the parent chain's posting fee
            # rose since it was decided): no nonce is spent, and the next look decides the amount anew.
            await self.resolve(
                operation, status=OperationStatus.FAILED, failure_reason="treasury_return_gas_above_reserve"
            )
            return
        if operation.kind == OperationKind.REFUND and not await float_covers_refund(
            session_factory=self.session_factory,
            operation=operation,
            endpoint=endpoint,
            operator_address=operator.address,
            gas_cost=gas_limit * max_fee_per_gas,
        ):
            await self.mark_waiting_for_float(operation)
            return
        # Read here, not once a pass: a nonce the key used elsewhere since the pass began must not be taken.
        chain_nonce = await endpoint.read_transaction_count(operator.address, block_tag="pending")
        async with self.session_factory() as session, session.begin():
            repository = SettlementRepository(session=session)
            locked = await repository.get_operation(operation.id, lock=True)
            if locked.status not in SIGNING_STATUSES:
                return
            nonce_row = await repository.lock_operator_nonce(chain.key, operator_address=operator.address)
            if nonce_row is None:
                nonce_row = await repository.insert_operator_nonce(
                    OperatorNonce(chain_key=chain.key, operator_address=operator.address, next_nonce=chain_nonce)
                )
            if chain_nonce > nonce_row.next_nonce and readings.key_status_of(operator.address) == (
                OperatorKeyStatus.RETIRED
            ):
                # A retired key the owner emptied by hand: adopted, since all it still signs is a return to the Safe.
                nonce_row.next_nonce = chain_nonce
            if chain_nonce > nonce_row.next_nonce:
                # The key signed nonces this allocator never handed out: used outside this system (2026-10-13).
                await self.freeze_key_used_elsewhere(
                    repository,
                    chain=chain,
                    operator_address=operator.address,
                    chain_nonce=chain_nonce,
                    allocator_next_nonce=nonce_row.next_nonce,
                )
                return
            nonce = nonce_row.next_nonce
            nonce_row.next_nonce = nonce + 1
            attempt = sign_operation_attempt(
                locked,
                account=operator,
                chain=chain,
                nonce=nonce,
                target_address=target_address,
                call_data=call_data,
                value=value,
                gas_limit=gas_limit,
                max_fee_per_gas=max_fee_per_gas,
                max_priority_fee_per_gas=max_priority_fee_per_gas,
            )
            await repository.insert_attempt(attempt)
            locked.nonce = nonce
            locked.status = OperationStatus.BROADCAST
            locked.operator_address = operator.address
        log.emit(
            logger,
            event=OperationBroadcast(
                operation_id=operation.id,
                kind=operation.kind.value,
                nonce=nonce,
                transaction_hash=attempt.transaction_hash,
                max_fee_per_gas=max_fee_per_gas,
                max_priority_fee_per_gas=max_priority_fee_per_gas,
            ),
        )
        await self.broadcast_attempt(attempt, operation=operation, endpoint=endpoint)

    async def freeze_key_used_elsewhere(
        self,
        repository: SettlementRepository,
        *,
        chain: EvmChainConfiguration,
        operator_address: str,
        chain_nonce: int,
        allocator_next_nonce: int,
    ) -> None:
        log.emit(
            logger,
            event=OperatorKeyNonceUsedElsewhere(
                chain_key=chain.key,
                operator_address=operator_address,
                chain_nonce=chain_nonce,
                allocator_next_nonce=allocator_next_nonce,
            ),
        )
        ring = self.operator_key_rings[chain.key]
        await OperatorKeyService(repository=repository).freeze_key(
            chain.key,
            operator_address=operator_address,
            role=ring.addresses.role_of(operator_address) or OperatorKeyRole.SWEEPER,
            reason=f"used elsewhere: pending nonce {chain_nonce}, allocator next {allocator_next_nonce}",
        )

    async def broadcast_attempt(
        self, attempt: OutgoingAttempt, *, operation: OutgoingOperation, endpoint: EvmRpcEndpoint
    ) -> None:
        """Send a stored attempt. A failure is logged, not raised: the attempt is stored and rebroadcast next pass."""
        try:
            await endpoint.send_raw_transaction(attempt.raw_transaction)
        except EvmInsufficientFundsError:
            # A node's estimate does not check the balance for gas, so a short key is found here; one line a pass,
            # no traceback, until the key has gas (2026-10-09, fork).
            log.emit(
                logger,
                event=OperatorGasShort(
                    operation_id=operation.id,
                    chain_key=operation.chain_key,
                    operator_address=operation.operator_address,
                ),
            )
            return
        except Exception:  # a refused or lost send leaves the stored attempt for the next pass
            log.exception(
                logger,
                event=OperationBroadcastFailed(operation_id=operation.id, transaction_hash=attempt.transaction_hash),
            )
            return
        async with self.session_factory() as session, session.begin():
            stored = await session.get(OutgoingAttempt, attempt.id)
            if stored is not None:
                stored.last_broadcast_at = datetime.now(UTC)

    async def reconcile_broadcast(
        self,
        operation: OutgoingOperation,
        *,
        attempts: list[OutgoingAttempt],
        chain: EvmChainConfiguration,
        endpoint: EvmRpcEndpoint,
        readings: SettlementPassReadings,
    ) -> None:
        """``attempts`` are the operation's attempts as the pass read them; only this step adds one, so they are all
        of them, and the pass read every one's receipt."""
        for attempt in attempts:
            receipt = readings.receipt_of(attempt.transaction_hash)
            if receipt is not None:
                await self.record_mined(operation.id, attempt_id=attempt.id, receipt=receipt)
                return
        newest_attempt = attempts[-1]
        latest_nonce = readings.latest_nonce_by_address.get(operation.operator_address)
        if latest_nonce is None:
            raise PassReadUnavailableError(f"the pass read no nonce of the key of broadcast operation {operation.id}")
        newest_attempt_aged = newest_attempt.created_at < datetime.now(UTC) - NONCE_TAKEN_ELSEWHERE_AFTER
        key_frozen = readings.key_status_of(operation.operator_address) == OperatorKeyStatus.FROZEN
        if key_frozen and (operation.kind.is_deposit_sweep or operation.kind == OperationKind.TOP_UP):
            # A sweep is asked for again on another key, whether or not its nonce was used; a top-up its frozen key
            # sends is never sent again (its recipient may be that key), and another key may send it next look.
            if newest_attempt_aged:
                await self.resolve_without_receipt(
                    operation, attempts=attempts, chain=chain, failure_reason=OPERATOR_KEY_FROZEN_FAILURE_REASON
                )
                return
            if operation.kind == OperationKind.TOP_UP:
                return
        if operation.nonce is not None and latest_nonce > operation.nonce:
            # Our nonce is used: one of our attempts mined (this endpoint may not serve its receipt yet) or another
            # transaction took it. A bump or a rebroadcast cannot help, so the newest attempt only ages from here.
            if newest_attempt_aged:
                await self.resolve_without_receipt(
                    operation, attempts=attempts, chain=chain, failure_reason="nonce_taken_elsewhere"
                )
            return
        if (
            newest_attempt.created_at < datetime.now(UTC) - timedelta(seconds=chain.fee_bump_after_seconds)
            and not key_frozen
        ):
            await self.replace_with_higher_fees(
                operation, newest_attempt=newest_attempt, chain=chain, endpoint=endpoint, readings=readings
            )
            return
        # A frozen key's stored attempts are only rebroadcast: it signs nothing new.
        await self.broadcast_attempt(newest_attempt, operation=operation, endpoint=endpoint)

    async def resolve_without_receipt(
        self,
        operation: OutgoingOperation,
        *,
        attempts: list[OutgoingAttempt],
        chain: EvmChainConfiguration,
        failure_reason: str,
    ) -> None:
        """Ask every endpoint for the receipts before deciding, since a lagging one may just not serve one; the
        tick's endpoint too, since the pass read its receipts before the earlier steps ran.

        An endpoint that does not answer is skipped, so one dead endpoint cannot hold the decision forever (a refund
        waited 31 minutes on a timing-out one, 2026-10-09, live); it still needs two endpoints that answered no
        receipt, unless the chain has only one. A wrong call is settled by the owner's ``mark-paid``."""
        chain_endpoints = self.rpc_clients[chain.key].endpoints
        endpoints_without_receipt = 0
        for chain_endpoint in chain_endpoints:
            try:
                endpoint_receipts = await chain_endpoint.read_transaction_receipts(
                    [attempt.transaction_hash for attempt in attempts]
                )
            except EvmRpcUnavailableError:
                log.emit(logger, event=ReceiptEndpointSkipped(operation_id=operation.id))
                continue
            for attempt, receipt in zip(attempts, endpoint_receipts, strict=True):
                if receipt is not None:
                    await self.record_mined(operation.id, attempt_id=attempt.id, receipt=receipt)
                    return
            endpoints_without_receipt += 1
        if endpoints_without_receipt < min(2, len(chain_endpoints)):
            return
        # A refund's failure is not proven, so it keeps its reservation and waits for the owner (no double refund).
        # A sweep may simply fail (another sweep moves the money to the same destination); so may a key operation:
        # what it moved, if anything, shows in the key's balances, which the next decision reads.
        status = OperationStatus.NEEDS_OWNER if operation.kind == OperationKind.REFUND else OperationStatus.FAILED
        await self.resolve(operation, status=status, failure_reason=failure_reason)

    async def replace_with_higher_fees(
        self,
        operation: OutgoingOperation,
        *,
        newest_attempt: OutgoingAttempt,
        chain: EvmChainConfiguration,
        endpoint: EvmRpcEndpoint,
        readings: SettlementPassReadings,
    ) -> None:
        if operation.nonce is None:
            return
        account = self.operator_key_rings[chain.key].find_account(operation.operator_address)
        if account is None:
            await self.rebroadcast_instead_of_bump(
                newest_attempt, operation=operation, endpoint=endpoint, reason="operator_key_not_configured"
            )
            return
        replacement_fee_caps = replacement_fee_caps_of(chain, newest_attempt=newest_attempt, readings=readings)
        if replacement_fee_caps is None:
            await self.broadcast_attempt(newest_attempt, operation=operation, endpoint=endpoint)
            return
        max_fee, priority_fee = replacement_fee_caps
        try:
            # A top-up sent for another key resends its first call, never a new signature (operation_transactions.py).
            target_address, call_data, value = (
                stored_call_of(newest_attempt)
                if operation.kind == OperationKind.TOP_UP and operation.recipient_address != operation.operator_address
                else await self.transaction_builder.transaction_call_of(operation, chain=chain, endpoint=endpoint)
            )
        except TreasuryNotConfiguredError:
            await self.rebroadcast_instead_of_bump(
                newest_attempt, operation=operation, endpoint=endpoint, reason="treasury_not_configured"
            )
            return
        try:
            gas_limit = await estimate_replacement_gas_limit(
                session_factory=self.session_factory,
                operation=operation,
                newest_attempt=newest_attempt,
                chain=chain,
                endpoint=endpoint,
                target_address=target_address,
                call_data=call_data,
                value=value,
                max_fee_per_gas=max_fee,
            )
        except ReplacementRefusedError as refusal:
            # Read after the estimate: a call that now reverts is likely the first attempt mined since the pass's reads.
            if await endpoint.read_transaction_count(operation.operator_address, block_tag="latest") <= operation.nonce:
                await self.rebroadcast_instead_of_bump(
                    newest_attempt, operation=operation, endpoint=endpoint, reason=refusal.reason
                )
            return
        # The pass's ``latest`` nonce is older than this step: a replacement at a nonce used since cannot mine.
        if await endpoint.read_transaction_count(operation.operator_address, block_tag="latest") > operation.nonce:
            return
        attempt = sign_operation_attempt(
            operation,
            account=account,
            chain=chain,
            nonce=operation.nonce,
            target_address=target_address,
            call_data=call_data,
            value=value,
            gas_limit=gas_limit,
            max_fee_per_gas=max_fee,
            max_priority_fee_per_gas=priority_fee,
        )
        async with self.session_factory() as session, session.begin():
            await SettlementRepository(session=session).insert_attempt(attempt)
        log.emit(
            logger,
            event=OperationFeeBumped(
                operation_id=operation.id,
                nonce=operation.nonce,
                transaction_hash=attempt.transaction_hash,
                max_fee_per_gas=max_fee,
            ),
        )
        await self.broadcast_attempt(attempt, operation=operation, endpoint=endpoint)

    async def rebroadcast_instead_of_bump(
        self, newest_attempt: OutgoingAttempt, *, operation: OutgoingOperation, endpoint: EvmRpcEndpoint, reason: str
    ) -> None:
        log.emit(logger, event=FeeBumpSkippedForRebroadcast(operation_id=operation.id, reason=reason))
        await self.broadcast_attempt(newest_attempt, operation=operation, endpoint=endpoint)

    async def record_mined(self, operation_id: int, *, attempt_id: int, receipt: TransactionReceipt) -> None:
        async with self.session_factory() as session, session.begin():
            repository = SettlementRepository(session=session)
            operation = await repository.get_operation(operation_id, lock=True)
            attempt = await session.get(OutgoingAttempt, attempt_id)
            if attempt is not None:
                attempt.receipt_succeeded = receipt.succeeded
                attempt.receipt_block_number = receipt.block_number
                attempt.receipt_block_hash = receipt.block_hash
                attempt.receipt_gas_used = receipt.gas_used
                attempt.receipt_effective_gas_price = receipt.effective_gas_price
            operation.status = OperationStatus.MINED
            operation.transaction_hash = receipt.transaction_hash
            operation.block_number = receipt.block_number
            operation.block_hash = receipt.block_hash
        log.emit(
            logger,
            event=OperationMined(
                operation_id=operation_id,
                transaction_hash=receipt.transaction_hash,
                block_number=receipt.block_number,
                succeeded=receipt.succeeded,
            ),
        )

    async def finalize_mined(self, operation: OutgoingOperation, *, readings: SettlementPassReadings) -> None:
        if operation.block_number is None or operation.transaction_hash is None:
            return
        if readings.block_heads.finalized.number < operation.block_number:
            return
        receipt = readings.receipt_of(operation.transaction_hash)
        if receipt is None:
            # Reorged out before finality: back to reconciling every attempt.
            await self.set_status(operation.id, status=OperationStatus.BROADCAST)
            return
        if receipt.block_hash != operation.block_hash:
            async with self.session_factory() as session, session.begin():
                locked = await SettlementRepository(session=session).get_operation(operation.id, lock=True)
                locked.block_number = receipt.block_number
                locked.block_hash = receipt.block_hash
            return
        if not receipt.succeeded:
            await self.resolve(
                operation, status=OperationStatus.FAILED, failure_reason=f"{operation.kind.value}_reverted"
            )
            return
        # A refund, top-up or treasury return moved what its receipt's Transfer logs to the recipient say (a native
        # one its value); a sweep's moves are its DepositSwept events, a Safe creation moves nothing.
        moved = (
            moved_by_refund(operation, receipt=receipt)
            if operation.kind in {OperationKind.REFUND, OperationKind.TOP_UP, OperationKind.TREASURY_RETURN}
            else None
        )
        await self.resolve(operation, status=OperationStatus.FINAL, moved_base_units=moved)

    async def set_status(self, operation_id: int, *, status: OperationStatus) -> None:
        async with self.session_factory() as session, session.begin():
            operation = await SettlementRepository(session=session).get_operation(operation_id, lock=True)
            operation.status = status

    async def mark_waiting_for_float(self, operation: OutgoingOperation) -> None:
        """``waiting_for_float``; an outcome only on the transition, so the owner is told once."""
        if operation.status == OperationStatus.WAITING_FOR_FLOAT:
            return
        log.emit(
            logger, event=OperationWaitingForFloat(operation_id=operation.id, asset_symbol=operation.asset_symbol or "")
        )
        await self.resolve(operation, status=OperationStatus.WAITING_FOR_FLOAT)

    async def resolve(
        self,
        operation: OutgoingOperation,
        *,
        status: OperationStatus,
        failure_reason: str | None = None,
        moved_base_units: int | None = None,
    ) -> None:
        """The new status and its effect on its invoice or static address in one transaction (module docstring);
        nothing when the locked row left the status this step read (the owner's CLI settled it meanwhile)."""
        async with self.session_factory() as session, session.begin():
            locked = await SettlementRepository(session=session).get_operation(operation.id, lock=True)
            if locked.status != operation.status:
                return
            locked.status = status
            locked.failure_reason = failure_reason
            if moved_base_units is not None:
                locked.moved_base_units = moved_base_units
            await session.flush()
            if locked.kind.has_invoice:
                outcome = SettlementOutcome(operation=operation_value_of(locked))
                if (locked.kind.value, status.value) in self.e2e_failing_resolutions[locked.chain_key]:
                    raise E2eInjectedResolveFailureError(
                        f"PAY_E2E_FAIL_AFTER_OPERATION_RESOLVE: {locked.kind.value} {operation.id} -> {status.value}"
                    )
                await self.apply_settlement_outcome(session, outcome=outcome)
            elif (static_deposit_outcome := static_deposit_sweep_outcome_of(locked, status=status)) is not None:
                await self.apply_static_deposit_sweep_outcome(session, outcome=static_deposit_outcome)
            kind = locked.kind
        log.emit(
            logger,
            event=OperationResolved(
                operation_id=operation.id, kind=kind.value, status=status.value, failure_reason=failure_reason
            ),
        )
