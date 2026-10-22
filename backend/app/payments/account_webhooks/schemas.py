"""Wire bodies of the account webhook and Discord webhook routes."""

from datetime import datetime

from pydantic import Field, JsonValue

from app.payments.account_webhooks.enums import (
    AccountDiscordWebhookStatus,
    AccountWebhookDeliveryStatus,
    AccountWebhookFailureReason,
    WebhookTestEventType,
)
from app.payments.account_webhooks.models import OPERATOR_ALERT_REPEAT_HOURS_MAXIMUM, WEBHOOK_URL_MAXIMUM_LENGTH
from app.shared.schemas import ApiRequest, ApiResponse


class SetAccountWebhookRequest(ApiRequest):
    """PUT /account-webhook: the URL and `credential.toJSON()` of the passkey check from
    `POST /account-webhook/change-options`."""

    url: str = Field(min_length=1, max_length=WEBHOOK_URL_MAXIMUM_LENGTH)
    credential: dict[str, JsonValue]


class RotateAccountWebhookSecretRequest(ApiRequest):
    """POST /account-webhook/secret-rotation: `credential.toJSON()` of the passkey check."""

    credential: dict[str, JsonValue]


class SendTestWebhookEventRequest(ApiRequest):
    """POST /account-webhook/test-events: ``webhook.test`` (a message) or one of the invoice or static deposit address
    event types, sent with a sample. Left out (no body), ``webhook.test``."""

    event_type: WebhookTestEventType = WebhookTestEventType.WEBHOOK_TEST


class AccountWebhookDeliveryResponse(ApiResponse):
    # `whd_...`: names the delivery in its resend path.
    id: str
    # The `webhook-id` header it is sent with: the event's id (`evt_...`), or `test_...` for a test.
    webhook_id: str
    event_type: str
    status: AccountWebhookDeliveryStatus
    attempt_count: int
    # Pending only: when the next attempt goes.
    next_attempt_at: datetime | None
    last_response_status: int | None
    # Why the last attempt failed; the discord_* reasons and rate_limited are a Discord message's. Null after a 2xx.
    last_failure_reason: AccountWebhookFailureReason | None
    created_at: datetime


class AccountWebhookResponse(ApiResponse):
    # null: no webhook set.
    url: str | None
    # When the receiver answered 410 Gone and sending stopped; null while it is on. Setting the URL again clears it.
    disabled_at: datetime | None
    # The newest 20 deliveries, newest first.
    deliveries: list[AccountWebhookDeliveryResponse]


class ChangedAccountWebhookResponse(AccountWebhookResponse):
    """The answer to setting the URL or rotating the secret."""

    # `whsec_...`, set only when this change made a new secret (the first URL set, every rotation); never shown again.
    secret: str | None


class SetAccountDiscordWebhookRequest(ApiRequest):
    """PUT /account-discord-webhook: the channel webhook's URL (Discord's "Copy Webhook URL") and `credential.toJSON()`
    of the passkey check from `POST /account-discord-webhook/change-options`."""

    url: str = Field(min_length=1, max_length=WEBHOOK_URL_MAXIMUM_LENGTH)
    credential: dict[str, JsonValue]


class AccountDiscordWebhookResponse(ApiResponse):
    """The account's Discord webhook as /settings shows it: never the URL or its token."""

    # The webhook's id (the number in its URL); null: none set.
    discord_webhook_id: str | None
    # The thread messages go to (`?thread_id=` of the pasted URL, for a forum or media channel); null: the channel.
    discord_thread_id: str | None = None
    # deleted_in_discord: Discord answered 404, Unknown Webhook or Invalid Webhook Token and sending stopped. Null: none
    # set.
    status: AccountDiscordWebhookStatus | None
    deleted_in_discord_at: datetime | None
    # The newest message queued to it (a test or an invoice event), to show whether it went out.
    latest_message: AccountWebhookDeliveryResponse | None
    # On the operator's account with a webhook set: how often an operator alert that lasts is raised again, in hours,
    # 0 for never. Null on every other account (it gets no operator alerts) or without a webhook.
    operator_alert_repeat_hours: int | None = None


class SetOperatorAlertRepeatRequest(ApiRequest):
    """PUT /account-discord-webhook/operator-alert-repeat: hours between reminders of a lasting operator alert, 0 for
    none (alert once per problem)."""

    repeat_hours: int = Field(ge=0, le=OPERATOR_ALERT_REPEAT_HOURS_MAXIMUM)
