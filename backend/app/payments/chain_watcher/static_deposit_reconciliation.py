"""The static deposit addresses' reconcile checks (PLAN.md section 17), read only, as ``monero_reconciliation.py`` is
for Monero; ``ReconciliationService`` runs them.

- ``find_static_deposit_sweep_findings`` (in the outgoing operations check, so the watcher alerts them): a static
  sweep unresolved past the stuck age (``operation_stuck``) and an address whose sweep failed (``sweep_blocked``,
  alert kind ``sweep_failed``), each naming ``sweep-static-deposit-address``.
- ``check_static_deposit_balances`` (``reconcile`` only, it reads every address): every static deposit address at the
  finalized block; one holding at least a minimum sweep with no sweep under way (in a coin its sweeps move: not one
  ``--skip-token`` left out), or whose balance could not be read.
  These findings have no alert kind.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta

from app.infrastructure.evm_rpc_api.client import EvmRpcClient, EvmRpcEndpoint, decode_uint256
from app.infrastructure.evm_rpc_api.errors import EvmExecutionRevertedError, EvmRpcUnavailableError
from app.infrastructure.evm_rpc_api.schemas import BlockHeads, Multicall3Result
from app.payments.chain_watcher.types import ReconciliationCheckResult, ReconciliationFinding
from app.payments.chains.types import EvmChainConfiguration
from app.payments.settlements.operator_key_assets import balance_call_of
from app.payments.settlements.service import SettlementService
from app.payments.static_deposit_addresses.readings_service import StaticDepositReadingsService
from app.shared.amounts import format_base_units

# Addresses per page of the balance read (each page is one aggregate3 per 500 calls), as for invoice deposits.
STATIC_DEPOSITS_PER_BALANCE_PAGE = 1_000


def describe_holdings(chain: EvmChainConfiguration, *, held_by_symbol: dict[str, int]) -> str:
    parts: Sequence[str] = [
        f"{format_base_units(amount, decimals=asset.decimals)} {asset.symbol}"
        for asset in chain.assets
        if (amount := held_by_symbol.get(asset.symbol))
    ]
    return ", ".join(parts)


async def find_static_deposit_sweep_findings(
    chain: EvmChainConfiguration,
    *,
    settlement_service: SettlementService,
    static_deposit_readings_service: StaticDepositReadingsService,
    now: datetime,
    stuck_after: timedelta,
) -> tuple[list[ReconciliationFinding], int, int]:
    """(findings, stuck sweep count, sweep-blocked address count)."""
    stuck_static_deposit_sweeps = await settlement_service.list_unresolved_static_deposit_sweeps_created_before(
        chain.key, created_before=now - stuck_after
    )
    sweep_blocked_static_deposit_address_ids = (
        await static_deposit_readings_service.list_sweep_blocked_static_deposit_address_ids(chain.key)
    )
    findings: list[ReconciliationFinding] = []
    for static_deposit_sweep in stuck_static_deposit_sweeps:
        minutes = int((now - static_deposit_sweep.created_at).total_seconds() // 60)
        findings.append(
            ReconciliationFinding(
                problem_key=f"operation_stuck:{static_deposit_sweep.operation_id}",
                alert_kind="operation_stuck",
                invoice_id=None,
                message=f"Static deposit sweep {static_deposit_sweep.operation_id} of "
                f"{static_deposit_sweep.static_deposit_address_id} is still {static_deposit_sweep.status.value} "
                f"after {minutes} minutes.",
                columns={
                    "operation": str(static_deposit_sweep.operation_id),
                    "kind": "static_deposit_sweep",
                    "static address": static_deposit_sweep.static_deposit_address_id,
                    "status": static_deposit_sweep.status.value,
                    "age": f"{minutes} min",
                },
            )
        )
    findings.extend(
        ReconciliationFinding(
            problem_key=f"static_deposit_sweep_failed:{static_deposit_address_id}",
            alert_kind="sweep_failed",
            invoice_id=None,
            message=f"A sweep of static deposit address {static_deposit_address_id} failed; its money stays there "
            f"until `sweep-static-deposit-address {static_deposit_address_id}` (a token frozen by its issuer: add "
            "`--skip-token <SYMBOL>`, which later automatic sweeps keep).",
            columns={
                "operation": "",
                "kind": "static_deposit_sweep",
                "static address": static_deposit_address_id,
                "status": "sweep_blocked",
            },
        )
        for static_deposit_address_id in sweep_blocked_static_deposit_address_ids
    )
    return findings, len(stuck_static_deposit_sweeps), len(sweep_blocked_static_deposit_address_ids)


async def check_static_deposit_balances(
    chain: EvmChainConfiguration,
    *,
    rpc_client: EvmRpcClient,
    static_deposit_readings_service: StaticDepositReadingsService,
    chain_unreadable_finding: ReconciliationFinding,
) -> ReconciliationCheckResult:
    """Every static deposit address at the finalized block: one holding at least a minimum sweep with no sweep under
    way (the scan asks for one whenever it reads such money, so this is a sweep that never got asked), or one whose
    balance could not be read."""
    static_deposits = []
    after_static_deposit_address_id: str | None = None
    while True:
        page = await static_deposit_readings_service.list_chain_static_deposits_after(
            chain.key,
            after_static_deposit_address_id=after_static_deposit_address_id,
            limit=STATIC_DEPOSITS_PER_BALANCE_PAGE,
        )
        static_deposits.extend(page)
        if len(page) < STATIC_DEPOSITS_PER_BALANCE_PAGE:
            break
        after_static_deposit_address_id = page[-1].static_deposit_address_id
    calls = [
        balance_call_of(asset, holder_address=static_deposit.deposit_address)
        for static_deposit in static_deposits
        for asset in chain.assets
    ]

    async def read_finalized_balances(endpoint: EvmRpcEndpoint) -> tuple[BlockHeads, list[Multicall3Result]]:
        # Heads and balances from one endpoint: another may not know the pinned block hash.
        block_heads = await endpoint.read_block_heads()
        return block_heads, await endpoint.aggregate3(calls, block_hash=block_heads.finalized.hash)

    try:
        block_heads, results = await rpc_client.call_with_failover(read_finalized_balances)
    except (EvmRpcUnavailableError, EvmExecutionRevertedError):
        return ReconciliationCheckResult(
            check_name="static_deposit_balances",
            summary=f"{len(static_deposits)} static deposit addresses, balances not readable",
            findings=[chain_unreadable_finding],
        )
    held_by_static_deposit_address_id: dict[str, dict[str, int]] = {}
    unreadable_static_deposit_address_ids: set[str] = set()
    asset_count = len(chain.assets)
    for index, static_deposit in enumerate(static_deposits):
        for asset, result in zip(chain.assets, results[index * asset_count : (index + 1) * asset_count], strict=True):
            if not result.succeeded:
                unreadable_static_deposit_address_ids.add(static_deposit.static_deposit_address_id)
                continue
            balance = decode_uint256(result.return_data)
            if (
                balance > 0
                and balance >= asset.minimum_sweep_base_units
                and asset.symbol not in static_deposit.skipped_sweep_token_symbols
            ):
                held_by_static_deposit_address_id.setdefault(static_deposit.static_deposit_address_id, {})[
                    asset.symbol
                ] = balance
    holding_ids = sorted(held_by_static_deposit_address_id)
    swept_ids = await static_deposit_readings_service.list_static_deposit_address_ids_with_active_sweep(holding_ids)
    findings: list[ReconciliationFinding] = []
    for static_deposit_address_id in holding_ids:
        if static_deposit_address_id in swept_ids:
            continue
        held_text = describe_holdings(
            chain, held_by_symbol=held_by_static_deposit_address_id[static_deposit_address_id]
        )
        findings.append(
            ReconciliationFinding(
                problem_key=f"static_deposit_unswept:{static_deposit_address_id}",
                alert_kind=None,
                invoice_id=None,
                message=f"Static deposit address {static_deposit_address_id} holds {held_text} at the finalized "
                f"block and no sweep is under way: `sweep-static-deposit-address {static_deposit_address_id}`.",
                columns={"static address": static_deposit_address_id, "holds": held_text},
            )
        )
    findings.extend(
        ReconciliationFinding(
            problem_key=f"static_deposit_unreadable:{static_deposit_address_id}",
            alert_kind=None,
            invoice_id=None,
            message=f"A balance of static deposit address {static_deposit_address_id} could not be read.",
            columns={"static address": static_deposit_address_id, "holds": "unreadable"},
        )
        for static_deposit_address_id in sorted(unreadable_static_deposit_address_ids)
    )
    return ReconciliationCheckResult(
        check_name="static_deposit_balances",
        summary=f"{len(static_deposits)} static deposit addresses in {asset_count} assets at finalized block "
        f"{block_heads.finalized.number}; {len(holding_ids)} hold at least a minimum sweep",
        findings=findings,
    )
