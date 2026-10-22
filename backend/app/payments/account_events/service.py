"""AccountEventService: each account's event feed (PLAN.md section 8.1).

``record_account_event`` writes a row on the caller's session, so it commits with the change it describes; an
invoice made signed out has no account, so its rows are kept and streamed to nobody. ``change_payout_address`` changes
the account's payout address behind its fresh passkey check and records ``account.payout_address_changed`` (no invoice)
in the same transaction, so the account's activity always shows it; it lives here because this
domain sits above accounts. ``enroll_passkey_by_link`` adds a passkey through an operator-minted
enrollment link and records ``account.passkey_enrolled_by_link`` (2026-10-15), here for the same reason, as do
``add_passkey`` and ``delete_passkey`` (each behind its own fresh passkey check), which record
``account.passkey_added`` and ``account.passkey_deleted``.
``list_account_events_page`` pages the account's history back by event id (``GET
/events``). The SSE stream's source is ``stream_service.py``.
"""

import binascii
from datetime import datetime
import re
from typing import Self

from pydantic import JsonValue, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.account_events.enums import AccountEventType
from app.payments.account_events.models import AccountEvent, OperatorAlertProblem
from app.payments.account_events.repository import AccountEventRepository
from app.payments.account_events.schemas import AccountEventPageResponse, AccountEventResponse
from app.payments.account_events.types import ClaimedAccountEvent
from app.payments.accounts.account_passkey_service import AccountPasskeyService
from app.payments.accounts.passkey_enrollment_link_service import PasskeyEnrollmentLinkService
from app.payments.accounts.service import AccountService
from app.payments.accounts.types import (
    AccountAccess,
    AccountPasskeySummary,
    GatewayDepositAddressCheck,
    PasskeyEnrollment,
    PayoutAddressChange,
)
from app.shared.cursors import decode_url_safe_base64_cursor
from app.shared.errors import InvalidInputError

# An event's public id: the SSE id, `Last-Event-ID` and the page cursor (the last event of the page).
ACCOUNT_EVENT_PUBLIC_ID_PATTERN = re.compile(r"evt_[1-9A-HJ-NP-Za-km-z]{22}")
# The cursor before migration 0036, base64url of this prefix and the internal id; still read, no longer handed out.
LEGACY_ACCOUNT_EVENT_CURSOR_PREFIX = "before_event_id:"
# account_events.id is a Postgres bigint.
LARGEST_ACCOUNT_EVENT_ID = 2**63 - 1
INVALID_CURSOR_MESSAGE = "cursor: not a cursor this API returned"


def decode_legacy_account_event_cursor(cursor: str) -> int:
    try:
        cursor_text = decode_url_safe_base64_cursor(cursor)
    except (ValueError, binascii.Error) as exc:
        raise InvalidInputError(INVALID_CURSOR_MESSAGE) from exc
    event_id_text = cursor_text.removeprefix(LEGACY_ACCOUNT_EVENT_CURSOR_PREFIX)
    is_event_id = event_id_text != cursor_text and event_id_text.isascii() and event_id_text.isdecimal()
    if not is_event_id or not 1 <= int(event_id_text) <= LARGEST_ACCOUNT_EVENT_ID:
        raise InvalidInputError(INVALID_CURSOR_MESSAGE)
    return int(event_id_text)


def present_account_event(account_event: AccountEvent) -> AccountEventResponse:
    return AccountEventResponse(
        id=account_event.public_id,
        type=account_event.event_type,
        invoice_id=account_event.invoice_id,
        created_at=account_event.created_at,
        data=account_event.data,
    )


class AccountEventService:
    def __init__(
        self,
        *,
        repository: AccountEventRepository,
        account_service: AccountService,
        account_passkey_service: AccountPasskeyService,
        passkey_enrollment_link_service: PasskeyEnrollmentLinkService,
    ) -> None:
        self.repository = repository
        self.account_service = account_service
        self.account_passkey_service = account_passkey_service
        self.passkey_enrollment_link_service = passkey_enrollment_link_service

    @classmethod
    def on_session(cls, session: AsyncSession) -> Self:
        return cls(
            repository=AccountEventRepository(session=session),
            account_service=AccountService.on_session(session),
            account_passkey_service=AccountPasskeyService.on_session(session),
            passkey_enrollment_link_service=PasskeyEnrollmentLinkService.on_session(session),
        )

    async def record_account_event(
        self, invoice_id: str | None, *, account_id: int | None, event_type: str, data: JsonValue
    ) -> None:
        await self.repository.insert_account_event(invoice_id, account_id=account_id, event_type=event_type, data=data)

    async def resolve_account_event_cursor(self, account_id: int, *, cursor: str) -> int:
        """The internal id the page starts below: an ``evt_`` cursor of one of this account's events, or the legacy
        base64 form; 400 for anything else, an unknown or another account's event included."""
        if not ACCOUNT_EVENT_PUBLIC_ID_PATTERN.fullmatch(cursor):
            return decode_legacy_account_event_cursor(cursor)
        account_event_id = await self.repository.find_account_event_id_by_public_id(account_id, public_id=cursor)
        if account_event_id is None:
            raise InvalidInputError(INVALID_CURSOR_MESSAGE)
        return account_event_id

    async def list_account_events_page(
        self, account_id: int, *, cursor: str | None, page_size: int, webhook_events_only: bool = False
    ) -> AccountEventPageResponse:
        """The account's events, newest first, ``page_size`` a page; keyset by id, so rows recorded meanwhile never
        shift a page. One row past the page is read to tell whether an older page exists. ``webhook_events_only``
        (an API token): the events the webhook sends (``webhook_event_scope.py``)."""
        account_events = await self.repository.list_account_events_page(
            account_id,
            before_event_id=await self.resolve_account_event_cursor(account_id, cursor=cursor) if cursor else None,
            limit=page_size + 1,
            webhook_events_only=webhook_events_only,
        )
        page_events = account_events[:page_size]
        return AccountEventPageResponse(
            events=[present_account_event(account_event) for account_event in page_events],
            next_cursor=page_events[-1].public_id if len(account_events) > page_size else None,
        )

    async def count_invoice_events(
        self, invoice_ids: list[str], *, event_types: list[str]
    ) -> dict[tuple[str, str], int]:
        """Rows per (invoice id, event type); reconcile compares them with the operations' outcomes."""
        if not invoice_ids:
            return {}
        return {
            (invoice_id, event_type): count
            for invoice_id, event_type, count in await self.repository.count_invoice_events(
                invoice_ids, event_types=event_types
            )
        }

    async def list_invoice_ids_with_events(self, invoice_ids: list[str]) -> set[str]:
        """Those of the invoices that have any event at all."""
        return await self.repository.list_invoice_ids_with_events(invoice_ids) if invoice_ids else set()

    async def lock_operator_alert_recording(self) -> None:
        """Held until the transaction ends, so two processes reading which alerts went out never both record one."""
        await self.repository.lock_operator_alert_recording()

    async def delete_operator_alert_problems_last_seen_before(self, last_seen_before: datetime) -> None:
        """Forgets the problems not seen since then: they cleared, so one that comes back alerts anew."""
        await self.repository.delete_operator_alert_problems_last_seen_before(last_seen_before)

    async def list_operator_alert_problems(self, problem_keys: list[str]) -> dict[str, OperatorAlertProblem]:
        return await self.repository.list_operator_alert_problems(problem_keys)

    async def insert_operator_alert_problem(self, problem_key: str, *, seen_at: datetime) -> OperatorAlertProblem:
        return await self.repository.insert_operator_alert_problem(problem_key, seen_at=seen_at)

    async def claim_account_events_for_webhooks(self, *, limit: int) -> tuple[int, list[ClaimedAccountEvent]]:
        """Webhook and Discord delivery (payments/account_webhooks): takes up to ``limit`` rows no webhook pass took
        yet, each for good, and answers how many it took and those of an account; each receiver kind picks the types
        it sends (the webhook invoice events, Discord those and ``operator.alert``)."""
        claimed_events = await self.repository.claim_account_events_for_webhooks(limit=limit)
        return len(claimed_events), [
            ClaimedAccountEvent(
                account_id=account_event.account_id,
                account_event_id=account_event.id,
                account_event_response=present_account_event(account_event),
            )
            for account_event in claimed_events
            if account_event.account_id is not None
        ]

    async def change_payout_address(
        self,
        account_access: AccountAccess,
        *,
        payout_address: str,
        credential_json: dict[str, JsonValue],
        gateway_deposit_address_check: GatewayDepositAddressCheck,
    ) -> PayoutAddressChange:
        """``AccountService.change_payout_address`` plus its account event, one transaction."""
        payout_address_change = await self.account_service.change_payout_address(
            account_access,
            payout_address=payout_address,
            credential_json=credential_json,
            gateway_deposit_address_check=gateway_deposit_address_check,
        )
        await self.record_account_event(
            None,
            account_id=account_access.account_id,
            event_type=AccountEventType.PAYOUT_ADDRESS_CHANGED,
            data=payout_address_change.model_dump(mode="json"),
        )
        return payout_address_change

    async def enroll_passkey_by_link(
        self, token: SecretStr, *, credential_json: dict[str, JsonValue], label: str
    ) -> PasskeyEnrollment:
        """``PasskeyEnrollmentLinkService.enroll_passkey`` plus its account event, one transaction."""
        passkey_enrollment = await self.passkey_enrollment_link_service.enroll_passkey(
            token, credential_json=credential_json, label=label
        )
        await self.record_account_event(
            None,
            account_id=passkey_enrollment.account_id,
            event_type=AccountEventType.PASSKEY_ENROLLED_BY_LINK,
            data={},
        )
        return passkey_enrollment

    async def add_passkey(
        self, account_access: AccountAccess, *, credential_json: dict[str, JsonValue], label: str
    ) -> AccountPasskeySummary:
        """``AccountPasskeyService.add_passkey`` plus its account event, one transaction."""
        account_passkey_summary = await self.account_passkey_service.add_passkey(
            account_access, credential_json=credential_json, label=label
        )
        await self.record_account_event(
            None,
            account_id=account_access.account_id,
            event_type=AccountEventType.PASSKEY_ADDED,
            data={},
        )
        return account_passkey_summary

    async def delete_passkey(
        self, account_access: AccountAccess, *, account_passkey_public_id: str, credential_json: dict[str, JsonValue]
    ) -> None:
        """``AccountPasskeyService.delete_passkey`` plus its account event, one transaction."""
        await self.account_passkey_service.delete_passkey(
            account_access, account_passkey_public_id=account_passkey_public_id, credential_json=credential_json
        )
        await self.record_account_event(
            None,
            account_id=account_access.account_id,
            event_type=AccountEventType.PASSKEY_DELETED,
            data={},
        )
