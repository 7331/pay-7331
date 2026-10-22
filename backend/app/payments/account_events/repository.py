"""Queries for account_events; no decisions."""

from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.account_events.models import AccountEvent, OperatorAlertProblem
from app.payments.account_events.webhook_event_scope import webhook_event_condition


class AccountEventRepository:
    def __init__(self, *, session: AsyncSession) -> None:
        self.session = session

    async def lock_operator_alert_recording(self) -> None:
        """Serialize operator alert recording until the transaction ends, across every process."""
        await self.session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended("operator_alert_recording", 0)))
        )

    async def insert_account_event(
        self, invoice_id: str | None, *, account_id: int | None, event_type: str, data: JsonValue
    ) -> None:
        self.session.add(AccountEvent(invoice_id=invoice_id, account_id=account_id, event_type=event_type, data=data))
        await self.session.flush()

    async def list_latest_account_events(self, account_id: int, *, limit: int) -> list[AccountEvent]:
        """The account's newest ``limit`` events, oldest first."""
        newest_first = await self.session.scalars(
            select(AccountEvent)
            .where(AccountEvent.account_id == account_id)
            .order_by(AccountEvent.id.desc())
            .limit(limit)
        )
        return list(reversed(list(newest_first)))

    async def list_account_events_page(
        self, account_id: int, *, before_event_id: int | None, limit: int, webhook_events_only: bool = False
    ) -> list[AccountEvent]:
        """Up to ``limit`` of the account's events below ``before_event_id`` (None: from the newest), newest first, read in
        index order (``ix_account_events_account_id_id`` or the primary key backwards, the planner's choice);
        ``webhook_events_only`` keeps those the webhook sends (``webhook_event_scope.py``)."""
        query = select(AccountEvent).where(AccountEvent.account_id == account_id)
        if webhook_events_only:
            query = query.where(webhook_event_condition())
        if before_event_id is not None:
            query = query.where(AccountEvent.id < before_event_id)
        return list(await self.session.scalars(query.order_by(AccountEvent.id.desc()).limit(limit)))

    async def list_account_events_after(
        self, account_id: int, *, after_event_id: int, created_since: datetime | None, limit: int
    ) -> list[AccountEvent]:
        """The account's events past ``after_event_id``, plus any made since ``created_since`` (None: none): an id
        taken by a transaction that committed after a higher one is still found."""
        newer_or_recent = AccountEvent.id > after_event_id
        if created_since is not None:
            newer_or_recent = or_(newer_or_recent, AccountEvent.created_at >= created_since)
        return list(
            await self.session.scalars(
                select(AccountEvent)
                .where(AccountEvent.account_id == account_id, newer_or_recent)
                .order_by(AccountEvent.id)
                .limit(limit)
            )
        )

    async def find_account_event_id_by_public_id(self, account_id: int, *, public_id: str) -> int | None:
        """The internal id of the account's event with that public id; None for an unknown or another account's."""
        return await self.session.scalar(
            select(AccountEvent.id).where(AccountEvent.public_id == public_id, AccountEvent.account_id == account_id)
        )

    async def find_latest_account_event_id(self) -> int | None:
        return await self.session.scalar(select(func.max(AccountEvent.id)))

    async def list_events_of_accounts_after(
        self,
        account_ids: list[int],
        *,
        after_event_id: int,
        created_since: datetime,
        after_page_event_id: int,
        limit: int,
    ) -> list[AccountEvent]:
        """One page, by id past ``after_page_event_id``, of those accounts' events past ``after_event_id`` or made since
        ``created_since`` (the stream's shared poller; the late-commit window as in ``list_account_events_after``)."""
        return list(
            await self.session.scalars(
                select(AccountEvent)
                .where(
                    AccountEvent.account_id.in_(account_ids),
                    or_(AccountEvent.id > after_event_id, AccountEvent.created_at >= created_since),
                    AccountEvent.id > after_page_event_id,
                )
                .order_by(AccountEvent.id)
                .limit(limit)
            )
        )

    async def count_invoice_events(
        self, invoice_ids: list[str], *, event_types: list[str]
    ) -> list[tuple[str, str, int]]:
        """(invoice id, event type, rows) of those invoices and types; a pair with no row is left out."""
        rows = await self.session.execute(
            select(AccountEvent.invoice_id, AccountEvent.event_type, func.count())
            .where(AccountEvent.invoice_id.in_(invoice_ids), AccountEvent.event_type.in_(event_types))
            .group_by(AccountEvent.invoice_id, AccountEvent.event_type)
        )
        return [(invoice_id or "", event_type, count) for invoice_id, event_type, count in rows]

    async def list_invoice_ids_with_events(self, invoice_ids: list[str]) -> set[str]:
        invoice_ids_with_events = await self.session.scalars(
            select(AccountEvent.invoice_id).where(AccountEvent.invoice_id.in_(invoice_ids)).distinct()
        )
        return {invoice_id for invoice_id in invoice_ids_with_events if invoice_id is not None}

    async def delete_operator_alert_problems_last_seen_before(self, last_seen_before: datetime) -> None:
        await self.session.execute(
            delete(OperatorAlertProblem).where(OperatorAlertProblem.last_seen_at < last_seen_before)
        )

    async def list_operator_alert_problems(self, problem_keys: list[str]) -> dict[str, OperatorAlertProblem]:
        problems = await self.session.scalars(
            select(OperatorAlertProblem).where(OperatorAlertProblem.problem_key.in_(problem_keys))
        )
        return {problem.problem_key: problem for problem in problems}

    async def insert_operator_alert_problem(self, problem_key: str, *, seen_at: datetime) -> OperatorAlertProblem:
        problem = OperatorAlertProblem(
            problem_key=problem_key, created_at=seen_at, last_seen_at=seen_at, last_alerted_at=seen_at
        )
        self.session.add(problem)
        await self.session.flush()
        return problem

    async def claim_account_events_for_webhooks(self, *, limit: int) -> list[AccountEvent]:
        """Marks and returns up to ``limit`` rows no webhook pass took yet, oldest first, of any type and account
        (``FOR UPDATE SKIP LOCKED``: a second claimer at the same moment takes other rows,
        https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE). A row is only visible once
        committed, so an id taken before a lower one commits is still found later: nothing here orders by a high-water
        mark."""
        claimable_event_ids = (
            select(AccountEvent.id)
            .where(AccountEvent.webhook_claimed_at.is_(None))
            .order_by(AccountEvent.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        claimed_events = await self.session.scalars(
            update(AccountEvent)
            .where(AccountEvent.id.in_(claimable_event_ids))
            .values(webhook_claimed_at=func.clock_timestamp())
            .returning(AccountEvent)
        )
        return sorted(claimed_events, key=lambda account_event: account_event.id)
