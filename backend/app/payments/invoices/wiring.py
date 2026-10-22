"""The long-lived collaborators every invoice service is built from, opened once per process by app/lifespan."""

from pydantic import BaseModel, ConfigDict

from app.infrastructure.chainlink_api.client import ChainlinkApiClient
from app.infrastructure.deposit_factory_api.client import DepositFactoryApiClient
from app.infrastructure.evm_rpc_api.client import EvmRpcClient
from app.infrastructure.ip_api.client import IpApiClient
from app.payments.accounts.types import GatewayDepositAddressCheck
from app.payments.chains.service import ChainsService
from app.payments.invoices.configuration import InvoiceSettings
from app.payments.invoices.types import InvoiceCallbackUrlCheck
from app.payments.monero_wallets.types import MoneroSidecar
from app.payments.settlements.operator_keys import OperatorKeyAddresses
from app.payments.sign_up_screening.configuration import ScreeningSettings


class InvoiceWiring(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    chains_service: ChainsService
    rpc_clients: dict[str, EvmRpcClient]
    # Only chains whose factory is deployed.
    factory_clients: dict[str, DepositFactoryApiClient]
    chainlink_clients: dict[str, ChainlinkApiClient]
    ip_api_client: IpApiClient | None
    # The Monero scanner sidecar (PLAN.md section 14), one client for every port: wallet checks, subaddresses, the
    # tip and txid checks. ``UnavailableMoneroSidecar`` with no Monero chain enabled or ``PAY_MONERO_SIDECAR_URL`` empty.
    monero_sidecar: MoneroSidecar
    # Seals and opens accounts' private view keys (monero_wallets/secret_sealing.py).
    monero_private_view_key_encryption_key: bytes
    # Each chain's refund and sweeper key addresses (no keys); chains with no refund key are absent.
    operator_key_addresses: dict[str, OperatorKeyAddresses]
    invoice_settings: InvoiceSettings
    screening_settings: ScreeningSettings
    public_base_url: str
    # None in a process that makes no account invoice with a callback; then a ``callback_url`` is refused.
    invoice_callback_url_check: InvoiceCallbackUrlCheck | None = None
    # Invoice and static deposit addresses (static_deposit_addresses/service.py): refused as an invoice destination
    # and as a payout address.
    gateway_deposit_address_check: GatewayDepositAddressCheck
