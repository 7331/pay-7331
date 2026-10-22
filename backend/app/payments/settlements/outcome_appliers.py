"""What the settlement runner hands an operation's end to, injected by the composition from the domains above this one
(runner_service.py's docstring): an invoice operation's outcome goes to its invoice, a static deposit sweep's to its
address (PLAN.md section 17), each on the session that writes the operation's new status."""

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.settlements.enums import OperationKind, OperationStatus
from app.payments.settlements.models import OutgoingOperation
from app.payments.settlements.types import SettlementOutcome, StaticDepositSweepOutcome


class SettlementOutcomeApplier(Protocol):
    """Applies an outcome to its invoice and records its events on the resolving transaction's session."""

    async def __call__(self, session: AsyncSession, *, outcome: SettlementOutcome) -> None: ...


class StaticDepositSweepOutcomeApplier(Protocol):
    """Applies a static deposit sweep's outcome to its address and records its events on the resolving transaction's
    session."""

    async def __call__(self, session: AsyncSession, *, outcome: StaticDepositSweepOutcome) -> None: ...


def static_deposit_sweep_outcome_of(
    operation: OutgoingOperation, *, status: OperationStatus
) -> StaticDepositSweepOutcome | None:
    """The outcome of a static deposit sweep that just changed to ``status``; None for any other operation (a sweep
    never waits for float)."""
    if (
        operation.kind != OperationKind.STATIC_DEPOSIT_SWEEP
        or operation.static_deposit_address_id is None
        or status == OperationStatus.WAITING_FOR_FLOAT
    ):
        return None
    return StaticDepositSweepOutcome(
        operation_id=operation.id,
        static_deposit_address_id=operation.static_deposit_address_id,
        status=status,
        transaction_hash=operation.transaction_hash,
        failure_reason=operation.failure_reason,
    )
