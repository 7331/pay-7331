"""Read types of the account webhook domain's delivery loop."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, SecretStr

from app.payments.account_webhooks.enums import AccountWebhookFailureReason


class AttemptOutcome(BaseModel):
    """What one claimed delivery came to in a pass."""

    model_config = ConfigDict(frozen=True)

    delivered: bool
    response_status: int | None
    failure_reason: AccountWebhookFailureReason | None
    receiver_host: str
    # Set for a Discord 429 (its `retry_after`): when it is due again, outside the schedule.
    retry_at: datetime | None = None
    # A webhook receiver's `Retry-After` on a failure, capped: the scheduled retry goes no earlier than this.
    retry_not_before: datetime | None = None


class DiscordMessageAnswer(BaseModel):
    """What one Discord message came to, read from Discord's answer without waiting on anything: what the pass must
    still tell Redis (the invalid request count, a pause of every send) is done after the sends' timeout, so the
    timeout cannot cancel it and lose the outcome."""

    model_config = ConfigDict(frozen=True)

    outcome: AttemptOutcome
    # When the account's next message may go (a spent bucket's reset or a 429's `retry_after`); None: at once.
    rate_limit_hold_until: datetime | None
    # A 401, 403 or 429 not scoped `shared`: counts toward Discord's invalid request limit (store.py).
    counts_as_invalid_request: bool = False
    # A global or unexplained 429: every account's sends pause this long, for this reason (store.py).
    sending_pause_seconds: float | None = None
    sending_pause_reason: str | None = None


class AccountDiscordSendResult(BaseModel):
    """What one account's claimed Discord messages came to in a pass: an outcome for each one sent, in order; the
    ones after them were held back unsent."""

    model_config = ConfigDict(frozen=True)

    attempted_outcomes: list[AttemptOutcome]
    # Discord's rate limit for the webhook: a spent bucket's reset or a 429's `retry_after`. None: none known.
    rate_limit_hold_until: datetime | None


class StandardWebhookDeliveryTarget(BaseModel):
    """A claimed webhook delivery's URL and secret, read in the claim's transaction."""

    model_config = ConfigDict(frozen=True)

    url: str
    secret: str


class DiscordDeliveryTarget(BaseModel):
    """A claimed Discord message's webhook, its token opened, read in the claim's transaction."""

    model_config = ConfigDict(frozen=True)

    discord_webhook_id: str
    discord_webhook_token: SecretStr
    discord_thread_id: str | None
