"""Find a deposit again without Postgres (``python -m app.cli recover-deposit``; PLAN.md section 3, 2026-10-12).

From an invoice id and its destination (typed in, or read from the API's ``invoice_create`` and ``invoice_deposit_derive`` log lines), the salt is
derived from ``PAY_INVOICE_SALT_SEED`` (deposit_salts.py), the factory's ``predictDepositAddress`` gives the address
(one ``eth_call``, never a local copy of the init code), and each configured asset's balance is read there at
``latest``. Read-only: it sends nothing and writes nothing; the sweep call it builds is for the operator to send.
It needs no database, so it works on an empty or missing one. The Docker log is on the same disk as Postgres, so it
covers a lost database, not a lost disk.
"""

from collections.abc import Iterable
import json
import re

from eth_utils import is_address, to_checksum_address
from pydantic import ValidationError

from app.infrastructure.evm_rpc_api.client import (
    EvmRpcEndpoint,
    decode_uint256,
    encode_native_balance_call,
    encode_token_balance_call,
)
from app.infrastructure.evm_rpc_api.schemas import Multicall3Result
from app.payments.invoices.deposit_salts import derive_invoice_salt
from app.payments.invoices.errors import FactoryNotDeployedError
from app.payments.invoices.log_events import InvoiceCreated, InvoiceDepositDerived
from app.payments.invoices.types import DepositRecoveryRequest, RecoveredDeposit, RecoveredDepositBalance
from app.payments.invoices.wiring import InvoiceWiring
from app.shared.errors import InvalidInputError

INVOICE_ID_PATTERN = r"inv_[0-9A-Za-z]+"


def read_logged_invoice_creations(log_lines: Iterable[str]) -> list[DepositRecoveryRequest]:
    """Every ``invoice_create`` or ``invoice_deposit_derive`` record in ``log_lines`` (``docker logs pay-7331-api``, with or without a compose
    prefix) that names its deposit and destination, first line per invoice id and chain. Other lines, and creation lines from
    before the deposit was logged, are skipped."""
    creations: dict[tuple[str, str], DepositRecoveryRequest] = {}
    for log_line in log_lines:
        json_start = log_line.find("{")
        if json_start < 0:
            continue
        try:
            record = json.loads(log_line[json_start:])
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("operation") not in {
            InvoiceCreated.operation,
            InvoiceDepositDerived.operation,
        }:
            continue
        try:
            creation = DepositRecoveryRequest.model_validate(record)
        except ValidationError:
            continue
        if creation.deposit_address is None:
            continue
        creations.setdefault((creation.invoice_id, creation.chain_key), creation)
    return list(creations.values())


def decode_balance(result: Multicall3Result) -> int | None:
    return decode_uint256(result.return_data) if result.succeeded and len(result.return_data) == 32 else None


class DepositRecoveryService:
    """Built on the process's invoice wiring alone: chains, their RPC clients and factories, and the seed."""

    def __init__(self, wiring: InvoiceWiring) -> None:
        self.wiring = wiring

    async def recover_deposit(
        self, invoice_id: str, *, destination_address: str, chain_key: str, logged_deposit_address: str | None = None
    ) -> RecoveredDeposit:
        if not re.fullmatch(INVOICE_ID_PATTERN, invoice_id):
            raise InvalidInputError("invoice id: inv_ followed by letters and digits")
        if not is_address(destination_address):
            raise InvalidInputError("destination: a 0x address is required")
        destination_address = to_checksum_address(destination_address)
        chain = self.wiring.chains_service.get_evm_chain(chain_key)
        factory_client = self.wiring.factory_clients.get(chain.key)
        if factory_client is None:
            raise FactoryNotDeployedError(f"no deposit factory is configured on {chain.key}")
        invoice_salt = derive_invoice_salt(invoice_id, invoice_salt_seed=self.wiring.invoice_settings.invoice_salt_seed)

        async def predict_and_read_balances(endpoint: EvmRpcEndpoint) -> tuple[str, list[Multicall3Result]]:
            deposit_address = await factory_client.predict_deposit_address(
                endpoint, invoice_salt=invoice_salt, destination_address=destination_address
            )
            balance_calls = [
                encode_native_balance_call(deposit_address)
                if asset.token_address is None
                else encode_token_balance_call(asset.token_address, holder_address=deposit_address)
                for asset in chain.assets
            ]
            return deposit_address, await endpoint.aggregate3(balance_calls, block_hash=None)

        deposit_address, balance_results = await self.wiring.rpc_clients[chain.key].call_with_failover(
            predict_and_read_balances
        )
        balances = [
            RecoveredDepositBalance(
                asset_symbol=asset.symbol,
                token_address=asset.token_address,
                decimals=asset.decimals,
                balance_base_units=decode_balance(result),
            )
            for asset, result in zip(chain.assets, balance_results, strict=True)
        ]
        # Only tokens holding money: a listed token that fails its transfer reverts the whole sweep;
        # the native balance always goes with it.
        sweep_token_addresses = [
            balance.token_address
            for balance in balances
            if balance.token_address is not None and (balance.balance_base_units or 0) > 0
        ]
        return RecoveredDeposit(
            invoice_id=invoice_id,
            chain_key=chain.key,
            factory_address=factory_client.factory_address,
            destination_address=destination_address,
            deposit_address=deposit_address,
            logged_deposit_address=to_checksum_address(logged_deposit_address) if logged_deposit_address else None,
            balances=balances,
            invoice_salt=invoice_salt,
            sweep_token_addresses=sweep_token_addresses,
            sweep_call_data=factory_client.encode_sweep_call(
                invoice_salt=invoice_salt,
                destination_address=destination_address,
                token_addresses=sweep_token_addresses,
            ),
        )
