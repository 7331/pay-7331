"""Whether the operator's float covers a refund (the runner's first attempt and each replacement of it)."""

from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.evm_rpc_api.client import EvmRpcEndpoint, decode_uint256, encode_token_balance_call
from app.payments.settlements.models import OutgoingOperation
from app.payments.settlements.repository import SettlementRepository


async def float_covers_refund(
    *,
    session_factory: Callable[[], AsyncSession],
    operation: OutgoingOperation,
    endpoint: EvmRpcEndpoint,
    operator_address: str,
    gas_cost: int,
) -> bool:
    """The refund's amount, the other refunds in flight and its gas at ``gas_cost`` (a token refund's gas from the
    native coin, its amount from the token)."""
    async with session_factory() as session:
        in_flight = await SettlementRepository(session=session).sum_in_flight_refunds(
            operation.chain_key, asset_symbol=operation.asset_symbol or "", excluding_operation_id=operation.id
        )
    amount = operation.amount_base_units or 0
    native_balance = await endpoint.read_native_balance(operator_address)
    if operation.token_address is None:
        return native_balance >= amount + in_flight + gas_cost
    (token_balance_result,) = await endpoint.aggregate3(
        [encode_token_balance_call(operation.token_address, holder_address=operator_address)], block_hash=None
    )
    token_balance = decode_uint256(token_balance_result.return_data) if token_balance_result.succeeded else 0
    return token_balance >= amount + in_flight and native_balance >= gas_cost
