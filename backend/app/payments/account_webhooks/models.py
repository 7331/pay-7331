"""An account's webhook, its Discord webhook and the messages sent to them (migrations 0015
and 0018; PLAN.md sections 8.3 and 8.4)."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.postgres.base import Base
from app.shared.identifiers import generate_public_id

WEBHOOK_URL_MAXIMUM_LENGTH = 2048
ACCOUNT_WEBHOOK_DELIVERY_PUBLIC_ID_PREFIX = "whd_"
DISCORD_WEBHOOK_ID_MAXIMUM_LENGTH = 32
# A week: a reminder rarer than that is no reminder.
OPERATOR_ALERT_REPEAT_HOURS_MAXIMUM = 168


class AccountWebhookEndpoint(Base):
    """At most one per account."""

    __tablename__ = "account_webhook_endpoints"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id", ondelete="CASCADE"), unique=True)
    # https (http only with PAY_WEBHOOK_ALLOW_PRIVATE_HOSTS, the e2e stack); never logged.
    url: Mapped[str] = mapped_column(String(WEBHOOK_URL_MAXIMUM_LENGTH))
    # The `whsec_...` secret, AES-GCM sealed under a key derived from PAY_INVOICE_SALT_SEED (secret_encryption.py):
    # recoverable, since every delivery is signed with it, but useless in a dump without the seed.
    encrypted_secret: Mapped[bytes] = mapped_column(LargeBinary)
    # Set when the receiver answered 410 Gone (Standard Webhooks: disable the endpoint and stop sending): nothing is
    # queued or sent until the account sets the URL again (migration 0019).
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AccountDiscordWebhook(Base):
    """At most one per account (2026-10-13): a Discord channel's "Execute Webhook" URL, stored as its parts."""

    __tablename__ = "account_discord_webhooks"
    __table_args__ = (
        # Discord's rate limits are per webhook, so one account at a time may use a webhook (2026-10-13).
        Index("uq_account_discord_webhooks_discord_webhook_id", "discord_webhook_id", unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id", ondelete="CASCADE"), unique=True)
    # The webhook's snowflake id: not a secret (it addresses nothing without the token), shown in /settings.
    discord_webhook_id: Mapped[str] = mapped_column(String(DISCORD_WEBHOOK_ID_MAXIMUM_LENGTH))
    # The webhook's token, which alone lets anyone post to the channel: AES-GCM sealed like a webhook secret
    # (secret_encryption.py, its own HKDF info); never logged or answered.
    encrypted_discord_webhook_token: Mapped[bytes] = mapped_column(LargeBinary)
    # The thread messages go to (`?thread_id=` of the pasted URL): a forum or media channel's webhook takes a message
    # only into a thread (migration 0019). None: the webhook's channel itself.
    discord_thread_id: Mapped[str | None] = mapped_column(String(DISCORD_WEBHOOK_ID_MAXIMUM_LENGTH))
    # Set when Discord answered 404, Unknown Webhook or Invalid Webhook Token (JSON code 10015 or 50027): nothing is
    # queued or sent until it is set again.
    deleted_in_discord_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The account's Discord messages are not claimed before this: after a spent rate-limit bucket or a 429, and until
    # a failed message's retry, so none overtakes an earlier one. Set to when the last pass ended otherwise, which
    # puts the least recently sent accounts first. None: never sent yet.
    next_discord_send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # While a delivery pass has the account's messages out: the end of its lease, so one pass at a time sends them. A
    # pass records only while it still equals the value its claim set (a late pass changes nothing). None: no pass.
    discord_send_lease_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # How often an operator alert that lasts is raised again, in hours; 0: once per problem
    # (migration 0031). Read only on the operator's account; without a Discord webhook nothing repeats.
    operator_alert_repeat_hours: Mapped[int] = mapped_column(SmallInteger, server_default=text("1"))


class AccountWebhookDelivery(Base):
    """One message to an account's webhook or Discord webhook: an invoice event, or a test. Retried on its schedule
    until a 2xx."""

    __tablename__ = "account_webhook_deliveries"
    __table_args__ = (
        # An event is queued once per receiver kind however often it is claimed.
        UniqueConstraint(
            "account_event_id",
            "receiver_kind",
            name="uq_account_webhook_deliveries_account_event_id_receiver_kind",
        ),
        Index("ix_account_webhook_deliveries_account_id_id", "account_id", "id"),
        Index("ux_account_webhook_deliveries_public_id", "public_id", unique=True),
        # The webhook claim: due pending rows of one kind, oldest due first.
        Index(
            "ix_account_webhook_deliveries_pending_kind_next_attempt_at",
            "receiver_kind",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
        # The Discord claim: an account's due pending messages.
        Index(
            "ix_account_webhook_deliveries_pending_account_kind_next_attempt",
            "account_id",
            "receiver_kind",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # The delivery's id on the wire (`whd_...`, migration 0036): its resend path; the integer id stays internal.
    public_id: Mapped[str] = mapped_column(
        String(26),
        default=lambda: generate_public_id(ACCOUNT_WEBHOOK_DELIVERY_PUBLIC_ID_PREFIX),
        server_default=text(f"generate_public_id('{ACCOUNT_WEBHOOK_DELIVERY_PUBLIC_ID_PREFIX}')"),
    )
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id", ondelete="CASCADE"))
    # The event it carries; None for a test.
    account_event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("account_events.id", ondelete="CASCADE")
    )
    # AccountWebhookReceiverKind: the account's webhook, its Discord webhook (2026-10-13) or an invoice's own
    # ``callback_url`` (2026-10-21).
    receiver_kind: Mapped[str] = mapped_column(String(16), server_default=text("'standard_webhook'"))
    # An invoice callback's URL, fixed when queued (migration 0032); None for the other kinds, which read their
    # account's current URL at send time. Never logged.
    receiver_url: Mapped[str | None] = mapped_column(String(WEBHOOK_URL_MAXIMUM_LENGTH))
    # The `webhook-id` header: the account event's public id (`evt_...`), or `test_...`; the same on every attempt and
    # resend. A Discord message's is `discord_` plus the event's public id or `discord_test_...` (never sent, only
    # unique). Rows queued before migration 0036 keep the integer event id they were first sent with.
    webhook_message_id: Mapped[str] = mapped_column(String(64), unique=True)
    event_type: Mapped[str] = mapped_column(String(64))
    # The exact JSON body sent (and, to a webhook, signed) on every attempt; never logged.
    body: Mapped[str] = mapped_column(Text)
    # AccountWebhookDeliveryStatus.
    status: Mapped[str] = mapped_column(String(16))
    # Every attempt made, resends included (shown in /settings).
    attempt_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # Failures since it was queued or last resent: picks the next delay of the retry schedule.
    scheduled_failure_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # When a pending delivery is due; while an attempt is out, the end of its lease, and its outcome is recorded only
    # while it still equals that (a late pass changes nothing).
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_response_status: Mapped[int | None] = mapped_column(Integer)
    # AccountWebhookFailureReason of the last failed attempt; None after a success.
    last_failure_reason: Mapped[str | None] = mapped_column(String(64))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When it was queued or last resent: a Discord 429 later than the retry schedule's span from here fails it.
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
