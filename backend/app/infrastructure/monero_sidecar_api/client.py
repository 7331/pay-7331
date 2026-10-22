"""The xmr-scanner sidecar's HTTP API (PLAN.md section 14; ``xmr-scanner/README.md`` is the contract):

- ``POST /v1/wallet-checks`` ``{"network", "primary_address", "private_view_key"}`` -> ``{"address_valid",
  "view_key_matches"}``
- ``POST /v1/subaddresses`` the same plus ``"major_index"``, ``"minor_index"`` -> ``{"subaddress"}``
- ``GET /healthz`` -> ``{"ok", "chains": [{"chain_key", "ok", "node_count", "healthy_operator_count", "tip_height",
  "confirmed_tip_height", "scanned_height", "last_completed_tick_at", "unusable_wallet_count"}]}``
- ``POST /v1/transaction-checks`` ``{"chain_key", "txid", "invoice_id"}`` -> ``{"outcome", "amount_piconero",
  "confirmations"}``

Each of them answers 400 ``{"error"}`` on input it cannot parse. Short timeouts (5 s) for the key routes and the health
read: a wallet check runs inside a request that holds the account's row. A transaction check asks up to two node
operators, so it gets ``TRANSACTION_CHECK_TIMEOUT``. Request and response bodies are never logged and never put in an
exception (they carry a private view key); a failure logs its route and kind.
"""

from typing import Any

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from app.infrastructure.monero_sidecar_api import log_events
from app.infrastructure.monero_sidecar_api.errors import (
    MoneroSidecarError,
    MoneroSidecarFailureKind,
    MoneroSidecarInputRefusedError,
)
from app.infrastructure.monero_sidecar_api.schemas import (
    MoneroChainHealth,
    MoneroHealthResult,
    MoneroSubaddressResult,
    MoneroTransactionCheck,
    MoneroWalletCheckResult,
)
from app.shared import logging as log

logger = log.get_logger(__name__)

SIDECAR_REQUEST_TIMEOUT = httpx.Timeout(5.0, connect=2.0)
# The sidecar bounds its own check at 20 s (``xmr-scanner/src/transaction_checks.rs``); this leaves it room to answer.
TRANSACTION_CHECK_TIMEOUT = httpx.Timeout(25.0, connect=2.0)
HEALTH_ROUTE = "/healthz"
WALLET_CHECKS_ROUTE = "/v1/wallet-checks"
SUBADDRESSES_ROUTE = "/v1/subaddresses"
TRANSACTION_CHECKS_ROUTE = "/v1/transaction-checks"
HTTP_STATUS_BAD_REQUEST = 400


def build_sidecar_failure(
    description: str, *, route: str, failure_kind: MoneroSidecarFailureKind, http_status: int | None = None
) -> MoneroSidecarError:
    """Logs the failure (route and kind only) and returns the error to raise."""
    log.emit(
        logger,
        event=log_events.MoneroSidecarRequestFailed(
            route=route, failure_kind=failure_kind.value, http_status=http_status
        ),
    )
    return MoneroSidecarError(description, failure_kind=failure_kind)


class MoneroSidecarApiClient:
    """Fills every Monero port of ``app.payments.monero_wallets.types`` (``MoneroSidecar``)."""

    def __init__(self, *, http_client: httpx.AsyncClient, base_url: str) -> None:
        self.http_client = http_client
        self.base_url = base_url.rstrip("/")

    async def request_sidecar[ResultModel: BaseModel](
        self,
        route: str,
        *,
        method: str,
        result_model: type[ResultModel],
        json_body: dict[str, Any] | None = None,
        request_timeout: httpx.Timeout = SIDECAR_REQUEST_TIMEOUT,
    ) -> ResultModel:
        """Raises ``MoneroSidecarInputRefusedError`` on a 400 and ``MoneroSidecarError`` on any other failure."""
        try:
            response = await self.http_client.request(
                method, f"{self.base_url}{route}", json=json_body, timeout=request_timeout
            )
        except httpx.HTTPError as transport_error:
            raise build_sidecar_failure(
                "the Monero sidecar is unreachable", route=route, failure_kind=MoneroSidecarFailureKind.UNREACHABLE
            ) from transport_error
        if response.status_code == HTTP_STATUS_BAD_REQUEST:
            raise MoneroSidecarInputRefusedError("the Monero sidecar could not parse the request")
        if not response.is_success:
            raise build_sidecar_failure(
                f"the Monero sidecar answered HTTP {response.status_code}",
                route=route,
                failure_kind=MoneroSidecarFailureKind.HTTP_STATUS,
                http_status=response.status_code,
            )
        try:
            return result_model.model_validate_json(response.content)
        except ValidationError:
            # Not chained: a validation error's text quotes the body it read.
            raise build_sidecar_failure(
                "the Monero sidecar sent an unreadable answer",
                route=route,
                failure_kind=MoneroSidecarFailureKind.UNREADABLE,
            ) from None

    async def check_wallet(
        self, primary_address: str, *, private_view_key: SecretStr, network: str
    ) -> MoneroWalletCheckResult:
        return await self.request_sidecar(
            WALLET_CHECKS_ROUTE,
            method="POST",
            result_model=MoneroWalletCheckResult,
            json_body={
                "network": network,
                "primary_address": primary_address,
                "private_view_key": private_view_key.get_secret_value(),
            },
        )

    async def derive_subaddress(
        self,
        primary_address: str,
        *,
        private_view_key: SecretStr,
        network: str,
        major_index: int,
        minor_index: int,
    ) -> str:
        subaddress_result = await self.request_sidecar(
            SUBADDRESSES_ROUTE,
            method="POST",
            result_model=MoneroSubaddressResult,
            json_body={
                "network": network,
                "primary_address": primary_address,
                "private_view_key": private_view_key.get_secret_value(),
                "major_index": major_index,
                "minor_index": minor_index,
            },
        )
        return subaddress_result.subaddress

    async def read_chain_health(self, chain_key: str) -> MoneroChainHealth:
        """The chain's entry of ``/healthz``; ``chain_not_followed`` when the sidecar does not scan the chain or has not
        completed a tick of it yet. Whether the scan is stale is the caller's call (``monero_scanner_health.py``)."""
        health_result = await self.request_sidecar(HEALTH_ROUTE, method="GET", result_model=MoneroHealthResult)
        for chain_entry in health_result.chains:
            if (
                chain_entry.chain_key == chain_key
                and chain_entry.tip_height is not None
                and chain_entry.last_completed_tick_at is not None
            ):
                return MoneroChainHealth(
                    chain_key=chain_key,
                    tip_height=chain_entry.tip_height,
                    confirmed_tip_height=chain_entry.confirmed_tip_height,
                    scanned_height=chain_entry.scanned_height,
                    last_completed_tick_at=chain_entry.last_completed_tick_at,
                    unusable_wallet_count=chain_entry.unusable_wallet_count,
                    healthy_operator_count=chain_entry.healthy_operator_count,
                    confirmed_tip_lagging_since=chain_entry.confirmed_tip_lagging_since,
                )
        raise build_sidecar_failure(
            "the Monero sidecar has no tip for this chain",
            route=HEALTH_ROUTE,
            failure_kind=MoneroSidecarFailureKind.CHAIN_NOT_FOLLOWED,
        )

    async def check_transaction(self, chain_key: str, *, txid: str, invoice_id: str) -> MoneroTransactionCheck:
        return await self.request_sidecar(
            TRANSACTION_CHECKS_ROUTE,
            method="POST",
            result_model=MoneroTransactionCheck,
            json_body={"chain_key": chain_key, "txid": txid, "invoice_id": invoice_id},
            request_timeout=TRANSACTION_CHECK_TIMEOUT,
        )
