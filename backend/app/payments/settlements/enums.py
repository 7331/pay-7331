"""Outgoing operation kinds and statuses (PLAN.md sections 5 and 11), and operator key roles and states."""

from enum import StrEnum


class OperationKind(StrEnum):
    SWEEP = "sweep"
    REFUND = "refund"
    # An operator key pulls funds to itself from the treasury Safe through its Allowance Module allowance.
    TOP_UP = "top_up"
    # A retired key sends what it holds back to the treasury Safe (and only there).
    TREASURY_RETURN = "treasury_return"
    # The refund key deploys the treasury Safe (``treasury create-safe``).
    TREASURY_SAFE_CREATION = "treasury_safe_creation"
    # The factory's ``sweepToDestination`` for a static deposit address (PLAN.md section 17): no invoice, its address
    # in ``static_deposit_address_id``.
    STATIC_DEPOSIT_SWEEP = "static_deposit_sweep"

    @property
    def has_invoice(self) -> bool:
        """Sweeps and refunds belong to an invoice; the key operations have none and no invoice effect."""
        return self in {OperationKind.SWEEP, OperationKind.REFUND}

    @property
    def is_deposit_sweep(self) -> bool:
        """The factory's ``sweepToDestination``, for an invoice's deposit or a static deposit address."""
        return self in {OperationKind.SWEEP, OperationKind.STATIC_DEPOSIT_SWEEP}

    @property
    def is_key_operation(self) -> bool:
        """The operator keys' own operations: no invoice, no static deposit address, no effect elsewhere."""
        return self in {OperationKind.TOP_UP, OperationKind.TREASURY_RETURN, OperationKind.TREASURY_SAFE_CREATION}


class OperationStatus(StrEnum):
    PENDING = "pending"
    WAITING_FOR_FLOAT = "waiting_for_float"
    BROADCAST = "broadcast"
    MINED = "mined"
    FINAL = "final"
    FAILED = "failed"
    # An unproven end (our nonce mined, no receipt of ours found on any endpoint): never retried, a refund keeps
    # its amount reserved until the owner settles it by hand.
    NEEDS_OWNER = "needs_owner"

    @property
    def is_active(self) -> bool:
        """Not yet resolved: it may still move money."""
        match self:
            case (
                OperationStatus.PENDING
                | OperationStatus.WAITING_FOR_FLOAT
                | OperationStatus.BROADCAST
                | OperationStatus.MINED
            ):
                return True
            case OperationStatus.FINAL | OperationStatus.FAILED | OperationStatus.NEEDS_OWNER:
                return False


class OperatorKeyRole(StrEnum):
    # PAY_OPERATOR_PRIVATE_KEY_<CHAIN>: refunds and their float, and sweeps when no sweeper key is set.
    REFUND = "refund"
    # PAY_SWEEPER_PRIVATE_KEYS_<CHAIN>: sweeps only, gas only.
    SWEEPER = "sweeper"


class OperatorKeyStatus(StrEnum):
    ACTIVE = "active"
    # Used outside this system (its nonce passed what the allocator handed out): never signs or gets work again
    # until the owner's ``operator unfreeze-key``.
    FROZEN = "frozen"
    # The owner's ``operator retire-key``: no new work; what it holds goes back to the treasury Safe.
    RETIRED = "retired"
