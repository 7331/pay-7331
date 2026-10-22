"""EvmDepositTargets: where a new EVM invoice is paid (PLAN.md section 3), split out of ``service.py``.

The destination is checked (not zero, not the factory, not one of the gateway's own deposits, no code unless allowed)
on every chain the invoice offers, and the chosen chain's factory predicts the deposit clone for it under the invoice's
salt (one ``eth_call``): at creation for one chain, at the payer's pick for several (PLAN.md section 15). A quantity
link checks its destination here too when it is made.
"""

from eth_utils import is_address, to_checksum_address
from pydantic import BaseModel, ConfigDict

from app.infrastructure.evm_rpc_api.client import EvmRpcEndpoint
from app.infrastructure.evm_rpc_api.errors import EvmExecutionRevertedError, EvmRpcUnavailableError
from app.payments.chains.types import EvmChainConfiguration
from app.payments.invoices.errors import DestinationRefusedError
from app.payments.invoices.repository import InvoiceRepository
from app.payments.invoices.wiring import InvoiceWiring
from app.shared.errors import ServiceUnavailableError

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


class PredictedDeposit(BaseModel):
    """A deposit clone the factory predicted and the `latest` block it was predicted at."""

    model_config = ConfigDict(frozen=True)

    deposit_address: str
    destination_address: str
    latest_block_number: int


class EvmDepositTargets:
    def __init__(self, *, repository: InvoiceRepository, wiring: InvoiceWiring) -> None:
        self.repository = repository
        self.wiring = wiring

    async def check_destination_on_offered_chains(
        self,
        destination_address: str,
        *,
        chains: list[EvmChainConfiguration],
        allow_contract_destination: bool,
        defaulted_chains: bool,
    ) -> tuple[str, list[EvmChainConfiguration]]:
        """The destination checked on every offered chain, so a refusal (zero, a factory, a deposit, a contract) on any
        of them stands, and the chains that passed. A chain that cannot be reached is left out when the gateway filled
        the chains in (``defaulted_chains``) and another passed; otherwise it fails the whole creation."""
        checked_address = ""
        passed_chains: list[EvmChainConfiguration] = []
        unreachable: ServiceUnavailableError | None = None
        for chain in chains:
            try:
                checked_address = await self.check_destination(
                    destination_address, chain=chain, allow_contract_destination=allow_contract_destination
                )
            except ServiceUnavailableError as exc:
                if not defaulted_chains:
                    raise
                unreachable = exc
                continue
            passed_chains.append(chain)
        if not passed_chains and unreachable is not None:
            raise unreachable
        return checked_address, passed_chains

    async def predict_deposit(
        self, destination_address: str, *, chain: EvmChainConfiguration, invoice_salt: bytes
    ) -> PredictedDeposit:
        """The deposit clone the chain's factory predicts for the checked destination under the invoice's salt."""
        factory_client = self.wiring.factory_clients[chain.key]

        async def predict_deposit_on(endpoint: EvmRpcEndpoint) -> tuple[str, int]:
            deposit_address = await factory_client.predict_deposit_address(
                endpoint, invoice_salt=invoice_salt, destination_address=destination_address
            )
            return deposit_address, (await endpoint.read_block_heads()).latest.number

        try:
            deposit_address, latest_block_number = await self.wiring.rpc_clients[chain.key].call_with_failover(
                predict_deposit_on
            )
        except (EvmRpcUnavailableError, EvmExecutionRevertedError) as exc:
            raise ServiceUnavailableError("the chain could not be reached; try again shortly") from exc
        return PredictedDeposit(
            deposit_address=deposit_address,
            destination_address=destination_address,
            latest_block_number=latest_block_number,
        )

    async def check_destination(
        self, destination_address: str, *, chain: EvmChainConfiguration, allow_contract_destination: bool
    ) -> str:
        """Refuse zero, the factory, a deposit address, and one with code unless allowed (PLAN.md section 3)."""
        if not destination_address or not is_address(destination_address):
            raise DestinationRefusedError("destination_address: a 0x address is required")
        address = to_checksum_address(destination_address)
        if address in {ZERO_ADDRESS, chain.factory_address}:
            raise DestinationRefusedError("destination_address: not a destination money can be sent to")
        if await self.wiring.gateway_deposit_address_check(self.repository.session, address):
            raise DestinationRefusedError("destination_address: that is a deposit address of this gateway")
        if not allow_contract_destination:
            try:
                with_code = await self.wiring.rpc_clients[chain.key].call_with_failover(
                    lambda endpoint: endpoint.read_addresses_with_code([address])
                )
            except EvmRpcUnavailableError as exc:
                raise ServiceUnavailableError("the chain could not be reached; try again shortly") from exc
            if with_code:
                raise DestinationRefusedError(
                    f"destination_address: it is a contract on {chain.name}; set allow_contract_destination if it "
                    f"accepts {chain.native_asset.symbol}"
                )
        return address
