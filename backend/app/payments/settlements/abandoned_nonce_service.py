"""AbandonedNonceService: a frozen key's nonces handed out but never used (2026-10-13; PLAN.md section 5.1).

A frozen key's unmined sweeps and top-ups end without their nonce being used (``runner_service.py``), and the
allocator's ``next_nonce`` stays past it, since a nonce is never handed out twice. Once the key signs again (unfrozen,
or retired and returning what it holds to the Safe), every later transaction would wait behind that gap forever. An
abandoned nonce is at or above the key's ``latest`` count (the highest any endpoint answers: a lagging one's lower
count would look like a gap), below its ``next_nonce`` and held by no active operation (that one is still the
runner's).

- ``find_abandoned_nonces``: read-only. ``operator unfreeze-key`` and ``operator retire-key`` refuse a frozen key that
  has one.
- ``fill_abandoned_nonces`` (``operator fill-abandoned-nonces``): each one is used by a 0-value transfer of the key to
  itself, 21,000 gas, its fees over every stored attempt at that nonce by the replacement margin (an old attempt may
  still sit in the pools), under the chain's fee cap, sent to every endpoint. It moves nothing, and it is below
  ``next_nonce``, so the leak check never takes it for a use elsewhere. It is not stored before it is sent, unlike an
  operation's attempt: a lost one is simply filled again by the next run.

The owner's CLI fills, not the runner (chosen 2026-10-13): a frozen key may have leaked, so whether it signs again is
the owner's decision, and the runner keeps its rule that a frozen key signs nothing; no operation kind, schema change
or alert is needed for transfers that move nothing.
"""

from collections.abc import Callable

from eth_account.signers.local import LocalAccount
from eth_typing import HexStr
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.evm_rpc_api.client import EvmRpcClient, EvmRpcEndpoint
from app.infrastructure.evm_rpc_api.errors import EvmRpcUnavailableError
from app.payments.chains.types import EvmChainConfiguration
from app.payments.settlements.errors import OperatorKeyHasAbandonedNoncesError
from app.payments.settlements.log_events import AbandonedNonceFillBroadcast
from app.payments.settlements.operation_transactions import native_transfer_gas_limit_of, raised_fee
from app.payments.settlements.repository import SettlementRepository
from app.shared import logging as log

logger = log.get_logger(__name__)

type SessionFactory = Callable[[], AsyncSession]


class AbandonedNonceFill(BaseModel):
    model_config = ConfigDict(frozen=True)

    nonce: int
    transaction_hash: str
    accepting_endpoints: int


class AbandonedNonceService:
    def __init__(
        self, *, chain: EvmChainConfiguration, session_factory: SessionFactory, rpc_client: EvmRpcClient
    ) -> None:
        self.chain = chain
        self.session_factory = session_factory
        self.rpc_client = rpc_client

    async def read_latest_transaction_count(self, operator_address: str) -> int:
        (latest_transaction_count,) = await self.rpc_client.read_highest_transaction_counts(
            [operator_address], block_tag="latest"
        )
        return latest_transaction_count

    async def find_abandoned_nonces(self, operator_address: str) -> list[int]:
        """The chain first, Postgres after: a nonce handed out between the two reads is in the rows read."""
        latest_transaction_count = await self.read_latest_transaction_count(operator_address)
        async with self.session_factory() as session:
            repository = SettlementRepository(session=session)
            next_nonce_by_address = await repository.read_next_nonces(
                self.chain.key, operator_addresses=[operator_address]
            )
            held_nonces = await repository.list_active_operation_nonces(
                self.chain.key, operator_address=operator_address
            )
        next_nonce = next_nonce_by_address.get(operator_address)
        if next_nonce is None:
            return []
        return [nonce for nonce in range(latest_transaction_count, next_nonce) if nonce not in held_nonces]

    async def refuse_when_abandoned_nonces(self, operator_address: str) -> None:
        """Raises ``OperatorKeyHasAbandonedNoncesError`` naming the nonces and the command that fills them."""
        abandoned_nonces = await self.find_abandoned_nonces(operator_address)
        if not abandoned_nonces:
            return
        raise OperatorKeyHasAbandonedNoncesError(
            f"nonces {', '.join(str(nonce) for nonce in abandoned_nonces)} of {operator_address} were handed out but "
            "never used (its sweeps or top-ups ended while it was frozen); every later transaction would wait behind "
            f"them. Fill them first: `operator fill-abandoned-nonces --chain {self.chain.key} --address "
            f"{operator_address}`, then run this again"
        )

    async def fill_abandoned_nonces(self, account: LocalAccount) -> list[AbandonedNonceFill]:
        """A 0-value transfer to itself at each abandoned nonce (module docstring), sent; not waited for."""
        abandoned_nonces = await self.find_abandoned_nonces(account.address)
        if not abandoned_nonces:
            return []

        async def read_fee_inputs(endpoint: EvmRpcEndpoint) -> tuple[int, int, int]:
            block_heads = await endpoint.read_block_heads()
            # The node's estimate, since a rollup's includes the L1 data fee (``native_transfer_gas_limit_of``).
            estimated_gas = await endpoint.estimate_gas(
                sender_address=account.address, target_address=account.address, call_data=b"", value=0
            )
            return (
                block_heads.latest.base_fee_per_gas,
                await endpoint.read_max_priority_fee_per_gas(),
                native_transfer_gas_limit_of(estimated_gas),
            )

        base_fee_per_gas, suggested_priority_fee, gas_limit = await self.rpc_client.call_with_failover(read_fee_inputs)
        fee_cap = self.chain.maximum_fee_per_gas_wei
        current_priority_fee = min(max(suggested_priority_fee, self.chain.minimum_priority_fee_per_gas_wei), fee_cap)
        current_max_fee = min(2 * base_fee_per_gas + current_priority_fee, fee_cap)
        fills: list[AbandonedNonceFill] = []
        for nonce in abandoned_nonces:
            async with self.session_factory() as session:
                previous_fees = await SettlementRepository(session=session).read_highest_attempt_fees_at_nonce(
                    self.chain.key, operator_address=account.address, nonce=nonce
                )
            max_fee, priority_fee = current_max_fee, current_priority_fee
            if previous_fees is not None:
                previous_max_fee, previous_priority_fee = previous_fees
                priority_fee = min(raised_fee(previous_priority_fee, current=current_priority_fee), fee_cap)
                max_fee = min(max(raised_fee(previous_max_fee, current=current_max_fee), priority_fee), fee_cap)
            signed = account.sign_transaction(
                {
                    "type": 2,
                    "chainId": self.chain.chain_id,
                    "nonce": nonce,
                    "to": HexStr(account.address),
                    "value": 0,
                    "gas": gas_limit,
                    "maxFeePerGas": max_fee,
                    "maxPriorityFeePerGas": priority_fee,
                }
            )
            accepting_endpoints = 0
            for endpoint in self.rpc_client.endpoints:
                try:
                    await endpoint.send_raw_transaction(bytes(signed.raw_transaction))
                except EvmRpcUnavailableError:
                    continue
                accepting_endpoints += 1
            fill = AbandonedNonceFill(
                nonce=nonce,
                transaction_hash="0x" + bytes(signed.hash).hex(),
                accepting_endpoints=accepting_endpoints,
            )
            log.emit(
                logger,
                event=AbandonedNonceFillBroadcast(
                    chain_key=self.chain.key,
                    operator_address=account.address,
                    nonce=nonce,
                    transaction_hash=fill.transaction_hash,
                    accepting_endpoints=accepting_endpoints,
                ),
            )
            fills.append(fill)
        return fills
