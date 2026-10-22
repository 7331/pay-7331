"""Refund refusals callers tell apart."""

from app.shared.errors import AppError, ConflictError


class RefundOfferNotFoundError(AppError):
    code = "refund_offer_not_found"
    status = 404


class RefundOfferUsedError(ConflictError):
    code = "refund_offer_used"


class RefundSignatureInvalidError(AppError):
    code = "refund_signature_invalid"
    status = 400


class RefundNotNeedingOwnerError(ConflictError):
    code = "refund_not_needing_owner"


class RefundPaymentNotProvenError(AppError):
    code = "refund_payment_not_proven"
    status = 400


class RefundUnpaidNotProvenError(AppError):
    code = "refund_unpaid_not_proven"
    status = 400


class RefundAttemptMinedError(ConflictError):
    code = "refund_attempt_mined"


class OperatorKeyNotFoundError(AppError):
    """No key state row for that chain and address (the watcher records one for every configured key)."""

    code = "operator_key_not_found"
    status = 404


class OperatorKeyStatusConflictError(ConflictError):
    """The key is not in the status the asked change starts from."""

    code = "operator_key_status_conflict"


class OperatorKeyHasAbandonedNoncesError(ConflictError):
    """A frozen key holds nonces it was handed but never used; it signs again only once they are filled."""

    code = "operator_key_has_abandoned_nonces"


class TreasuryNotConfiguredError(ConflictError):
    code = "treasury_not_configured"


class TreasuryOwnerIsOperatorKeyError(AppError):
    """The treasury Safe's owner must never be an operator key: a server key would then own the treasury."""

    code = "treasury_owner_is_operator_key"
    status = 400


class E2eInjectedResolveFailureError(RuntimeError):
    """Raised only by the fork e2e's ``PAY_E2E_FAIL_AFTER_OPERATION_RESOLVE`` hook (configuration.py)."""


class PassReadUnavailableError(RuntimeError):
    """A read this operation's step needs failed in the pass's reads; the step fails alone and runs next pass."""


class ReplacementRefusedError(RuntimeError):
    """A replacement attempt cannot be signed now (its call no longer estimates, or the key cannot pay its gas); the
    newest stored attempt is rebroadcast instead. ``reason`` is the log event's."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
