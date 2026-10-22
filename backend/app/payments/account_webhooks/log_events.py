"""Typed log records of account webhooks and Discord webhooks. Ids, types, codes and the receiver's host only: never
the URL's path or query, a body, a secret or a Discord webhook's token."""

import logging

from app.shared.logging import LogEvent


class AccountWebhookSet(LogEvent):
    message = "Account webhook URL set after a fresh passkey check"
    operation = "account_webhook"
    account_id: int
    # True when this made the webhook's first secret.
    new_secret: bool


class AccountWebhookSecretRotated(LogEvent):
    message = "Account webhook secret rotated after a fresh passkey check"
    operation = "account_webhook"
    account_id: int


class AccountWebhookRemoved(LogEvent):
    message = "Account webhook removed; its pending deliveries failed"
    operation = "account_webhook"
    account_id: int


class AccountWebhookDisabledByReceiver(LogEvent):
    level = logging.WARNING
    message = "The webhook receiver answered 410 Gone: the account's webhook is disabled; its pending deliveries failed"
    operation = "account_webhook"
    account_id: int


class AccountWebhookDeliveryQueued(LogEvent):
    message = "A webhook delivery was queued"
    operation = "account_webhook_delivery"
    account_id: int
    delivery_id: int
    event_type: str
    # `event`, `test` or `resend`.
    cause: str


class AccountWebhookDeliveryAttempted(LogEvent):
    message = "A webhook delivery attempt finished"
    operation = "account_webhook_delivery"
    account_id: int
    delivery_id: int
    # standard_webhook or discord.
    receiver_kind: str
    event_type: str
    attempt_count: int
    receiver_host: str
    delivered: bool
    response_status: int | None
    # AccountWebhookFailureReason; None when delivered.
    failure_reason: str | None
    # pending (a retry is scheduled), delivered or failed.
    status: str


class AccountWebhookDeliveryNotAttempted(LogEvent):
    level = logging.WARNING
    message = "An account's webhook secret or Discord webhook token does not open under this seed; its deliveries fail"
    operation = "account_webhook_delivery"
    account_id: int


class AccountDiscordWebhookSet(LogEvent):
    message = "Account Discord webhook set after a fresh passkey check"
    operation = "account_discord_webhook"
    account_id: int


class AccountDiscordOperatorAlertRepeatSet(LogEvent):
    message = "Operator account's alert reminder cadence set"
    operation = "account_discord_webhook"
    account_id: int
    repeat_hours: int


class AccountDiscordWebhookRemoved(LogEvent):
    message = "Account Discord webhook removed; its pending messages failed"
    operation = "account_discord_webhook"
    account_id: int


class AccountDiscordWebhookDeletedInDiscord(LogEvent):
    level = logging.WARNING
    message = "Discord answered 404, Unknown Webhook or Invalid Webhook Token: the account's Discord webhook is gone"
    operation = "account_discord_webhook"
    account_id: int
    response_status: int


class DiscordSendsPaused(LogEvent):
    level = logging.WARNING
    message = "Every account's Discord sends paused"
    operation = "account_discord_webhook"
    # global_rate_limit, rate_limit_without_json_body or invalid_request_budget (store.py).
    reason: str
    pause_seconds: int


class DiscordSendPauseUnavailable(LogEvent):
    level = logging.WARNING
    message = "Discord send pauses could not reach Redis; sends go on unpaused"
    operation = "account_discord_webhook"
    # read, pause or count.
    step: str
