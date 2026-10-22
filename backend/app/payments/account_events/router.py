"""The signed-in account's event stream (PLAN.md section 8.1): Server-Sent Events under the passkey session. Its
history a page at a time (``GET /events``), by session, or by API token with its invoice events only (2026-10-21).

A session holds at most ``MAXIMUM_OPEN_STREAMS_PER_ACCOUNT_SESSION`` streams open at once, counted across every API
process (``stream_slot_store.py``); one more answers 429 ``too_many_event_streams`` before the stream starts, and a
stream's slot is released when it ends. The slot is held by a dependency with ``yield``, whose exit code runs after
the response is sent (https://fastapi.tiangolo.com/tutorial/dependencies/dependencies-with-yield/), so for a stream
only once it has ended. API tokens cannot open a stream (session only).

The page reads it with ``fetch`` (``EventSource`` cannot send ``Last-Event-ID`` on the first connect) and sends the
last id it saw as ``Last-Event-ID`` when it reconnects; the session cookie rides along. The process's shared poller
(``stream_service.py``) feeds every open stream.

Also the payout address change (PLAN.md section 7): its passkey options and the change itself, which
records an account event, so both routes live here, above accounts. Adding and deleting a passkey too, each behind a
fresh passkey check, for the same reason. So does enrolling a passkey through an
operator-minted link (2026-10-15): public like sign-up, the link's token in the body, and it signs the browser in.
"""

import asyncio
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import suppress
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import JsonValue, SecretStr

from app.lifespan.dependencies import DatabaseSession, RedisDependency, ResourcesDependency
from app.payments.account_events.errors import TooManyAccountEventStreamsError
from app.payments.account_events.schemas import AccountEventPageResponse, AccountEventResponse
from app.payments.account_events.service import AccountEventService
from app.payments.account_events.stream_slot_store import (
    MAXIMUM_OPEN_STREAMS_PER_ACCOUNT_SESSION,
    STREAM_SLOT_RENEW_INTERVAL_SECONDS,
    AccountEventStreamSlotStore,
)
from app.payments.accounts.dependencies import (
    AccountAccessDependency,
    AccountOrApiTokenAccessDependency,
    AccountPasskeyServiceDependency,
    AccountServiceDependency,
    PasskeyEnrollmentLinkServiceDependency,
)
from app.payments.accounts.router import (
    NO_STORE_HEADERS,
    present_account_passkey_response,
    present_account_session_response,
    start_session_response,
)
from app.payments.accounts.schemas import (
    AccountPasskeyResponse,
    AccountSessionResponse,
    ChangePayoutAddressRequest,
    EnrollPasskeyRequest,
    FreshPasskeyCheckRequest,
    PasskeyCredentialRequest,
    PasskeyEnrollmentOptionsRequest,
    PayoutAddressChangeOptionsRequest,
)
from app.payments.accounts.types import AccountAccess
from app.shared.path_params import API_PREFIX, AccountPasskeyIdPath

account_events_router = APIRouter(prefix=API_PREFIX)


def get_account_event_service(session: DatabaseSession) -> AccountEventService:
    return AccountEventService.on_session(session)


AccountEventServiceDependency = Annotated[AccountEventService, Depends(get_account_event_service)]

RECONNECT_DELAY_MILLISECONDS = 3000
ACCOUNT_EVENT_STREAM_DESCRIPTION = (
    "A text/event-stream of two event names. `account_event`: `data` is an AccountEventResponse as JSON and `id` its event "
    "id (`evt_...`); send the last one back as `Last-Event-ID` to resume after it (an unknown one starts over, as a "
    "first connect; one that is not an `evt_` id gets `caught_up` at once, with no backlog). `caught_up`: sent once "
    'the backlog is out, `data` is `{"latest_event_id": <id or null>}` and `retry` the reconnect delay in '
    "milliseconds. Comment lines keep the connection alive."
)
DEFAULT_ACCOUNT_EVENTS_PER_PAGE = 50
MAXIMUM_ACCOUNT_EVENTS_PER_PAGE = 200


@account_events_router.get("/events", tags=["events"])
async def list_account_events(
    *,
    response: Response,
    account_access: AccountOrApiTokenAccessDependency,
    account_event_service: AccountEventServiceDependency,
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=DEFAULT_ACCOUNT_EVENTS_PER_PAGE, ge=1, le=MAXIMUM_ACCOUNT_EVENTS_PER_PAGE),
) -> AccountEventPageResponse:
    """The account's events, newest first; ``next_cursor`` gives the older page. By API token only the events the
    account webhook sends: never ``operator.alert`` or an ``account.*`` event, which name the operator's problems or
    the account's addresses."""
    # One rule for the token's list and the webhook: webhook_event_scope.py.
    response.headers.update(NO_STORE_HEADERS)
    return await account_event_service.list_account_events_page(
        account_access.account_id,
        cursor=cursor,
        page_size=limit,
        webhook_events_only=not isinstance(account_access, AccountAccess),
    )


async def renew_stream_slot_until_cancelled(
    stream_slot_store: AccountEventStreamSlotStore, *, account_session_id: int, stream_slot_id: str
) -> None:
    while True:
        await asyncio.sleep(STREAM_SLOT_RENEW_INTERVAL_SECONDS)
        await stream_slot_store.renew_stream_slot(account_session_id, stream_slot_id=stream_slot_id)


async def hold_account_event_stream_slot(
    *, account_access: AccountAccessDependency, redis: RedisDependency
) -> AsyncIterator[None]:
    """One of the session's stream slots for as long as the stream is open, renewed while it is; 429 when all are
    held."""
    stream_slot_store = AccountEventStreamSlotStore(client=redis)
    stream_slot_id = secrets.token_hex(16)
    account_session_id = account_access.account_session_id
    if not await stream_slot_store.claim_stream_slot(account_session_id, stream_slot_id=stream_slot_id):
        raise TooManyAccountEventStreamsError(
            f"this session already has {MAXIMUM_OPEN_STREAMS_PER_ACCOUNT_SESSION} event streams open; close one first"
        )
    renewal_task = asyncio.create_task(
        renew_stream_slot_until_cancelled(
            stream_slot_store, account_session_id=account_session_id, stream_slot_id=stream_slot_id
        )
    )
    try:
        yield
    finally:
        renewal_task.cancel()
        with suppress(asyncio.CancelledError):
            await renewal_task
        await stream_slot_store.release_stream_slot(account_session_id, stream_slot_id=stream_slot_id)


@account_events_router.get(
    "/events/stream",
    response_class=EventSourceResponse,
    response_description=ACCOUNT_EVENT_STREAM_DESCRIPTION,
    dependencies=[Depends(hold_account_event_stream_slot)],
    tags=["events"],
)
async def stream_account_events(
    *,
    account_access: AccountAccessDependency,
    resources: ResourcesDependency,
    # Not validated here: a malformed or unknown id is never refused (stream_service.py).
    last_event_id: Annotated[str | None, Header()] = None,
) -> AsyncIterable[ServerSentEvent]:
    """The account's events as they happen, as Server-Sent Events, behind the passkey session (an API token cannot
    open one). First the events after ``Last-Event-ID`` (none on a first connect), then ``caught_up``, then each new
    event. A session holds a few streams open at once; one more answers 429 ``too_many_event_streams``."""
    async for item in resources.account_event_stream_service.follow_account_events(
        account_access.account_id,
        account_session_id=account_access.account_session_id,
        last_event_public_id=last_event_id,
    ):
        if isinstance(item, AccountEventResponse):
            yield ServerSentEvent(data=item, event="account_event", id=str(item.id))
        else:
            yield ServerSentEvent(data=item, event="caught_up", retry=RECONNECT_DELAY_MILLISECONDS)


@account_events_router.post("/accounts/current/payout-address-change-options", tags=["accounts"])
async def create_payout_address_change_options(
    options_request: PayoutAddressChangeOptionsRequest,
    *,
    response: Response,
    account_access: AccountAccessDependency,
    account_service: AccountServiceDependency,
    resources: ResourcesDependency,
) -> dict[str, JsonValue]:
    """WebAuthn request options (``PublicKeyCredentialRequestOptionsJSON``) for the passkey check that approves this
    new payout address: only the account's passkeys, user verification required, a challenge for this account and
    this address only, single use, 10 minutes."""
    response.headers.update(NO_STORE_HEADERS)
    return await account_service.create_payout_address_change_options(
        account_access,
        payout_address=options_request.payout_address,
        gateway_deposit_address_check=resources.invoice_wiring.gateway_deposit_address_check,
    )


@account_events_router.put("/accounts/current/payout-address", tags=["accounts"])
async def change_payout_address(
    change_request: ChangePayoutAddressRequest,
    *,
    response: Response,
    account_access: AccountAccessDependency,
    account_event_service: AccountEventServiceDependency,
    resources: ResourcesDependency,
) -> AccountSessionResponse:
    """Changes the payout address once the passkey check verifies, records ``account.payout_address_changed`` and
    answers the session view. Existing invoices keep their destination."""
    response.headers.update(NO_STORE_HEADERS)
    payout_address_change = await account_event_service.change_payout_address(
        account_access,
        payout_address=change_request.payout_address,
        credential_json=change_request.credential,
        gateway_deposit_address_check=resources.invoice_wiring.gateway_deposit_address_check,
    )
    return present_account_session_response(account_access.with_payout_address(payout_address_change.payout_address))


@account_events_router.post("/account-passkeys/addition-options", tags=["passkeys"])
async def create_add_passkey_approval_options(
    *,
    response: Response,
    account_access: AccountAccessDependency,
    account_passkey_service: AccountPasskeyServiceDependency,
) -> dict[str, JsonValue]:
    """WebAuthn request options for the fresh passkey check that approves one more passkey: only the account's
    passkeys, user verification required, single use, 10 minutes."""
    response.headers.update(NO_STORE_HEADERS)
    return await account_passkey_service.create_add_passkey_approval_options(account_access)


@account_events_router.post("/account-passkeys/registration-options", tags=["passkeys"])
async def create_add_passkey_options(
    approval_request: FreshPasskeyCheckRequest,
    *,
    response: Response,
    account_access: AccountAccessDependency,
    account_passkey_service: AccountPasskeyServiceDependency,
) -> dict[str, JsonValue]:
    """Registration options (``PublicKeyCredentialCreationOptionsJSON``) for one more passkey, once the check from
    ``POST /account-passkeys/addition-options`` verifies."""
    response.headers.update(NO_STORE_HEADERS)
    return await account_passkey_service.create_add_passkey_options(
        account_access, approval_credential_json=approval_request.credential
    )


@account_events_router.post("/account-passkeys", status_code=201, tags=["passkeys"])
async def add_account_passkey(
    passkey_request: PasskeyCredentialRequest,
    *,
    response: Response,
    account_access: AccountAccessDependency,
    account_event_service: AccountEventServiceDependency,
) -> AccountPasskeyResponse:
    """Adds the passkey made for those registration options and records ``account.passkey_added``."""
    response.headers.update(NO_STORE_HEADERS)
    summary = await account_event_service.add_passkey(
        account_access, credential_json=passkey_request.credential, label=passkey_request.label
    )
    return present_account_passkey_response(summary)


@account_events_router.post("/account-passkeys/{account_passkey_id}/deletion-options", tags=["passkeys"])
async def create_delete_passkey_options(
    account_passkey_id: AccountPasskeyIdPath,
    *,
    response: Response,
    account_access: AccountAccessDependency,
    account_passkey_service: AccountPasskeyServiceDependency,
) -> dict[str, JsonValue]:
    """WebAuthn request options for the fresh passkey check that approves deleting this passkey only; 404
    ``account_passkey_not_found``, 409 ``last_passkey``."""
    response.headers.update(NO_STORE_HEADERS)
    return await account_passkey_service.create_delete_passkey_options(
        account_access, account_passkey_public_id=account_passkey_id
    )


@account_events_router.post("/account-passkeys/{account_passkey_id}/deletion", status_code=204, tags=["passkeys"])
async def delete_account_passkey(
    account_passkey_id: AccountPasskeyIdPath,
    *,
    deletion_request: FreshPasskeyCheckRequest,
    account_access: AccountAccessDependency,
    account_event_service: AccountEventServiceDependency,
) -> None:
    """Deletes the passkey once its check verifies (its sessions end with it) and records
    ``account.passkey_deleted``."""
    await account_event_service.delete_passkey(
        account_access, account_passkey_public_id=account_passkey_id, credential_json=deletion_request.credential
    )


@account_events_router.post("/passkey-enrollment-links/registration-options", tags=["passkeys"])
async def create_passkey_enrollment_options(
    options_request: PasskeyEnrollmentOptionsRequest,
    *,
    response: Response,
    passkey_enrollment_link_service: PasskeyEnrollmentLinkServiceDependency,
) -> dict[str, JsonValue]:
    """Registration options (``PublicKeyCredentialCreationOptionsJSON``) for the link's account, as when adding a
    passkey; 404 ``passkey_enrollment_link_unknown``, 410 ``passkey_enrollment_link_used`` or ``_expired``."""
    response.headers.update(NO_STORE_HEADERS)
    return await passkey_enrollment_link_service.create_enrollment_options(SecretStr(options_request.token))


@account_events_router.post("/passkey-enrollment-links/passkeys", status_code=201, tags=["passkeys"])
async def enroll_passkey_by_link(
    enroll_request: EnrollPasskeyRequest,
    *,
    response: Response,
    account_event_service: AccountEventServiceDependency,
) -> AccountSessionResponse:
    """Adds the passkey, uses up the link, records ``account.passkey_enrolled_by_link`` and sets the session cookie."""
    passkey_enrollment = await account_event_service.enroll_passkey_by_link(
        SecretStr(enroll_request.token), credential_json=enroll_request.credential, label=enroll_request.label
    )
    return start_session_response(response, started_session=passkey_enrollment.started_session)
