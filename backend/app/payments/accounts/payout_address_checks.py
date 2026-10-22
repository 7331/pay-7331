"""The gateway-deposit refusal for a payout address (PLAN.md section 17), checked at sign-up and on a payout address
change."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.accounts.types import GatewayDepositAddressCheck
from app.shared.errors import InvalidInputError


async def refuse_gateway_deposit_payout_address(
    session: AsyncSession, *, payout_address: str, gateway_deposit_address_check: GatewayDepositAddressCheck
) -> None:
    """A payout address is never one of this gateway's deposits (it would be swept back into itself)."""
    if await gateway_deposit_address_check(session, payout_address):
        raise InvalidInputError("payout_address: that is a deposit address of this gateway")
