"""Static deposit address uncredited reasons and event types (PLAN.md section 17)."""

from enum import StrEnum

from app.payments.account_events.webhook_event_scope import STATIC_DEPOSIT_ADDRESS_EVENT_PREFIX


class UncreditedReason(StrEnum):
    # A configured coin the address does not take: swept with the rest, never credited (the stray rule of a
    # receiver's invoice).
    WRONG_COIN = "wrong_coin"


class StaticDepositAddressEventType(StrEnum):
    """Each starts with ``STATIC_DEPOSIT_ADDRESS_EVENT_PREFIX``, which puts it in the webhook's scope (and an API
    token's ``GET /events``)."""

    PAYMENT_RECEIVED = f"{STATIC_DEPOSIT_ADDRESS_EVENT_PREFIX}payment_received"
    STRAY_PAYMENT = f"{STATIC_DEPOSIT_ADDRESS_EVENT_PREFIX}stray_payment"
    SWEPT = f"{STATIC_DEPOSIT_ADDRESS_EVENT_PREFIX}swept"
    SWEEP_FAILED = f"{STATIC_DEPOSIT_ADDRESS_EVENT_PREFIX}sweep_failed"
