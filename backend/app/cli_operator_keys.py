"""``python -m app.cli operator ...``: the operator keys of a chain (2026-10-13; PLAN.md section 5, "Operator keys").

- ``list-keys --chain polygon``: every key the chain knows (configured or only in ``operator_key_states``), its role,
  status and why, its nonce at ``latest`` against the allocator's next, its active operations, balances and Allowance
  Module allowances, the treasury Safe's balances, and what needs the operator. Read-only.
- ``retire-key --chain polygon --address 0x...``: no new work for the key. The watcher returns what it holds to the
  configured treasury Safe once its last operation ends (tokens first, then its native coin once); without a treasury
  its funds are moved by hand.
- ``unfreeze-key --chain polygon --address 0x... --reason "..."``: only when the use that froze it was the owner's
  own. The allocator moves up to the key's pending nonce count, so the nonces used elsewhere are skipped.
- ``fill-abandoned-nonces --chain polygon --address 0x...``: a 0-value transfer of the frozen key to itself at each
  nonce it was handed but never used (``abandoned_nonce_service.py``); ``unfreeze-key`` and ``retire-key`` refuse a
  frozen key until none is left.

It prints addresses, never a key.
"""

import asyncio
import time
from typing import Annotated

from eth_utils import to_checksum_address
import typer

from app.infrastructure.evm_rpc_api.client import EvmRpcEndpoint
from app.lifespan.composition import open_cli_invoice_resources
from app.lifespan.resources import CliInvoiceResources
from app.payments.chains.configuration import read_enabled_chains
from app.payments.chains.types import EvmChainConfiguration
from app.payments.settlements.abandoned_nonce_service import AbandonedNonceService
from app.payments.settlements.enums import OperatorKeyStatus
from app.payments.settlements.operator_key_maintenance_service import OperatorKeyMaintenanceService
from app.payments.settlements.operator_key_service import OperatorKeyService
from app.payments.settlements.operator_keys import read_operator_key_ring
from app.payments.settlements.types import OperatorKeyReport
from app.shared.errors import AppError

operator_keys_application = typer.Typer(
    no_args_is_help=True,
    help="The operator keys: refund key and sweeper pool (list, retire, unfreeze, fill abandoned nonces).",
)

# How long ``fill-abandoned-nonces`` waits for its transfers to mine: a few Polygon blocks with room for a slow pool.
ABANDONED_NONCE_FILL_WAIT_SECONDS = 180


def check_enabled_chain(chain_key: str) -> None:
    enabled_chains = {enabled_chain.key: enabled_chain for enabled_chain in read_enabled_chains()}
    if chain_key not in enabled_chains:
        raise typer.BadParameter(f"{chain_key} is not an enabled chain (PAY_ENABLED_CHAINS)", param_hint="--chain")
    if not isinstance(enabled_chains[chain_key], EvmChainConfiguration):
        raise typer.BadParameter(
            f"{chain_key} is a Monero chain, which has no operator keys and no treasury", param_hint="--chain"
        )


def checksum_address_of(address: str) -> str:
    try:
        return to_checksum_address(address)
    except ValueError as error:
        raise typer.BadParameter("not an address", param_hint="--address") from error


async def build_operator_key_report(
    resources: CliInvoiceResources, *, chain: EvmChainConfiguration
) -> OperatorKeyReport:
    """The watcher's look at the chain's keys and treasury, nothing written (``reconcile`` prints it too)."""
    key_addresses = resources.invoice_wiring.operator_key_addresses.get(chain.key)
    if key_addresses is None:
        raise typer.BadParameter(
            f"PAY_OPERATOR_PRIVATE_KEY_{chain.key.upper()} is not set here: run the CLI in the watcher container",
            param_hint="--chain",
        )
    maintenance_service = OperatorKeyMaintenanceService(
        chain=chain,
        key_addresses=key_addresses,
        session_factory=resources.database.session_factory,
        rpc_client=resources.invoice_wiring.rpc_clients[chain.key],
    )

    async def read(endpoint: EvmRpcEndpoint) -> OperatorKeyReport:
        return await maintenance_service.build_report(endpoint, block_heads=await endpoint.read_block_heads())

    return await resources.invoice_wiring.rpc_clients[chain.key].call_with_failover(read)


def format_operator_key_report(report: OperatorKeyReport) -> list[str]:
    lines = [report.summary]
    for row in report.rows:
        nonce_text = f"nonce {row.latest_nonce} (allocator next {row.allocator_next_nonce})"
        lines.append(
            f"  key {row.key_id if row.key_id is not None else '-'}\t{row.role}\t{row.status}\t{row.operator_address}"
            f"\t{nonce_text}\t{row.active_operations} active operations"
            + ("" if row.configured else "\tno longer configured")
        )
        if row.reason:
            lines.append(f"    why: {row.reason}")
        if row.balances:
            lines.append("    holds: " + ", ".join(f"{amount} {symbol}" for symbol, amount in row.balances.items()))
        if row.allowances:
            lines.append("    allowance: " + ", ".join(f"{symbol} {text}" for symbol, text in row.allowances.items()))
    if report.treasury_balances:
        lines.append(
            "  treasury Safe holds: "
            + ", ".join(f"{amount} {symbol}" for symbol, amount in report.treasury_balances.items())
        )
    lines.extend(f"  - {finding.message}" for finding in report.findings)
    return lines


async def list_operator_keys(chain_key: str) -> bool:
    async with open_cli_invoice_resources() as resources:
        chain = resources.invoice_wiring.chains_service.get_evm_chain(chain_key)
        report = await build_operator_key_report(resources, chain=chain)
    for line in format_operator_key_report(report):
        typer.echo(line)
    return not report.findings


@operator_keys_application.command("list-keys")
def list_keys_command(
    chain: Annotated[str, typer.Option(help="A chains.toml key among PAY_ENABLED_CHAINS.")] = "polygon",
) -> None:
    """Every operator key of the chain with its state, balances and allowances; exit 1 when one needs the operator."""
    check_enabled_chain(chain)
    if not asyncio.run(list_operator_keys(chain)):
        raise typer.Exit(1)


def abandoned_nonce_service_of(
    resources: CliInvoiceResources, *, chain: EvmChainConfiguration
) -> AbandonedNonceService:
    return AbandonedNonceService(
        chain=chain,
        session_factory=resources.database.session_factory,
        rpc_client=resources.invoice_wiring.rpc_clients[chain.key],
    )


async def refuse_frozen_key_with_abandoned_nonces(
    resources: CliInvoiceResources, *, chain: EvmChainConfiguration, operator_address: str
) -> None:
    """A frozen key signs again (unfrozen, or retired for its returns) only with no nonce gap before its next one."""
    async with resources.database.session_factory() as session:
        key_states = await OperatorKeyService.on_session(session).list_key_states(chain.key)
    if any(
        key_state.operator_address == operator_address and key_state.status == OperatorKeyStatus.FROZEN
        for key_state in key_states
    ):
        await abandoned_nonce_service_of(resources, chain=chain).refuse_when_abandoned_nonces(operator_address)


async def retire_operator_key(chain_key: str, *, operator_address: str) -> None:
    async with open_cli_invoice_resources() as resources:
        chain = resources.invoice_wiring.chains_service.get_evm_chain(chain_key)
        await refuse_frozen_key_with_abandoned_nonces(resources, chain=chain, operator_address=operator_address)
        async with resources.database.session_factory() as session, session.begin():
            key_state = await OperatorKeyService.on_session(session).retire_key(
                chain_key, operator_address=operator_address
            )
    returns = (
        "the watcher returns what it holds to the treasury Safe once its operations end"
        if chain.treasury.is_enabled
        else "no treasury Safe is configured: move what it holds by hand"
    )
    typer.echo(f"operator key {key_state.key_id} ({key_state.role.value}) retired; {returns}")


@operator_keys_application.command("retire-key")
def retire_key_command(
    address: Annotated[str, typer.Option(help="The key's address (never its private key).")],
    chain: Annotated[str, typer.Option(help="A chains.toml key among PAY_ENABLED_CHAINS.")] = "polygon",
) -> None:
    """No new work for the key; what it holds goes back to the configured treasury Safe."""
    check_enabled_chain(chain)
    try:
        asyncio.run(retire_operator_key(chain, operator_address=checksum_address_of(address)))
    except AppError as error:
        typer.echo(error.message, err=True)
        raise typer.Exit(1) from error


async def unfreeze_operator_key(chain_key: str, *, operator_address: str, reason: str) -> None:
    async with open_cli_invoice_resources() as resources:
        chain = resources.invoice_wiring.chains_service.get_evm_chain(chain_key)
        await refuse_frozen_key_with_abandoned_nonces(resources, chain=chain, operator_address=operator_address)

        async def read_pending_count(endpoint: EvmRpcEndpoint) -> int:
            return await endpoint.read_transaction_count(operator_address, block_tag="pending")

        pending_transaction_count = await resources.invoice_wiring.rpc_clients[chain_key].call_with_failover(
            read_pending_count
        )
        async with resources.database.session_factory() as session, session.begin():
            key_state = await OperatorKeyService.on_session(session).unfreeze_key(
                chain_key,
                operator_address=operator_address,
                reason=reason,
                pending_transaction_count=pending_transaction_count,
            )
    typer.echo(
        f"operator key {key_state.key_id} ({key_state.role.value}) active again; "
        f"its next nonce is at least {pending_transaction_count}"
    )


@operator_keys_application.command("unfreeze-key")
def unfreeze_key_command(
    address: Annotated[str, typer.Option(help="The key's address (never its private key).")],
    reason: Annotated[str, typer.Option(help="Why the use outside pay-7331 was the owner's own; kept on the key.")],
    chain: Annotated[str, typer.Option(help="A chains.toml key among PAY_ENABLED_CHAINS.")] = "polygon",
) -> None:
    """Active again after a freeze, only when that use was the owner's own."""
    check_enabled_chain(chain)
    if not reason.strip():
        raise typer.BadParameter("say why", param_hint="--reason")
    try:
        asyncio.run(unfreeze_operator_key(chain, operator_address=checksum_address_of(address), reason=reason))
    except AppError as error:
        typer.echo(error.message, err=True)
        raise typer.Exit(1) from error


async def fill_abandoned_operator_nonces(chain_key: str, *, operator_address: str) -> bool:
    """True once every abandoned nonce is used (the key's ``latest`` count passed the last one)."""
    key_ring = read_operator_key_ring(chain_key)
    account = key_ring.find_account(operator_address) if key_ring is not None else None
    if account is None:
        raise typer.BadParameter(
            "not a key configured here: run the CLI in the watcher container, or send the key's 0-value transfers to "
            "itself by hand",
            param_hint="--address",
        )
    async with open_cli_invoice_resources() as resources:
        chain = resources.invoice_wiring.chains_service.get_evm_chain(chain_key)
        abandoned_nonce_service = abandoned_nonce_service_of(resources, chain=chain)
        fills = await abandoned_nonce_service.fill_abandoned_nonces(account)
        if not fills:
            typer.echo(f"{operator_address} has no abandoned nonce")
            return True
        for fill in fills:
            typer.echo(
                f"nonce {fill.nonce}: {fill.transaction_hash} sent, {fill.accepting_endpoints} endpoints accepted it"
            )
        last_filled_nonce = max(fill.nonce for fill in fills)
        deadline = time.monotonic() + ABANDONED_NONCE_FILL_WAIT_SECONDS
        while time.monotonic() < deadline:
            if await abandoned_nonce_service.read_latest_transaction_count(operator_address) > last_filled_nonce:
                typer.echo("every abandoned nonce is used; `unfreeze-key` or `retire-key` may run now")
                return True
            await asyncio.sleep(5)
    typer.echo(
        f"not all mined in {ABANDONED_NONCE_FILL_WAIT_SECONDS} s (fees at the cap, or an old attempt still pending): "
        "run this again later",
        err=True,
    )
    return False


@operator_keys_application.command("fill-abandoned-nonces")
def fill_abandoned_nonces_command(
    address: Annotated[str, typer.Option(help="The key's address (never its private key).")],
    chain: Annotated[str, typer.Option(help="A chains.toml key among PAY_ENABLED_CHAINS.")] = "polygon",
) -> None:
    """Use each nonce a frozen key was handed but never used, with a 0-value transfer to itself; exit 1 when not all
    of them mined in time."""
    check_enabled_chain(chain)
    if not asyncio.run(fill_abandoned_operator_nonces(chain, operator_address=checksum_address_of(address))):
        raise typer.Exit(1)
