"""The gas limit of a replacement attempt (runner_service.py, 2026-10-14).

A replacement at the same nonce prices its fee caps anew, so its gas limit is estimated anew too: on a rollup the
node's estimate includes the parent chain's posting fee as gas, which moves with that chain's fee market
(https://docs.arbitrum.io/how-arbitrum-works/deep-dives/gas-and-fees: "the value returned by ``eth_estimateGas`` will
change over time"), so the first attempt's limit may no longer pay for the transaction at all and no replacement
carrying it would ever mine. The call is the one the caller resolved (a relayed top-up's stored first call, never a
new signature), and nothing about the operation's recipient or amount changes: only the fee caps and the gas limit.
"""

from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.evm_rpc_api.client import EvmRpcEndpoint
from app.infrastructure.evm_rpc_api.errors import EvmExecutionRevertedError, EvmInsufficientFundsError
from app.payments.chains.types import EvmChainConfiguration
from app.payments.settlements.enums import OperationKind
from app.payments.settlements.errors import ReplacementRefusedError
from app.payments.settlements.models import OutgoingAttempt, OutgoingOperation
from app.payments.settlements.operation_transactions import gas_limit_of, key_covers_value_and_gas
from app.payments.settlements.refund_float import float_covers_refund


async def estimate_replacement_gas_limit(
    *,
    session_factory: Callable[[], AsyncSession],
    operation: OutgoingOperation,
    newest_attempt: OutgoingAttempt,
    chain: EvmChainConfiguration,
    endpoint: EvmRpcEndpoint,
    target_address: str,
    call_data: bytes,
    value: int,
    max_fee_per_gas: int,
) -> int:
    """The newest attempt's limit or the fresh estimate's, whichever is higher, once the key (the float, for a
    refund) is known to cover ``value`` and that limit at ``max_fee_per_gas``. Raises ``ReplacementRefusedError``
    when the call no longer estimates or the balance falls short: nothing is signed then, and the caller rebroadcasts
    the newest attempt, which its own fee caps already fit."""
    try:
        estimated_gas = await endpoint.estimate_gas(
            sender_address=operation.operator_address, target_address=target_address, call_data=call_data, value=value
        )
    except EvmExecutionRevertedError as error:
        raise ReplacementRefusedError("estimate_reverts") from error
    except EvmInsufficientFundsError as error:
        raise ReplacementRefusedError("operator_gas_short") from error
    gas_limit = max(
        newest_attempt.gas_limit,
        gas_limit_of(operation, chain=chain, call_data=call_data, estimated_gas=estimated_gas),
    )
    gas_cost = gas_limit * max_fee_per_gas
    if operation.kind == OperationKind.REFUND:
        funded = await float_covers_refund(
            session_factory=session_factory,
            operation=operation,
            endpoint=endpoint,
            operator_address=operation.operator_address,
            gas_cost=gas_cost,
        )
    else:
        funded = await key_covers_value_and_gas(
            endpoint, operator_address=operation.operator_address, value=value, gas_cost=gas_cost
        )
    if not funded:
        raise ReplacementRefusedError("operator_gas_short")
    return gas_limit
