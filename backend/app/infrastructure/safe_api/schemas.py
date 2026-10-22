"""What the Safe and Allowance Module encoders take and the readers return."""

from pydantic import BaseModel, ConfigDict


class SafeCall(BaseModel):
    """One call a Safe makes (a Transaction Builder entry, one leg of a MultiSendCallOnly batch)."""

    model_config = ConfigDict(frozen=True)

    to: str
    value: int
    data: bytes
    # What it does, for the operator's eyes (the batch's printout); not encoded.
    description: str


class TokenAllowance(BaseModel):
    """``getTokenAllowance``'s ``[amount, spent, resetTimeMin, lastResetMin, nonce]``, base units and minutes."""

    model_config = ConfigDict(frozen=True)

    amount: int
    spent: int
    reset_time_minutes: int
    last_reset_minute: int
    nonce: int

    def available_at(self, *, block_timestamp: int) -> int:
        """What one more transfer may move at ``block_timestamp``. ``getAllowance`` zeroes ``spent`` once
        ``resetTimeMin`` minutes have passed since ``lastResetMin`` (AllowanceModule.sol, safe-modules tag allowance/v0.1.1,
        read 2026-10-13); ``getTokenAllowance`` already applies that at the block it reads, and the check is repeated
        here for a block read later than the allowance (idempotent on an already reset reading)."""
        current_minute = block_timestamp // 60
        spent = self.spent
        if self.reset_time_minutes > 0 and self.last_reset_minute <= current_minute - self.reset_time_minutes:
            spent = 0
        return max(self.amount - spent, 0)
