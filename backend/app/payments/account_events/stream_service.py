"""AccountEventStreamService: the SSE stream's source (PLAN.md section 8.1), one shared poller per API process.

A connection subscribes (an in-process queue for its account), reads its backlog on its own short transactions, then
only waits on its queue. ``poll_subscribed_account_events``, run in the API's own loops every
``POLL_INTERVAL_SECONDS``, is the one reader of new rows: one short transaction re-checks every subscribed session
at once and reads the new rows of every subscribed account, which it hands to their queues. So the queries no longer
grow with the open pages, and a process with no open stream makes none (the pass waits for a first subscriber).

Polling, not Postgres LISTEN/NOTIFY: NOTIFY reaches only a session listening at that moment and is not stored for one
that is not (https://www.postgresql.org/docs/current/sql-notify.html), so a dropped listener connection would need
this same catch-up read anyway; psycopg wants that listener on its own autocommit connection outside SQLAlchemy's
pool (https://www.psycopg.org/psycopg3/docs/advanced/async.html#asynchronous-notifications); the watcher's inserts
would need a trigger (a migration); and a full notification queue makes the NOTIFYing transaction fail at commit,
which here would be the watcher's payment steps. One indexed read every 2 s per process is simpler and costs little.

Ordering and the late commit (unchanged from the per-connection poll this replaces): ids are taken at insert and
committed later, so a lower id can appear after a higher one was sent. The poller reads past its cursor plus every
row made in the last ``LATE_COMMIT_WINDOW`` and skips ids it already handed out; each connection again skips ids it
sent and anything at or below its own cursor older than the window, so it sends what its own poll would have, in id
order per read, without duplicates, with one difference accepted (audit, 2026-10-13): the poller's cursor is
shared by every subscribed account, so a row committed more than ``LATE_COMMIT_WINDOW`` after its insert, below an id another
account already moved the cursor past, is not streamed (the page shows it on its next load). Watcher transactions
end within the 90 s tick timeout and API ones within a request, so none should lag that long. Handoff: a connection subscribes before its backlog read, so a row committed
after that read is found by a later poll and queued, and one committed before it is in the backlog (and if queued too,
skipped as sent). A poller that wakes from idle starts its cursor at the newest id: older rows are in the backlogs.

A slow page: each queue holds ``SUBSCRIPTION_QUEUE_EVENT_COUNT`` events; one that is full is closed and dropped, so
it neither blocks the others nor grows, and its page reconnects with ``Last-Event-ID`` and reads what it missed as
backlog. A sign-out, an expiry or a deleted passkey ends the stream at the next poll, as before.
"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.account_events.log_events import AccountEventStreamSubscriberDropped
from app.payments.account_events.models import AccountEvent
from app.payments.account_events.repository import AccountEventRepository
from app.payments.account_events.schemas import AccountEventResponse, AccountEventsCaughtUpResponse
from app.payments.account_events.service import ACCOUNT_EVENT_PUBLIC_ID_PATTERN, present_account_event
from app.payments.account_events.types import StreamedAccountEvent
from app.payments.accounts.service import AccountService
from app.shared import logging as log

logger = log.get_logger(__name__)

# About one watcher tick (chains.toml `poll_interval_seconds`).
POLL_INTERVAL_SECONDS = 2.0
BACKLOG_EVENT_COUNT = 50
EVENTS_PER_POLL = 200
# Ids are taken at insert and committed later, so a lower id can appear after a higher one was sent: every read
# re-reads this window and skips what it already sent.
LATE_COMMIT_WINDOW = timedelta(minutes=2)
# Events one connection may have waiting; a page that falls further behind is closed and reconnects.
SUBSCRIPTION_QUEUE_EVENT_COUNT = 1000

type SessionFactory = Callable[[], AsyncSession]


def present_streamed_account_event(account_event: AccountEvent) -> StreamedAccountEvent:
    return StreamedAccountEvent(
        account_event_id=account_event.id,
        created_at=account_event.created_at,
        account_event_response=present_account_event(account_event),
    )


class AccountEventSubscription:
    """One open stream's queue. ``None`` in the queue, or ``closed``, ends the stream."""

    def __init__(self, *, account_id: int, account_session_id: int) -> None:
        self.account_id = account_id
        self.account_session_id = account_session_id
        self.queue: asyncio.Queue[StreamedAccountEvent | None] = asyncio.Queue(maxsize=SUBSCRIPTION_QUEUE_EVENT_COUNT)
        self.closed = False

    def queue_account_event(self, account_event: StreamedAccountEvent) -> bool:
        """False when the queue is full: the stream is closed instead."""
        try:
            self.queue.put_nowait(account_event)
        except asyncio.QueueFull:
            self.close()
            return False
        return True

    def close(self) -> None:
        self.closed = True
        # A waiting stream wakes on the None; a full queue's stream is not waiting and sees ``closed`` next.
        with suppress(asyncio.QueueFull):
            self.queue.put_nowait(None)


class AccountEventStreamService:
    def __init__(self, *, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory
        self.subscriptions_by_account_id: dict[int, set[AccountEventSubscription]] = {}
        self.subscriber_present = asyncio.Event()
        # None while idle: the first pass after starts from the newest id.
        self.cursor_event_id: int | None = None
        # Ids the poller handed out, with their creation times, kept for the late-commit window.
        self.queued_event_times: dict[int, datetime] = {}

    @asynccontextmanager
    async def subscribe_account(
        self, account_id: int, *, account_session_id: int
    ) -> AsyncGenerator[AccountEventSubscription]:
        subscription = AccountEventSubscription(account_id=account_id, account_session_id=account_session_id)
        self.subscriptions_by_account_id.setdefault(account_id, set()).add(subscription)
        self.subscriber_present.set()
        try:
            yield subscription
        finally:
            self.unsubscribe(subscription)

    def unsubscribe(self, subscription: AccountEventSubscription) -> None:
        account_subscriptions = self.subscriptions_by_account_id.get(subscription.account_id)
        if account_subscriptions is not None:
            account_subscriptions.discard(subscription)
            if not account_subscriptions:
                del self.subscriptions_by_account_id[subscription.account_id]
        if not self.subscriptions_by_account_id:
            self.subscriber_present.clear()

    async def poll_subscribed_account_events(self) -> None:
        """One pass of the shared poller; with no subscriber it first waits for one, querying nothing."""
        if not self.subscriptions_by_account_id:
            self.cursor_event_id = None
            self.queued_event_times = {}
            await self.subscriber_present.wait()
        subscriptions = [
            subscription
            for account_subscriptions in self.subscriptions_by_account_id.values()
            for subscription in account_subscriptions
        ]
        window_start = datetime.now(UTC) - LATE_COMMIT_WINDOW
        new_events: list[AccountEvent] = []
        async with self.session_factory() as session, session.begin():
            account_id_by_live_session_id = await AccountService.on_session(session).find_live_session_account_ids(
                sorted({subscription.account_session_id for subscription in subscriptions})
            )
            for subscription in subscriptions:
                if account_id_by_live_session_id.get(subscription.account_session_id) != subscription.account_id:
                    subscription.close()
                    self.unsubscribe(subscription)
            account_ids = sorted(self.subscriptions_by_account_id)
            if not account_ids:
                return
            repository = AccountEventRepository(session=session)
            cursor_event_id = self.cursor_event_id
            if cursor_event_id is None:
                cursor_event_id = await repository.find_latest_account_event_id() or 0
            after_page_event_id = 0
            while True:
                page = await repository.list_events_of_accounts_after(
                    account_ids,
                    after_event_id=cursor_event_id,
                    created_since=window_start,
                    after_page_event_id=after_page_event_id,
                    limit=EVENTS_PER_POLL,
                )
                new_events.extend(page)
                if len(page) < EVENTS_PER_POLL:
                    break
                after_page_event_id = page[-1].id
        for account_event in new_events:
            if account_event.id in self.queued_event_times or account_event.account_id is None:
                continue
            self.queued_event_times[account_event.id] = account_event.created_at
            cursor_event_id = max(cursor_event_id, account_event.id)
            self.queue_for_account(
                account_event.account_id, account_event=present_streamed_account_event(account_event)
            )
        self.cursor_event_id = cursor_event_id
        self.queued_event_times = {
            event_id: created_at
            for event_id, created_at in self.queued_event_times.items()
            if created_at >= window_start
        }

    def queue_for_account(self, account_id: int, *, account_event: StreamedAccountEvent) -> None:
        for subscription in list(self.subscriptions_by_account_id.get(account_id, ())):
            if not subscription.queue_account_event(account_event):
                self.unsubscribe(subscription)
                log.emit(
                    logger,
                    event=AccountEventStreamSubscriberDropped(
                        account_id=account_id, queued_event_count=subscription.queue.qsize()
                    ),
                )

    async def read_account_events_after(
        self, account_id: int, *, after_event_id: int, created_since: datetime | None
    ) -> list[AccountEvent]:
        async with self.session_factory() as session, session.begin():
            return await AccountEventRepository(session=session).list_account_events_after(
                account_id, after_event_id=after_event_id, created_since=created_since, limit=EVENTS_PER_POLL
            )

    async def resolve_last_event_id(self, account_id: int, *, last_event_public_id: str | None) -> int | None:
        """The internal id a reconnect resumes after; None without a well-formed ``Last-Event-ID`` or for an id that
        is not one of this account's events."""
        if last_event_public_id is None or not ACCOUNT_EVENT_PUBLIC_ID_PATTERN.fullmatch(last_event_public_id):
            return None
        async with self.session_factory() as session, session.begin():
            return await AccountEventRepository(session=session).find_account_event_id_by_public_id(
                account_id, public_id=last_event_public_id
            )

    async def read_stream_backlog(
        self, account_id: int, *, last_event_public_id: str | None, last_event_id: int | None
    ) -> tuple[list[AccountEvent], bool]:
        """What a connection sends before ``caught_up``, and whether more pages of it wait."""
        # A tab from before random ids resends its numeric id: caught up at once, so no burst of old toasts.
        if last_event_public_id is not None and not ACCOUNT_EVENT_PUBLIC_ID_PATTERN.fullmatch(last_event_public_id):
            return [], False
        if last_event_id is None:
            async with self.session_factory() as session, session.begin():
                latest_events = await AccountEventRepository(session=session).list_latest_account_events(
                    account_id, limit=BACKLOG_EVENT_COUNT
                )
            return latest_events, True
        page = await self.read_account_events_after(
            account_id, after_event_id=last_event_id, created_since=datetime.now(UTC) - LATE_COMMIT_WINDOW
        )
        return page, len(page) == EVENTS_PER_POLL

    async def follow_account_events(
        self, account_id: int, *, account_session_id: int, last_event_public_id: str | None
    ) -> AsyncIterator[AccountEventResponse | AccountEventsCaughtUpResponse]:
        """The account's latest events (or those after ``Last-Event-ID`` on a reconnect), a caught-up marker, then
        new events until the client leaves, its session stops being valid or it falls too far behind. Ids are
        compared by the internal key; only public ids are sent."""
        async with self.subscribe_account(account_id, account_session_id=account_session_id) as subscription:
            last_event_id = await self.resolve_last_event_id(account_id, last_event_public_id=last_event_public_id)
            backlog, more_backlog_waiting = await self.read_stream_backlog(
                account_id, last_event_public_id=last_event_public_id, last_event_id=last_event_id
            )
            sent_event_times: dict[int, datetime] = {}
            cursor_event_id = last_event_id or 0
            cursor_event_public_id = last_event_public_id if last_event_id is not None else None
            # After the latest events, the first page read re-reads the late-commit window; after a reconnect's
            # backlog, which read it, none.
            created_since = datetime.now(UTC) - LATE_COMMIT_WINDOW if last_event_id is None else None
            for account_event in backlog:
                sent_event_times[account_event.id] = account_event.created_at
                if account_event.id > cursor_event_id:
                    cursor_event_id, cursor_event_public_id = account_event.id, account_event.public_id
                yield present_account_event(account_event)
            yield AccountEventsCaughtUpResponse(latest_event_id=cursor_event_public_id)
            # The rest a page at a time, as the per-connection poll read them: after the latest events, its first
            # read (the late-commit window included); after a full reconnect page, the pages past it.
            while more_backlog_waiting:
                page = await self.read_account_events_after(
                    account_id, after_event_id=cursor_event_id, created_since=created_since
                )
                created_since = None
                for account_event in page:
                    if account_event.id in sent_event_times:
                        continue
                    sent_event_times[account_event.id] = account_event.created_at
                    cursor_event_id = max(cursor_event_id, account_event.id)
                    yield present_account_event(account_event)
                more_backlog_waiting = len(page) == EVENTS_PER_POLL
            while True:
                streamed_event = await subscription.queue.get()
                if streamed_event is None or subscription.closed:
                    return
                window_start = datetime.now(UTC) - LATE_COMMIT_WINDOW
                sent_event_times = {
                    event_id: created_at
                    for event_id, created_at in sent_event_times.items()
                    if created_at >= window_start
                }
                if streamed_event.account_event_id in sent_event_times or (
                    streamed_event.account_event_id <= cursor_event_id and streamed_event.created_at < window_start
                ):
                    continue
                sent_event_times[streamed_event.account_event_id] = streamed_event.created_at
                cursor_event_id = max(cursor_event_id, streamed_event.account_event_id)
                yield streamed_event.account_event_response
