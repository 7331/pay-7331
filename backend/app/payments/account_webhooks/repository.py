"""Queries for account_webhook_endpoints, account_discord_webhooks and account_webhook_deliveries; no decisions."""

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, DateTime, ScalarSelect, case, delete, func, or_, select, true, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.account_webhooks.enums import (
    AccountWebhookDeliveryStatus,
    AccountWebhookFailureReason,
    AccountWebhookReceiverKind,
)
from app.payments.account_webhooks.models import AccountDiscordWebhook, AccountWebhookDelivery, AccountWebhookEndpoint


class AccountWebhookRepository:
    def __init__(self, *, session: AsyncSession) -> None:
        self.session = session

    # ---- Endpoints ----

    async def find_endpoint(self, account_id: int) -> AccountWebhookEndpoint | None:
        return await self.session.scalar(
            select(AccountWebhookEndpoint).where(AccountWebhookEndpoint.account_id == account_id)
        )

    async def list_endpoints_of_accounts(
        self, account_ids: list[int], *, lock_for_share: bool = False
    ) -> list[AccountWebhookEndpoint]:
        """``lock_for_share`` (queuing deliveries): disabling the endpoint on a 410 or removing it waits for this
        transaction, so its failing of the pending deliveries sees the ones queued here."""
        statement = (
            select(AccountWebhookEndpoint)
            .where(AccountWebhookEndpoint.account_id.in_(account_ids))
            .order_by(AccountWebhookEndpoint.account_id)
        )
        if lock_for_share:
            statement = statement.with_for_update(read=True)
        return list(await self.session.scalars(statement))

    async def insert_endpoint(self, account_id: int, *, url: str, encrypted_secret: bytes) -> AccountWebhookEndpoint:
        endpoint = AccountWebhookEndpoint(account_id=account_id, url=url, encrypted_secret=encrypted_secret)
        self.session.add(endpoint)
        await self.session.flush()
        await self.session.refresh(endpoint)
        return endpoint

    async def update_endpoint(
        self, account_id: int, *, url: str | None = None, encrypted_secret: bytes | None = None
    ) -> None:
        values: dict[str, object] = {}
        if url is not None:
            # Setting the URL again turns a webhook disabled by a 410 back on.
            values["url"] = url
            values["disabled_at"] = None
        if encrypted_secret is not None:
            values["encrypted_secret"] = encrypted_secret
        if values:
            await self.session.execute(
                update(AccountWebhookEndpoint).where(AccountWebhookEndpoint.account_id == account_id).values(values)
            )

    async def disable_endpoint(self, account_id: int, *, url: str, disabled_at: datetime) -> bool:
        """The receiver answered 410 Gone: disabled, only while the account still has that URL and it is not disabled
        yet; True when disabled now."""
        result = await self.session.execute(
            update(AccountWebhookEndpoint)
            .where(
                AccountWebhookEndpoint.account_id == account_id,
                AccountWebhookEndpoint.url == url,
                AccountWebhookEndpoint.disabled_at.is_(None),
            )
            .values(disabled_at=disabled_at)
            .returning(AccountWebhookEndpoint.id)
        )
        return result.scalar_one_or_none() is not None

    async def delete_endpoint(self, account_id: int) -> bool:
        result = await self.session.execute(
            delete(AccountWebhookEndpoint)
            .where(AccountWebhookEndpoint.account_id == account_id)
            .returning(AccountWebhookEndpoint.id)
        )
        return result.scalar_one_or_none() is not None

    # ---- Discord webhooks ----

    async def find_discord_webhook(self, account_id: int) -> AccountDiscordWebhook | None:
        return await self.session.scalar(
            select(AccountDiscordWebhook).where(AccountDiscordWebhook.account_id == account_id)
        )

    async def find_discord_webhook_id(self, account_id: int) -> str | None:
        """The column only: an ORM row loaded here would stay stale in the session after a Core upsert."""
        return await self.session.scalar(
            select(AccountDiscordWebhook.discord_webhook_id).where(AccountDiscordWebhook.account_id == account_id)
        )

    async def find_operator_alert_repeat_hours(self, account_id: int) -> int | None:
        """None: no Discord webhook, or one Discord deleted, so the operator's alerts are not repeated: only Discord
        nags, so without a Discord webhook an alert is raised once."""
        return await self.session.scalar(
            select(AccountDiscordWebhook.operator_alert_repeat_hours).where(
                AccountDiscordWebhook.account_id == account_id, AccountDiscordWebhook.deleted_in_discord_at.is_(None)
            )
        )

    async def update_operator_alert_repeat_hours(self, account_id: int, *, repeat_hours: int) -> bool:
        """False when the account has no Discord webhook."""
        result = await self.session.execute(
            update(AccountDiscordWebhook)
            .where(AccountDiscordWebhook.account_id == account_id)
            .values(operator_alert_repeat_hours=repeat_hours, updated_at=func.now())
            .returning(AccountDiscordWebhook.id)
        )
        return result.scalar_one_or_none() is not None

    async def list_discord_webhooks_of_accounts(
        self, account_ids: list[int], *, lock_for_share: bool = False
    ) -> list[AccountDiscordWebhook]:
        """``lock_for_share`` (queuing messages): a removal or a deleted-in-Discord mark waits for this transaction, so
        its failing of the pending messages sees the ones queued here and none is left pending without a webhook."""
        statement = (
            select(AccountDiscordWebhook)
            .where(AccountDiscordWebhook.account_id.in_(account_ids))
            .order_by(AccountDiscordWebhook.account_id)
        )
        if lock_for_share:
            statement = statement.with_for_update(read=True)
        return list(await self.session.scalars(statement))

    async def lock_discord_webhooks_for_recording(self, account_ids: list[int]) -> list[AccountDiscordWebhook]:
        """A delivery pass recording its Discord outcomes locks the accounts' webhook rows first, in ascending
        ``account_id``, the order every other writer of those rows and then their messages takes (queuing, removal,
        replacing, the claim), so none of them deadlocks with it."""
        return list(
            await self.session.scalars(
                select(AccountDiscordWebhook)
                .where(AccountDiscordWebhook.account_id.in_(account_ids))
                .order_by(AccountDiscordWebhook.account_id)
                .with_for_update(key_share=True)
            )
        )

    async def upsert_discord_webhook(
        self,
        account_id: int,
        *,
        discord_webhook_id: str,
        encrypted_discord_webhook_token: bytes,
        discord_thread_id: str | None,
    ) -> None:
        """Sets or replaces the account's Discord webhook; a replaced one counts as not deleted in Discord. The row's
        ``created_at`` stays: events after the first set are sent. Another webhook drops the old one's hold (its rate
        limit or a failed message's retry), so the account's messages may go now; a pass that has them out keeps its
        lease. Raises IntegrityError on ``uq_account_discord_webhooks_discord_webhook_id`` when another account has
        that webhook."""
        statement = insert(AccountDiscordWebhook).values(
            account_id=account_id,
            discord_webhook_id=discord_webhook_id,
            encrypted_discord_webhook_token=encrypted_discord_webhook_token,
            discord_thread_id=discord_thread_id,
        )
        await self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[AccountDiscordWebhook.account_id],
                set_={
                    "discord_webhook_id": discord_webhook_id,
                    "encrypted_discord_webhook_token": encrypted_discord_webhook_token,
                    "discord_thread_id": discord_thread_id,
                    "deleted_in_discord_at": None,
                    "next_discord_send_at": case(
                        (
                            AccountDiscordWebhook.discord_webhook_id == statement.excluded.discord_webhook_id,
                            AccountDiscordWebhook.next_discord_send_at,
                        ),
                        else_=func.least(AccountDiscordWebhook.next_discord_send_at, func.now()),
                    ),
                    "updated_at": func.now(),
                },
            )
        )

    async def make_held_discord_deliveries_due_now(self, account_id: int) -> None:
        """The account's pending Discord messages held for its old webhook (a rate limit or a failed message's retry)
        are due now, for its new one. Not while a pass has them out: that pass does this when it records."""
        now = func.now()
        discord_send_lease_active = (
            select(AccountDiscordWebhook.id)
            .where(
                AccountDiscordWebhook.account_id == account_id,
                AccountDiscordWebhook.discord_send_lease_ends_at > now,
            )
            .exists()
        )
        await self.session.execute(
            update(AccountWebhookDelivery)
            .where(
                AccountWebhookDelivery.account_id == account_id,
                AccountWebhookDelivery.receiver_kind == AccountWebhookReceiverKind.DISCORD.value,
                AccountWebhookDelivery.status == AccountWebhookDeliveryStatus.PENDING.value,
                AccountWebhookDelivery.next_attempt_at > now,
                ~discord_send_lease_active,
            )
            .values(next_attempt_at=now)
        )

    async def delete_discord_webhook(self, account_id: int) -> bool:
        result = await self.session.execute(
            delete(AccountDiscordWebhook)
            .where(AccountDiscordWebhook.account_id == account_id)
            .returning(AccountDiscordWebhook.id)
        )
        return result.scalar_one_or_none() is not None

    async def mark_discord_webhook_deleted_in_discord(
        self, account_id: int, *, discord_webhook_id: str, discord_thread_id: str | None, deleted_at: datetime
    ) -> bool:
        """Only while the account still has that webhook and thread (it may have set another meanwhile); True when
        marked now."""
        result = await self.session.execute(
            update(AccountDiscordWebhook)
            .where(
                AccountDiscordWebhook.account_id == account_id,
                AccountDiscordWebhook.discord_webhook_id == discord_webhook_id,
                AccountDiscordWebhook.discord_thread_id.is_not_distinct_from(discord_thread_id),
                AccountDiscordWebhook.deleted_in_discord_at.is_(None),
            )
            .values(deleted_in_discord_at=deleted_at)
            .returning(AccountDiscordWebhook.id)
        )
        return result.scalar_one_or_none() is not None

    async def end_account_discord_send_lease(
        self, account_id: int, *, lease_ends_at: datetime, next_discord_send_at: datetime
    ) -> None:
        """Ends a pass's lease on the account's Discord sends, its next messages claimed from
        ``next_discord_send_at``; nothing when the lease is no longer the one the pass's claim set."""
        await self.session.execute(
            update(AccountDiscordWebhook)
            .where(
                AccountDiscordWebhook.account_id == account_id,
                AccountDiscordWebhook.discord_send_lease_ends_at == lease_ends_at,
            )
            .values(next_discord_send_at=next_discord_send_at, discord_send_lease_ends_at=None)
        )

    # ---- Deliveries ----

    async def insert_delivery(
        self,
        account_id: int,
        *,
        receiver_kind: AccountWebhookReceiverKind,
        account_event_id: int | None,
        webhook_message_id: str,
        event_type: str,
        body: str,
        due_at: datetime,
        receiver_url: str | None = None,
    ) -> AccountWebhookDelivery | None:
        """Queues one message; None when that account event was already queued to that kind (a claim seen twice).
        ``receiver_url``: an invoice callback's URL."""
        return await self.session.scalar(
            insert(AccountWebhookDelivery)
            .values(
                account_id=account_id,
                receiver_kind=receiver_kind.value,
                receiver_url=receiver_url,
                account_event_id=account_event_id,
                webhook_message_id=webhook_message_id,
                event_type=event_type,
                body=body,
                status=AccountWebhookDeliveryStatus.PENDING.value,
                next_attempt_at=due_at,
            )
            .on_conflict_do_nothing()
            .returning(AccountWebhookDelivery)
        )

    async def list_latest_account_deliveries(
        self, account_id: int, *, receiver_kind: AccountWebhookReceiverKind, limit: int
    ) -> list[AccountWebhookDelivery]:
        return list(
            await self.session.scalars(
                select(AccountWebhookDelivery)
                .where(
                    AccountWebhookDelivery.account_id == account_id,
                    AccountWebhookDelivery.receiver_kind == receiver_kind.value,
                )
                .order_by(AccountWebhookDelivery.id.desc())
                .limit(limit)
            )
        )

    async def requeue_account_delivery(
        self, delivery_public_id: str, *, account_id: int, receiver_kind: AccountWebhookReceiverKind, due_at: datetime
    ) -> AccountWebhookDelivery | None:
        """Resend: pending again, due now, the retry schedule from its start; None for an unknown public id or one of
        another account or receiver kind."""
        return await self.session.scalar(
            update(AccountWebhookDelivery)
            .where(
                AccountWebhookDelivery.public_id == delivery_public_id,
                AccountWebhookDelivery.account_id == account_id,
                AccountWebhookDelivery.receiver_kind == receiver_kind.value,
            )
            .values(
                status=AccountWebhookDeliveryStatus.PENDING.value,
                next_attempt_at=due_at,
                scheduled_failure_count=0,
                queued_at=due_at,
            )
            .returning(AccountWebhookDelivery)
        )

    async def fail_pending_account_deliveries(
        self,
        account_id: int,
        *,
        receiver_kinds: Sequence[AccountWebhookReceiverKind],
        reason: AccountWebhookFailureReason,
    ) -> None:
        """Locks the rows of every kind in ``receiver_kinds`` in one query, in the order a delivery pass records them
        (``lease_deliveries``: event order, a test message last), so a removal and a pass recording the same account's
        rows one by one never deadlock."""
        pending_ids_in_record_order = (
            select(AccountWebhookDelivery.id)
            .where(
                AccountWebhookDelivery.account_id == account_id,
                AccountWebhookDelivery.receiver_kind.in_([receiver_kind.value for receiver_kind in receiver_kinds]),
                AccountWebhookDelivery.status == AccountWebhookDeliveryStatus.PENDING.value,
            )
            .order_by(AccountWebhookDelivery.account_event_id.asc().nulls_last(), AccountWebhookDelivery.id)
            .with_for_update()
        )
        locked_ids = list(await self.session.scalars(pending_ids_in_record_order))
        if not locked_ids:
            return
        await self.session.execute(
            update(AccountWebhookDelivery)
            .where(AccountWebhookDelivery.id.in_(locked_ids))
            .values(
                status=AccountWebhookDeliveryStatus.FAILED.value, next_attempt_at=None, last_failure_reason=reason.value
            )
        )

    async def claim_due_webhook_deliveries(self, *, limit: int, lease: timedelta) -> list[AccountWebhookDelivery]:
        """Takes up to ``limit`` pending webhook and invoice callback deliveries (both signed Standard Webhooks) that are
        due, oldest due first, and pushes each one's
        ``next_attempt_at`` out by ``lease`` while its attempt is out (``FOR UPDATE SKIP LOCKED``: a second API takes
        others). An attempt whose outcome is never recorded (a crash) is due again when the lease ends."""
        now = func.clock_timestamp(type_=DateTime(timezone=True))
        due_delivery_ids = (
            select(AccountWebhookDelivery.id)
            .where(
                AccountWebhookDelivery.receiver_kind.in_(
                    [
                        AccountWebhookReceiverKind.STANDARD_WEBHOOK.value,
                        AccountWebhookReceiverKind.INVOICE_CALLBACK.value,
                    ]
                ),
                AccountWebhookDelivery.status == AccountWebhookDeliveryStatus.PENDING.value,
                AccountWebhookDelivery.next_attempt_at <= now,
            )
            .order_by(AccountWebhookDelivery.next_attempt_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        return await self.lease_deliveries(due_delivery_ids, now=now, lease=lease)

    async def claim_due_discord_deliveries(
        self, *, limit: int, messages_per_account: int, lease: timedelta
    ) -> tuple[list[AccountWebhookDelivery], dict[int, datetime]]:
        """Takes up to ``limit`` due Discord messages, at most ``messages_per_account`` of each account, from accounts
        whose sends are neither held (``next_discord_send_at``) nor leased to another pass, the least recently sent
        accounts first (``next_discord_send_at`` is when a pass last ended theirs) and each account's in event order;
        leases those accounts for ``lease`` (``discord_send_lease_ends_at``). The webhook rows are locked ``FOR UPDATE
        SKIP LOCKED`` while claiming, so two passes never take the same account's messages. Answers the messages by
        id and each account's lease end, which its outcomes must still match to be recorded. A crash leaves the leases
        to run out."""
        now = func.clock_timestamp(type_=DateTime(timezone=True))
        due_discord_delivery_exists = (
            select(AccountWebhookDelivery.id)
            .where(
                AccountWebhookDelivery.account_id == AccountDiscordWebhook.account_id,
                AccountWebhookDelivery.receiver_kind == AccountWebhookReceiverKind.DISCORD.value,
                AccountWebhookDelivery.status == AccountWebhookDeliveryStatus.PENDING.value,
                AccountWebhookDelivery.next_attempt_at <= now,
            )
            .exists()
        )
        least_recently_sent_first = (
            AccountDiscordWebhook.next_discord_send_at.asc().nulls_first(),
            AccountDiscordWebhook.account_id,
        )
        sendable_account_ids = list(
            await self.session.scalars(
                select(AccountDiscordWebhook.account_id)
                .where(
                    or_(
                        AccountDiscordWebhook.next_discord_send_at.is_(None),
                        AccountDiscordWebhook.next_discord_send_at <= now,
                    ),
                    or_(
                        AccountDiscordWebhook.discord_send_lease_ends_at.is_(None),
                        AccountDiscordWebhook.discord_send_lease_ends_at <= now,
                    ),
                    due_discord_delivery_exists,
                )
                .order_by(*least_recently_sent_first)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        if not sendable_account_ids:
            return [], {}
        due_messages_of_account = (
            select(AccountWebhookDelivery.id, AccountWebhookDelivery.account_event_id)
            .where(
                AccountWebhookDelivery.account_id == AccountDiscordWebhook.account_id,
                AccountWebhookDelivery.receiver_kind == AccountWebhookReceiverKind.DISCORD.value,
                AccountWebhookDelivery.status == AccountWebhookDeliveryStatus.PENDING.value,
                AccountWebhookDelivery.next_attempt_at <= now,
            )
            .order_by(AccountWebhookDelivery.account_event_id, AccountWebhookDelivery.id)
            .limit(messages_per_account)
            .with_for_update(skip_locked=True)
            .lateral("due_messages_of_account")
        )
        due_delivery_ids = (
            select(due_messages_of_account.c.id)
            .select_from(AccountDiscordWebhook)
            .join(due_messages_of_account, true())
            .where(AccountDiscordWebhook.account_id.in_(sendable_account_ids))
            .order_by(
                *least_recently_sent_first,
                due_messages_of_account.c.account_event_id,
                due_messages_of_account.c.id,
            )
            .limit(limit)
            .scalar_subquery()
        )
        claimed = await self.lease_deliveries(due_delivery_ids, now=now, lease=lease)
        if not claimed:
            return [], {}
        leased_accounts = await self.session.execute(
            update(AccountDiscordWebhook)
            .where(AccountDiscordWebhook.account_id.in_({delivery.account_id for delivery in claimed}))
            .values(discord_send_lease_ends_at=now + lease)
            .returning(AccountDiscordWebhook.account_id, AccountDiscordWebhook.discord_send_lease_ends_at)
        )
        lease_ends_at_by_account_id = {
            account_id: lease_ends_at for account_id, lease_ends_at in leased_accounts.tuples() if lease_ends_at
        }
        return claimed, lease_ends_at_by_account_id

    async def lease_deliveries(
        self, delivery_ids: ScalarSelect[int], *, now: ColumnElement[datetime], lease: timedelta
    ) -> list[AccountWebhookDelivery]:
        """Counts an attempt on each and moves its ``next_attempt_at`` to the lease's end; answers them in event order
        (``account_event_id``, then ``id``; a test message last)."""
        claimed = await self.session.scalars(
            update(AccountWebhookDelivery)
            .where(AccountWebhookDelivery.id.in_(delivery_ids))
            .values(
                next_attempt_at=now + lease,
                last_attempt_at=now,
                attempt_count=AccountWebhookDelivery.attempt_count + 1,
            )
            .returning(AccountWebhookDelivery)
        )
        return sorted(
            claimed,
            key=lambda delivery: (
                delivery.account_event_id is None,
                delivery.account_event_id or 0,
                delivery.id,
            ),
        )

    async def record_delivery_success(
        self, delivery_id: int, *, lease_ends_at: datetime, response_status: int, delivered_at: datetime
    ) -> None:
        await self.session.execute(
            update(AccountWebhookDelivery)
            .where(*still_leased_to_this_pass(delivery_id, lease_ends_at=lease_ends_at))
            .values(
                status=AccountWebhookDeliveryStatus.DELIVERED.value,
                next_attempt_at=None,
                last_response_status=response_status,
                last_failure_reason=None,
                delivered_at=delivered_at,
            )
        )

    async def record_delivery_failure(
        self,
        delivery_id: int,
        *,
        lease_ends_at: datetime,
        response_status: int | None,
        failure_reason: AccountWebhookFailureReason,
        next_attempt_at: datetime | None,
    ) -> None:
        """Counts the failure; ``next_attempt_at`` None ends the schedule (failed)."""
        await self.session.execute(
            update(AccountWebhookDelivery)
            .where(*still_leased_to_this_pass(delivery_id, lease_ends_at=lease_ends_at))
            .values(
                status=(
                    AccountWebhookDeliveryStatus.PENDING if next_attempt_at else AccountWebhookDeliveryStatus.FAILED
                ).value,
                next_attempt_at=next_attempt_at,
                last_response_status=response_status,
                last_failure_reason=failure_reason.value,
                scheduled_failure_count=AccountWebhookDelivery.scheduled_failure_count + 1,
            )
        )

    async def record_delivery_rate_limited(
        self, delivery_id: int, *, lease_ends_at: datetime, response_status: int, next_attempt_at: datetime
    ) -> None:
        """Discord's 429: pending until ``next_attempt_at`` (its ``retry_after``), the retry schedule untouched."""
        await self.session.execute(
            update(AccountWebhookDelivery)
            .where(*still_leased_to_this_pass(delivery_id, lease_ends_at=lease_ends_at))
            .values(
                next_attempt_at=next_attempt_at,
                last_response_status=response_status,
                last_failure_reason=AccountWebhookFailureReason.RATE_LIMITED.value,
            )
        )

    async def release_unattempted_delivery(
        self, delivery_id: int, *, lease_ends_at: datetime, next_attempt_at: datetime
    ) -> None:
        """A claimed Discord message held back unsent (an earlier one of its account failed or was rate limited, or
        the pass ran out of time): due at ``next_attempt_at``, the claim's attempt uncounted."""
        await self.session.execute(
            update(AccountWebhookDelivery)
            .where(*still_leased_to_this_pass(delivery_id, lease_ends_at=lease_ends_at))
            .values(next_attempt_at=next_attempt_at, attempt_count=AccountWebhookDelivery.attempt_count - 1)
        )

    async def release_attempted_delivery(
        self, delivery_id: int, *, lease_ends_at: datetime, next_attempt_at: datetime, response_status: int
    ) -> None:
        """A message Discord refused for a webhook the account has since replaced: due again at ``next_attempt_at``
        (to the new one), the attempt counted, the retry schedule untouched."""
        await self.session.execute(
            update(AccountWebhookDelivery)
            .where(*still_leased_to_this_pass(delivery_id, lease_ends_at=lease_ends_at))
            .values(next_attempt_at=next_attempt_at, last_response_status=response_status)
        )


def still_leased_to_this_pass(delivery_id: int, *, lease_ends_at: datetime) -> tuple[ColumnElement[bool], ...]:
    """A pass records a claimed delivery's outcome only while it is still pending under the lease the claim set: not
    once its webhook was removed or deleted in Discord (failed meanwhile, so a late outcome cannot make it pending
    again and send it to the account's next webhook), resent, or claimed again by a later pass after the lease ran
    out (that pass's outcome stands)."""
    return (
        AccountWebhookDelivery.id == delivery_id,
        AccountWebhookDelivery.status == AccountWebhookDeliveryStatus.PENDING.value,
        AccountWebhookDelivery.next_attempt_at == lease_ends_at,
    )
