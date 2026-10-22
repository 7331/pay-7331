"""The factory's DepositSwept event, decoded."""

from pydantic import BaseModel, ConfigDict


class DepositSweptEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    deposit_address: str
    destination_address: str
    # None for the native coin (the event's token is address zero).
    token_address: str | None
    amount: int
    block_number: int
    block_hash: str
    transaction_hash: str
    log_index: int
