"""``python -m app.cli treasury ...``: the treasury Safe the operator keys are topped up from (2026-10-13; PLAN.md
section 5, "Treasury"; ``settlements/treasury_service.py``).

- ``create-safe --chain polygon --owner-address 0x...``: a SafeL2 proxy with that one owner and threshold 1, sent by
  the watcher from the refund key (it needs a little gas). An operator key's address is refused as the owner.
  Prints the Safe's address, the same on every run for the same owner. Set ``PAY_TREASURY_SAFE_ADDRESS_<CHAIN>`` to it and restart the watcher.
- ``setup-transactions --chain polygon``: a Safe Transaction Builder batch file on stdout (what each call does on
  stderr) that enables the Allowance Module, adds every active key as a delegate with its daily allowances, and
  removes frozen, retired and no longer configured keys. Load it in the Safe app, check it and sign it there.
- ``execute-setup --chain polygon --owner-key-environment-variable NAME``: the same batch sent at once by the owner's
  key, read from the named environment variable (never an operator key's). For fork e2e and test Safes only.
- ``status --chain polygon``: the Safe's owners, threshold, module, delegates, the setup still missing, balances and
  each key's allowance.
"""

import asyncio
import json
import os
from typing import Annotated

from eth_utils import to_checksum_address
from pydantic import SecretStr
import typer

from app.cli_operator_keys import check_enabled_chain, format_operator_key_report
from app.lifespan.composition import open_cli_invoice_resources
from app.lifespan.resources import CliInvoiceResources
from app.payments.settlements.configuration import OPERATOR_KEY_VARIABLE_PREFIXES
from app.payments.settlements.treasury_service import TreasuryService, transaction_builder_batch_of
from app.shared.errors import AppError

treasury_application = typer.Typer(no_args_is_help=True, help="The treasury Safe that tops up the operator keys.")


def treasury_service_of(resources: CliInvoiceResources, *, chain_key: str) -> TreasuryService:
    wiring = resources.invoice_wiring
    key_addresses = wiring.operator_key_addresses.get(chain_key)
    if key_addresses is None:
        raise typer.BadParameter(
            f"PAY_OPERATOR_PRIVATE_KEY_{chain_key.upper()} is not set here: run the CLI in the watcher container",
            param_hint="--chain",
        )
    return TreasuryService(
        chain=wiring.chains_service.get_evm_chain(chain_key),
        key_addresses=key_addresses,
        rpc_client=wiring.rpc_clients[chain_key],
        session_factory=resources.database.session_factory,
    )


def owner_address_of(address: str) -> str:
    try:
        return to_checksum_address(address)
    except ValueError as error:
        raise typer.BadParameter("not an address", param_hint="--owner-address") from error


async def create_treasury_safe(chain_key: str, *, owner_address: str) -> None:
    async with open_cli_invoice_resources() as resources:
        creation = await treasury_service_of(resources, chain_key=chain_key).create_safe(owner_address=owner_address)
    explanation = {
        "deployed": "already deployed",
        "recorded": f"recorded as operation {creation.operation_id}; the watcher sends it from the refund key",
        "under_way": f"operation {creation.operation_id} is already sending it",
    }[creation.state]
    typer.echo(f"treasury Safe {creation.safe_address}: {explanation}")
    typer.echo(f"then set PAY_TREASURY_SAFE_ADDRESS_{chain_key.upper()}={creation.safe_address} and restart")


@treasury_application.command("create-safe")
def create_safe_command(
    owner_address: Annotated[str, typer.Option(help="The Safe's one owner (threshold 1), e.g. a hardware wallet.")],
    chain: Annotated[str, typer.Option(help="A chains.toml key among PAY_ENABLED_CHAINS.")] = "polygon",
) -> None:
    """Create the treasury Safe (the watcher sends it from the refund key); prints its address."""
    check_enabled_chain(chain)
    try:
        asyncio.run(create_treasury_safe(chain, owner_address=owner_address_of(owner_address)))
    except AppError as error:
        typer.echo(error.message, err=True)
        raise typer.Exit(1) from error


async def print_setup_transactions(chain_key: str) -> None:
    async with open_cli_invoice_resources() as resources:
        treasury_service = treasury_service_of(resources, chain_key=chain_key)
        calls = await treasury_service.build_setup_calls()
        safe_address, _ = treasury_service.configured_safe_and_module()
        chain = treasury_service.chain
    if not calls:
        typer.echo("nothing to set up: the Safe already matches the keys and their allowances", err=True)
        return
    for call in calls:
        typer.echo(f"- {call.description}", err=True)
    typer.echo(json.dumps(transaction_builder_batch_of(calls, chain=chain, safe_address=safe_address), indent=2))


@treasury_application.command("setup-transactions")
def setup_transactions_command(
    chain: Annotated[str, typer.Option(help="A chains.toml key among PAY_ENABLED_CHAINS.")] = "polygon",
) -> None:
    """The setup still missing, as a Safe Transaction Builder batch file on stdout."""
    check_enabled_chain(chain)
    try:
        asyncio.run(print_setup_transactions(chain))
    except AppError as error:
        typer.echo(error.message, err=True)
        raise typer.Exit(1) from error


async def execute_treasury_setup(chain_key: str, *, owner_private_key: SecretStr) -> bool:
    async with open_cli_invoice_resources() as resources:
        execution = await treasury_service_of(resources, chain_key=chain_key).execute_setup(
            owner_private_key=owner_private_key
        )
    if execution.transaction_hash is None:
        typer.echo("nothing to set up: the Safe already matches the keys and their allowances")
        return True
    verdict = "done" if execution.succeeded else "FAILED (reverted or no receipt in time)"
    typer.echo(f"setup of {execution.call_count} calls {verdict}: {execution.transaction_hash}")
    return execution.succeeded


@treasury_application.command("execute-setup")
def execute_setup_command(
    owner_key_environment_variable: Annotated[
        str, typer.Option(help="The environment variable holding the owner's private key (never the key itself).")
    ],
    chain: Annotated[str, typer.Option(help="A chains.toml key among PAY_ENABLED_CHAINS.")] = "polygon",
) -> None:
    """Send the setup batch with the owner's key. Fork e2e and test Safes only: a Safe holding real money is set up
    in the Safe app from ``setup-transactions``, its owner key never on this host."""
    check_enabled_chain(chain)
    if owner_key_environment_variable.startswith(OPERATOR_KEY_VARIABLE_PREFIXES):
        raise typer.BadParameter(
            "that is an operator key's variable: the Safe's owner is never an operator key",
            param_hint="--owner-key-environment-variable",
        )
    owner_private_key = os.environ.get(owner_key_environment_variable, "")
    if not owner_private_key:
        raise typer.BadParameter("that environment variable is empty", param_hint="--owner-key-environment-variable")
    try:
        succeeded = asyncio.run(execute_treasury_setup(chain, owner_private_key=SecretStr(owner_private_key)))
    except AppError as error:
        typer.echo(error.message, err=True)
        raise typer.Exit(1) from error
    if not succeeded:
        raise typer.Exit(1)


async def print_treasury_status(chain_key: str) -> bool:
    async with open_cli_invoice_resources() as resources:
        status = await treasury_service_of(resources, chain_key=chain_key).read_status()
    owners_text = "unreadable" if status.owners is None else ", ".join(status.owners) or "none (no Safe there)"
    typer.echo(f"treasury Safe {status.safe_address}: owners {owners_text}, threshold {status.threshold}")
    typer.echo(f"Allowance Module enabled: {status.module_enabled}")
    for address, is_delegate in status.delegates.items():
        typer.echo(f"  delegate {address}: {'unreadable' if is_delegate is None else is_delegate}")
    for description in status.pending_setup_calls:
        typer.echo(f"  setup missing: {description}")
    for line in format_operator_key_report(status.key_report):
        typer.echo(line)
    return not status.pending_setup_calls and not status.key_report.findings


@treasury_application.command("status")
def status_command(
    chain: Annotated[str, typer.Option(help="A chains.toml key among PAY_ENABLED_CHAINS.")] = "polygon",
) -> None:
    """The Safe's owners, module, delegates and balances and each key's allowance; exit 1 when something is missing."""
    check_enabled_chain(chain)
    try:
        healthy = asyncio.run(print_treasury_status(chain))
    except AppError as error:
        typer.echo(error.message, err=True)
        raise typer.Exit(1) from error
    if not healthy:
        raise typer.Exit(1)
