"""The operator CLI: ``python -m app.cli`` (PLAN.md section 2).

- ``sweep-invoice <invoice id> [--skip-token USDC ...]``: clears ``sweep_blocked`` and records a sweep the watcher
  sends. ``--skip-token`` (a configured token's symbol, repeatable) leaves that token out even when the deposit holds
  it: one token whose transfer reverts (USDC blacklists the deposit or the destination, or is paused) reverts the
  whole ``forwardAll``, native coin and every other token included, so the owner sweeps the rest without it. The
  native coin is always forwarded and cannot be skipped. The skipped token stays in the deposit and the next sweep
  that lists it fails again (``sweep_blocked``), so ``reconcile`` keeps showing it until it moves (2026-10-13).
- ``sweep-static-deposit-address <sda_...> [--skip-token USDC ...] [--clear-skipped-tokens]``: the same for a
  static deposit address whose sweep failed (``sweep_blocked``; PLAN.md section 17). Its ``--skip-token`` coins are
  kept on the address, so its later automatic sweeps leave them out too; ``--clear-skipped-tokens`` forgets them.
- ``refunds list-needing-owner``: refunds that ended unproven (``needs_owner``) or wait for float
  (``waiting_for_float``, which may never be sendable), with nonce and signed attempts.
- ``refunds mark-paid <operation id> --transaction-hash 0x...``: ``final``, after the chain shows that transaction
  moved the reserved amount to the recipient.
- ``refunds mark-unpaid <operation id> --nonce-taken-by 0x...``: ``failed``, which frees the reserved amount, once
  the chain shows that finalized operator transaction at the refund's nonce, none of its attempts and paying nothing
  to the recipient, so no attempt can ever mine. "No endpoint shows a receipt" alone is no proof: a pruned node
  forgets old transactions (2026-10-09). A ``waiting_for_float`` refund that never signed an attempt used no nonce
  and takes no flag. Still refused while the nonce is unused or any endpoint shows an attempt's receipt (PLAN.md
  section 6).
- ``recover-deposit --invoice-id inv_... --destination 0x... [--chain polygon]`` or ``recover-deposit --from-log
  FILE`` (``-`` is stdin; the ``invoice_create`` lines of ``docker logs pay-7331-api``): for a deposit Postgres no
  longer knows, derives the salt from ``PAY_INVOICE_SALT_SEED``, predicts the address through the factory, prints its
  balance in every configured asset and the ``sweepToDestination`` call that would move it (PLAN.md section 3,
  2026-10-12). Read-only and needs no database; it sends nothing. Its output holds the salt: anyone with it can sweep
  that deposit early (to its destination only), so keep it off public channels.
- ``reconcile [--chain polygon]``: read only; every check of ``ReconciliationService`` (the watcher's progress and
  heartbeat, the price feeds, stuck or failed sweeps and refunds, the operator float against refunds waiting for it,
  outcomes never applied to their invoice, stray money with no sender, long-deferred automatic refunds, Postgres's WAL
  archiving failing or stalled, money left in deposits whose payment window is over), a short table per check, exit 0
  when nothing needs the operator and 1 otherwise, so a cron can run it (2026-10-12). It prints addresses, never a key.
- ``record-operator-alert --kind backup_failed|backup_restore_drill_failed [--detail "result=exit-code ..."]``: an
  ``operator.alert`` on the operator's account from the host's backup units, through the watcher's recorder and its
  once-per-problem rule with reminders (``host_operator_alerts.py``; 2026-10-12). Needs Postgres only, so the units run
  it in the api container (``docker exec``). Exit 1 when ``PAY_OPERATOR_ACCOUNT_IDS`` is empty or its first id names no
  account: then the WARNING log line on stderr is the only trace.
- ``download-coin-logos [--chain polygon]``: writes each configured asset's logo to
  ``frontend/public/coin_logos/<symbol lowercased>.png`` from CoinGecko's token list (by token address, decimals
  checked against chains.toml); run by hand when the asset list changes and commit the files. Nothing fetches logos
  or the list at runtime.
- ``mint-passkey-enrollment-link <username> [--expires-minutes 15]``: prints a one-time link that adds a passkey to
  that account without signing in, the account recovery path (``passkey_enrollment_link_service.py``, 2026-10-15). It
  writes at once like the other commands; a new link replaces the account's unused ones. The link holds its token, so
  it goes only to the account's owner.
- ``operator list-keys|retire-key|unfreeze-key|fill-abandoned-nonces`` (``cli_operator_keys.py``) and ``treasury
  create-safe|setup-transactions|execute-setup|status`` (``cli_treasury.py``): the refund and sweeper keys and the
  treasury Safe that tops them up (PLAN.md section 5.1, 2026-10-13). ``reconcile`` prints the key report too.
"""

import asyncio
from datetime import timedelta
from pathlib import Path
import re
from typing import Annotated

import httpx
import typer

from app.cli_operator_keys import build_operator_key_report, format_operator_key_report, operator_keys_application
from app.cli_treasury import treasury_application
from app.config import read_logging_settings
from app.infrastructure.evm_rpc_api.errors import EvmRpcUnavailableError
from app.infrastructure.monero_sidecar_api.configuration import monero_sidecar_is_configured
from app.lifespan.composition import open_cli_chain_resources, open_cli_database, open_cli_invoice_resources
from app.payments.accounts.passkey_enrollment_link_service import PasskeyEnrollmentLinkService
from app.payments.chain_watcher.heartbeat_service import WatcherHeartbeatService
from app.payments.chain_watcher.host_operator_alerts import (
    HOST_OPERATOR_ALERT_DETAIL_MAXIMUM_LENGTH,
    HOST_OPERATOR_ALERT_DETAIL_PATTERN,
    HostOperatorAlertKind,
    build_host_operator_alert_finding,
)
from app.payments.chain_watcher.monero_reconciliation import check_monero_scanner
from app.payments.chain_watcher.operator_alert_recorder import (
    HOST_OPERATOR_ALERT_PROBLEM_CLEARED_AFTER,
    OperatorAlertRecorder,
)
from app.payments.chain_watcher.reconciliation_service import ReconciliationService
from app.payments.chain_watcher.types import ReconciliationCheckResult
from app.payments.chains.configuration import read_enabled_chains
from app.payments.chains.types import EvmChainConfiguration, MoneroChainConfiguration
from app.payments.invoices.chain_readings_service import ChainReadingsService
from app.payments.invoices.configuration import read_invoice_settings
from app.payments.invoices.deposit_recovery_service import DepositRecoveryService, read_logged_invoice_creations
from app.payments.invoices.service import InvoiceService
from app.payments.invoices.types import DepositRecoveryRequest, RecoveredDeposit
from app.payments.settlements.service import SettlementService
from app.payments.static_deposit_addresses.readings_service import StaticDepositReadingsService
from app.shared.amounts import format_base_units
from app.shared.errors import AppError
from app.shared.logging import configure_logging

cli_application = typer.Typer(no_args_is_help=True, add_completion=False)
refunds_application = typer.Typer(no_args_is_help=True, help="Refunds that wait for the owner (needs_owner).")
cli_application.add_typer(refunds_application, name="refunds")
cli_application.add_typer(operator_keys_application, name="operator")
cli_application.add_typer(treasury_application, name="treasury")
TRANSACTION_HASH_PATTERN = r"^0x[0-9a-fA-F]{64}$"
# Keyless Token Lists file per chain id (856 tokens on Polygon, 2026-10-11).
COINGECKO_TOKEN_LIST_URLS = {137: "https://tokens.coingecko.com/polygon-pos/all.json"}
# Where the frontend's build picks them up; the pages load `/static/coin_logos/<symbol lowercased>.png`.
COIN_LOGOS_DIRECTORY = Path(__file__).resolve().parents[2] / "frontend" / "public" / "coin_logos"
# Logos the token list cannot give as a PNG (checked 2026-10-11): the native coin has no token address (the list's
# WPOL entry shows the retired MATIC mark), USDT0's is a JPEG and WETH's a WebP. These are CoinGecko's images of the
# same coins (POL, Tether, WETH).
COIN_LOGO_URL_OVERRIDES = {
    "POL": "https://assets.coingecko.com/coins/images/32440/large/polygon.png",
    "USDT": "https://assets.coingecko.com/coins/images/325/large/Tether.png",
    "WETH": "https://assets.coingecko.com/coins/images/2518/large/weth.png",
}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
LOGO_DOWNLOAD_TIMEOUT = httpx.Timeout(30.0)


async def sweep_invoice(invoice_id: str, *, skipped_asset_symbols: list[str]) -> None:
    try:
        async with (
            open_cli_invoice_resources() as resources,
            resources.database.session_factory() as session,
            session.begin(),
        ):
            invoice_service = InvoiceService.on_session(
                session, redis=resources.redis_client.client, wiring=resources.invoice_wiring
            )
            recorded = await invoice_service.sweep_invoice_by_hand(
                invoice_id, skipped_asset_symbols=skipped_asset_symbols
            )
    except AppError as error:
        typer.echo(f"refused: {error.message}", err=True)
        raise typer.Exit(1) from error
    if not recorded:
        typer.echo(f"a sweep of {invoice_id} is already under way")
        return
    skipped_note = f" without {', '.join(skipped_asset_symbols)}" if skipped_asset_symbols else ""
    typer.echo(f"sweep of {invoice_id}{skipped_note} recorded")


async def sweep_static_deposit_address(
    static_deposit_address_id: str, *, skipped_asset_symbols: list[str], clears_skipped_tokens: bool
) -> None:
    try:
        async with (
            open_cli_invoice_resources() as resources,
            resources.database.session_factory() as session,
            session.begin(),
        ):
            readings_service = StaticDepositReadingsService.on_session(
                session, redis=resources.redis_client.client, wiring=resources.invoice_wiring
            )
            recorded = await readings_service.sweep_by_hand(
                static_deposit_address_id,
                skipped_asset_symbols=skipped_asset_symbols,
                clears_skipped_tokens=clears_skipped_tokens,
            )
    except AppError as error:
        typer.echo(f"refused: {error.message}", err=True)
        raise typer.Exit(1) from error
    if not recorded:
        typer.echo(f"a sweep of {static_deposit_address_id} is already under way")
        return
    skipped_note = f" without {', '.join(skipped_asset_symbols)}" if skipped_asset_symbols else ""
    typer.echo(f"sweep of {static_deposit_address_id}{skipped_note} recorded")


async def list_refunds_needing_owner() -> None:
    async with open_cli_invoice_resources() as resources, resources.database.session_factory() as session:
        chains_service = resources.invoice_wiring.chains_service
        settlement_service = SettlementService.on_session(
            session, operator_key_addresses=resources.invoice_wiring.operator_key_addresses
        )
        for refund in await settlement_service.list_refunds_needing_owner():
            asset = chains_service.get_asset(refund.chain_key, symbol=refund.asset_symbol)
            amount = format_base_units(refund.amount_base_units, decimals=asset.decimals)
            typer.echo(
                f"{refund.operation_id}\t{refund.status}\t{refund.invoice_id}\t{amount} {refund.asset_symbol} to "
                f"{refund.recipient_address}"
                f"\tnonce {refund.nonce} of {refund.operator_address}\t{refund.created_at.isoformat()}"
            )
            for transaction_hash in refund.attempt_transaction_hashes:
                typer.echo(f"\tattempt {transaction_hash}")


async def settle_refund_needing_owner(
    operation_id: int, *, paid_transaction_hash: str | None, nonce_taking_transaction_hash: str | None
) -> None:
    try:
        async with (
            open_cli_invoice_resources() as resources,
            resources.database.session_factory() as session,
            session.begin(),
        ):
            readings_service = ChainReadingsService.on_session(
                session, redis=resources.redis_client.client, wiring=resources.invoice_wiring
            )
            operation = await readings_service.settle_refund_needing_owner(
                operation_id,
                paid_transaction_hash=paid_transaction_hash,
                nonce_taking_transaction_hash=nonce_taking_transaction_hash,
            )
    except AppError as error:
        typer.echo(f"refused: {error.message}", err=True)
        raise typer.Exit(1) from error
    except EvmRpcUnavailableError as error:
        typer.echo(f"refused: the chain did not answer ({error}); nothing changed, try again in a minute", err=True)
        raise typer.Exit(1) from error
    typer.echo(f"refund {operation.operation_id} of {operation.invoice_id} is now {operation.status.value}")


async def record_stray_payment_from_transaction(transaction_hash: str, *, chain_key: str) -> None:
    """Thin wrapper over ``ChainReadingsService.record_stray_payment_from_transaction`` (INC-001)."""
    try:
        async with (
            open_cli_invoice_resources() as resources,
            resources.database.session_factory() as session,
            session.begin(),
        ):
            readings_service = ChainReadingsService.on_session(
                session, redis=resources.redis_client.client, wiring=resources.invoice_wiring
            )
            invoice_ids = await readings_service.record_stray_payment_from_transaction(
                transaction_hash, chain_key=chain_key
            )
    except AppError as error:
        typer.echo(f"refused: {error.message}", err=True)
        raise typer.Exit(1) from error
    except EvmRpcUnavailableError as error:
        typer.echo(f"refused: the chain did not answer ({error}); nothing changed, try again in a minute", err=True)
        raise typer.Exit(1) from error
    typer.echo(f"stray payment recorded on {', '.join(invoice_ids)}; it goes back by itself where the rules allow")


def echo_recovered_deposit(recovered_deposit: RecoveredDeposit) -> bool:
    """Print what was found; False when the predicted address is not the one the log named."""
    typer.echo(
        f"{recovered_deposit.invoice_id}\t{recovered_deposit.chain_key}\tdeposit {recovered_deposit.deposit_address}"
        f"\tdestination {recovered_deposit.destination_address}"
    )
    if not recovered_deposit.matches_logged_deposit_address:
        typer.echo(
            f"\tMISMATCH: the log names {recovered_deposit.logged_deposit_address}; another PAY_INVOICE_SALT_SEED, or "
            "an invoice made before the seed (its salt is only in Postgres and the backups)"
        )
        return False
    for balance in recovered_deposit.balances:
        amount = (
            "unreadable"
            if balance.balance_base_units is None
            else format_base_units(balance.balance_base_units, decimals=balance.decimals)
        )
        typer.echo(f"\tbalance\t{balance.asset_symbol}\t{amount}")
    if not any(balance.balance_base_units for balance in recovered_deposit.balances):
        typer.echo("\tnothing to sweep")
        return True
    token_list = ",".join(recovered_deposit.sweep_token_addresses)
    typer.echo(f"\tsweep to\t{recovered_deposit.factory_address}")
    typer.echo(f"\tsweep data\t0x{recovered_deposit.sweep_call_data.hex()}")
    typer.echo(
        f"\tsweep with\tcast send {recovered_deposit.factory_address} "
        f"'sweepToDestination(bytes32,address,address[])' 0x{recovered_deposit.invoice_salt.hex()} "
        f"{recovered_deposit.destination_address} '[{token_list}]' --rpc-url <rpc-url> --account <keystore-account>"
    )
    return True


async def recover_deposits(recovery_requests: list[DepositRecoveryRequest]) -> bool:
    """True when every deposit was found and matched its log line."""
    all_recovered = True
    async with open_cli_chain_resources() as invoice_wiring:
        recovery_service = DepositRecoveryService(invoice_wiring)
        for recovery_request in recovery_requests:
            try:
                recovered_deposit = await recovery_service.recover_deposit(
                    recovery_request.invoice_id,
                    destination_address=recovery_request.destination_address,
                    chain_key=recovery_request.chain_key,
                    logged_deposit_address=recovery_request.deposit_address,
                )
            except AppError as error:
                typer.echo(f"{recovery_request.invoice_id}\trefused: {error.message}", err=True)
                all_recovered = False
                continue
            except EvmRpcUnavailableError as error:
                typer.echo(f"{recovery_request.invoice_id}\tthe chain did not answer ({error})", err=True)
                all_recovered = False
                continue
            all_recovered = echo_recovered_deposit(recovered_deposit) and all_recovered
    return all_recovered


def download_coin_logos(chain_key: str) -> None:
    chain = next((chain for chain in read_enabled_chains() if chain.key == chain_key), None)
    if chain is None:
        raise typer.BadParameter(f"{chain_key} is not an enabled chain (PAY_ENABLED_CHAINS)", param_hint="--chain")
    if not isinstance(chain, EvmChainConfiguration):
        raise typer.BadParameter(
            f"{chain_key} is a Monero chain: it has no token list to fetch logos from", param_hint="--chain"
        )
    token_list_url = COINGECKO_TOKEN_LIST_URLS.get(chain.chain_id)
    if token_list_url is None:
        raise typer.BadParameter(f"no token list is known for chain id {chain.chain_id}", param_hint="--chain")
    with httpx.Client(timeout=LOGO_DOWNLOAD_TIMEOUT, follow_redirects=True) as http_client:
        token_list_response = http_client.get(token_list_url)
        token_list_response.raise_for_status()
        token_by_address = {
            str(token["address"]).lower(): token
            for token in token_list_response.json()["tokens"]
            if token.get("chainId") == chain.chain_id
        }
        COIN_LOGOS_DIRECTORY.mkdir(parents=True, exist_ok=True)
        for asset in chain.assets:
            token = token_by_address.get(asset.token_address.lower()) if asset.token_address is not None else None
            if asset.token_address is not None and token is None:
                typer.echo(f"refused: {asset.symbol} {asset.token_address} is not in {token_list_url}", err=True)
                raise typer.Exit(1)
            if token is not None and token.get("decimals") != asset.decimals:
                typer.echo(
                    f"refused: {asset.symbol} has {token.get('decimals')} decimals in the token list, "
                    f"{asset.decimals} in chains.toml",
                    err=True,
                )
                raise typer.Exit(1)
            logo_url = COIN_LOGO_URL_OVERRIDES.get(asset.symbol) or (
                str(token["logoURI"]).replace("/thumb/", "/large/") if token is not None else None
            )
            if logo_url is None:
                typer.echo(f"refused: no logo source for {asset.symbol}; add one to COIN_LOGO_URL_OVERRIDES", err=True)
                raise typer.Exit(1)
            logo_response = http_client.get(logo_url)
            logo_response.raise_for_status()
            if not logo_response.content.startswith(PNG_SIGNATURE):
                typer.echo(f"refused: {logo_url} is not a PNG; add a PNG source to COIN_LOGO_URL_OVERRIDES", err=True)
                raise typer.Exit(1)
            logo_path = COIN_LOGOS_DIRECTORY / f"{asset.symbol.lower()}.png"
            logo_path.write_bytes(logo_response.content)
            typer.echo(f"{asset.symbol}\t{len(logo_response.content)} bytes\t{logo_url}")


@cli_application.command("download-coin-logos")
def download_coin_logos_command(
    chain: Annotated[str, typer.Option(help="A chains.toml key among PAY_ENABLED_CHAINS.")] = "polygon",
) -> None:
    """Write each configured asset's logo to frontend/public/coin_logos/ from CoinGecko's token list (by hand)."""
    download_coin_logos(chain)


@cli_application.command("recover-deposit")
def recover_deposit_command(
    invoice_id: Annotated[str | None, typer.Option(help="inv_...", show_default=False)] = None,
    destination: Annotated[
        str | None, typer.Option(help="The invoice's destination address.", show_default=False)
    ] = None,
    chain: Annotated[str, typer.Option(help="A chains.toml key among PAY_ENABLED_CHAINS.")] = "polygon",
    from_log: Annotated[
        typer.FileText | None,
        typer.Option(help="The API's JSON log (docker logs pay-7331-api), - for stdin.", show_default=False),
    ] = None,
) -> None:
    """Find a deposit from its invoice id, destination and PAY_INVOICE_SALT_SEED; print its balances and the sweep
    call. Read-only, no database."""
    if from_log is not None:
        if invoice_id is not None or destination is not None:
            raise typer.BadParameter(
                "give either --from-log or --invoice-id with --destination", param_hint="--from-log"
            )
        recovery_requests = read_logged_invoice_creations(from_log)
        if not recovery_requests:
            typer.echo("no invoice_create line naming a deposit address in that log", err=True)
            raise typer.Exit(1)
    elif invoice_id is not None and destination is not None:
        recovery_requests = [
            DepositRecoveryRequest(invoice_id=invoice_id, chain_key=chain, destination_address=destination)
        ]
    else:
        raise typer.BadParameter("give --invoice-id and --destination, or --from-log", param_hint="--invoice-id")
    if not asyncio.run(recover_deposits(recovery_requests)):
        raise typer.Exit(1)


@cli_application.command("sweep-invoice")
def sweep_invoice_command(
    invoice_id: Annotated[str, typer.Argument(help="inv_...")],
    skip_token: Annotated[
        list[str] | None,
        typer.Option(
            help="A configured token to leave out, e.g. a frozen USDC (repeatable); the native coin always moves."
        ),
    ] = None,
) -> None:
    """Clear ``sweep_blocked`` and record a sweep of the invoice's deposit, without the ``--skip-token`` ones."""
    asyncio.run(sweep_invoice(invoice_id, skipped_asset_symbols=skip_token or []))


@cli_application.command("sweep-static-deposit-address")
def sweep_static_deposit_address_command(
    static_deposit_address_id: Annotated[str, typer.Argument(help="sda_...")],
    skip_token: Annotated[
        list[str] | None,
        typer.Option(
            help="A configured token to leave out, e.g. a frozen USDC (repeatable); kept for every later sweep of "
            "the address. The native coin always moves."
        ),
    ] = None,
    clear_skipped_tokens: Annotated[
        bool, typer.Option(help="Forget the tokens earlier --skip-token runs left out, before adding these.")
    ] = False,
) -> None:
    """Clear a static deposit address's ``sweep_blocked`` and record a sweep, without its skipped tokens."""
    asyncio.run(
        sweep_static_deposit_address(
            static_deposit_address_id,
            skipped_asset_symbols=skip_token or [],
            clears_skipped_tokens=clear_skipped_tokens,
        )
    )


@cli_application.command("record-stray-payment-from-transaction")
def record_stray_payment_from_transaction_command(
    transaction_hash: Annotated[str, typer.Argument(help="A final transaction that sent another coin to a deposit.")],
    chain: Annotated[str, typer.Option(help="The chain key in chains.toml.")] = "polygon",
) -> None:
    """Record money in a coin an invoice did not ask for from its transaction, so it goes back by itself."""
    if not re.fullmatch(TRANSACTION_HASH_PATTERN, transaction_hash):
        raise typer.BadParameter("a transaction hash is 0x and 64 hex digits", param_hint="TRANSACTION_HASH")
    asyncio.run(record_stray_payment_from_transaction(transaction_hash.lower(), chain_key=chain))


@refunds_application.command("list-needing-owner")
def list_refunds_needing_owner_command() -> None:
    asyncio.run(list_refunds_needing_owner())


@refunds_application.command("mark-paid")
def mark_refund_paid_command(
    operation_id: Annotated[int, typer.Argument(help="The id `refunds list-needing-owner` shows.")],
    transaction_hash: Annotated[
        str, typer.Option(help="The finalized transaction that paid the recipient.", show_default=False)
    ],
) -> None:
    if not re.fullmatch(TRANSACTION_HASH_PATTERN, transaction_hash):
        raise typer.BadParameter("a transaction hash is 0x and 64 hex digits", param_hint="--transaction-hash")
    asyncio.run(
        settle_refund_needing_owner(
            operation_id, paid_transaction_hash=transaction_hash.lower(), nonce_taking_transaction_hash=None
        )
    )


@refunds_application.command("mark-unpaid")
def mark_refund_unpaid_command(
    operation_id: Annotated[int, typer.Argument(help="The id `refunds list-needing-owner` shows.")],
    nonce_taken_by: Annotated[
        str | None,
        typer.Option(
            help="The finalized operator transaction, none of the listed attempts, that used the refund's nonce. "
            "Required once the refund signed an attempt; a waiting_for_float refund with no attempt takes none.",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Mark a refund failed, freeing its reserved amount, once another transaction provably took its nonce."""
    if nonce_taken_by is not None and not re.fullmatch(TRANSACTION_HASH_PATTERN, nonce_taken_by):
        raise typer.BadParameter("a transaction hash is 0x and 64 hex digits", param_hint="--nonce-taken-by")
    asyncio.run(
        settle_refund_needing_owner(
            operation_id,
            paid_transaction_hash=None,
            nonce_taking_transaction_hash=nonce_taken_by.lower() if nonce_taken_by is not None else None,
        )
    )


async def mint_passkey_enrollment_link(username: str, *, lifetime: timedelta) -> None:
    try:
        async with (
            open_cli_database() as database,
            database.session_factory() as session,
            session.begin(),
        ):
            minted_link = await PasskeyEnrollmentLinkService.on_session(session).mint_passkey_enrollment_link(
                username, lifetime=lifetime
            )
    except AppError as error:
        typer.echo(f"refused: {error.message}", err=True)
        raise typer.Exit(1) from error
    typer.echo(minted_link.link.get_secret_value())


@cli_application.command("mint-passkey-enrollment-link")
def mint_passkey_enrollment_link_command(
    username: Annotated[str, typer.Argument(help="The account's username.")],
    expires_minutes: Annotated[int, typer.Option(min=1, max=1440, help="How long the link works.")] = 15,
) -> None:
    """Print a one-time link that adds a passkey to the account without signing in (account recovery)."""
    asyncio.run(mint_passkey_enrollment_link(username, lifetime=timedelta(minutes=expires_minutes)))


def format_findings_table(check_result: ReconciliationCheckResult) -> list[str]:
    """The findings' columns as a plain fixed-width table, the columns in first-seen order."""
    column_names: list[str] = []
    for finding in check_result.findings:
        column_names.extend(name for name in finding.columns if name not in column_names)
    rows = [[finding.columns.get(name, "") for name in column_names] for finding in check_result.findings]
    widths = [max(len(name), *(len(row[index]) for row in rows)) for index, name in enumerate(column_names)]
    return [
        "  " + "  ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=True)).rstrip()
        for cells in [column_names, ["-" * width for width in widths], *rows]
    ]


async def reconcile(chain_key: str) -> bool:
    """Print every check's summary and findings; True when nothing needs the operator."""
    async with open_cli_invoice_resources() as resources, resources.database.session_factory() as session:
        chain = resources.invoice_wiring.chains_service.get_chain(chain_key)
        check_results = await ReconciliationService.on_session(
            session, redis=resources.redis_client.client, wiring=resources.invoice_wiring
        ).run_every_check(chain)
        if isinstance(chain, MoneroChainConfiguration):
            check_results.insert(
                0,
                await check_monero_scanner(
                    chain,
                    scanner_reader=resources.invoice_wiring.monero_sidecar,
                    heartbeat_service=WatcherHeartbeatService.on_redis(resources.redis_client.client),
                    sidecar_is_configured=monero_sidecar_is_configured(),
                ),
            )
        operator_key_report = (
            await build_operator_key_report(resources, chain=chain)
            if isinstance(chain, EvmChainConfiguration) and chain.key in resources.invoice_wiring.operator_key_addresses
            else None
        )
    for check_result in check_results:
        verdict = (
            f"{len(check_result.findings)} need attention"
            if check_result.findings
            else f"warning, {check_result.warning}"
            if check_result.warning
            else "clean"
        )
        typer.echo(f"== {check_result.check_name}: {verdict} ({check_result.summary})")
        if check_result.findings:
            for line in format_findings_table(check_result):
                typer.echo(line)
            for finding in check_result.findings:
                typer.echo(f"  - {finding.message}")
    if operator_key_report is not None:
        verdict = f"{len(operator_key_report.findings)} need attention" if operator_key_report.findings else "clean"
        typer.echo(f"== operator_keys: {verdict}")
        for line in format_operator_key_report(operator_key_report):
            typer.echo(f"  {line}")
    return not any(check_result.findings for check_result in check_results) and not (
        operator_key_report is not None and operator_key_report.findings
    )


@cli_application.command("reconcile")
def reconcile_command(
    chain: Annotated[str, typer.Option(help="A chains.toml key among PAY_ENABLED_CHAINS.")] = "polygon",
) -> None:
    """Read-only check of everything that needs the operator; exit 1 when anything does."""
    if chain not in {enabled_chain.key for enabled_chain in read_enabled_chains()}:
        raise typer.BadParameter(f"{chain} is not an enabled chain (PAY_ENABLED_CHAINS)", param_hint="--chain")
    if not asyncio.run(reconcile(chain)):
        raise typer.Exit(1)


async def record_host_operator_alert(kind: HostOperatorAlertKind, *, detail: str | None) -> bool:
    """Record the alert; False when no operator account exists to record it on."""
    async with open_cli_database() as database:
        recording = await OperatorAlertRecorder(
            session_factory=database.session_factory,
            operator_alert_account_id=read_invoice_settings().operator_alert_account_id,
            problem_cleared_after=HOST_OPERATOR_ALERT_PROBLEM_CLEARED_AFTER,
        ).record_new_findings([build_host_operator_alert_finding(kind, detail=detail)], chain_key=None)
    if not recording.alerted_problem_keys:
        typer.echo(f"{kind.value}: already alerted and still open, no reminder due")
        return True
    if not recording.operator_account_found:
        typer.echo(
            f"{kind.value}: logged only, PAY_OPERATOR_ACCOUNT_IDS is empty or its first id names no account", err=True
        )
        return False
    typer.echo(f"{kind.value}: operator.alert recorded on the operator account")
    return True


@cli_application.command("record-operator-alert")
def record_operator_alert_command(
    kind: Annotated[HostOperatorAlertKind, typer.Option(help="What failed on the host.")],
    detail: Annotated[
        str | None, typer.Option(help='systemd\'s result words, e.g. "result=exit-code status=1"; no free text.')
    ] = None,
) -> None:
    """Record an operator.alert from the host (its backup units): once per problem, then at the reminder cadence."""
    if detail is not None and (
        len(detail) > HOST_OPERATOR_ALERT_DETAIL_MAXIMUM_LENGTH
        or not re.fullmatch(HOST_OPERATOR_ALERT_DETAIL_PATTERN, detail)
    ):
        raise typer.BadParameter(
            f"space-separated key=value words of letters, digits and =._:-, at most "
            f"{HOST_OPERATOR_ALERT_DETAIL_MAXIMUM_LENGTH} characters",
            param_hint="--detail",
        )
    if not asyncio.run(record_host_operator_alert(kind, detail=detail)):
        raise typer.Exit(1)


if __name__ == "__main__":
    configure_logging(read_logging_settings().log_level)
    cli_application()
