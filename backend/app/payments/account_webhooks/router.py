"""The signed-in account's webhook (PLAN.md sections 7 and 8.3; 2026-10-12). Passkey session cookie only, like every
account route; changes need the matching ``Origin``. Setting the URL and rotating the secret also need a fresh passkey
check from ``POST /account-webhook/change-options``.

- ``GET /account-webhook``: the URL (null when none), ``disabled_at`` (set once the receiver answered 410 Gone) and
  the newest 20 deliveries.
- ``PUT /account-webhook`` (``{"url", "credential"}``): sets or changes the URL (turning a webhook a 410 disabled back
  on); the first set answers ``secret`` once.
- ``POST /account-webhook/secret-rotation`` (``{"credential"}``): a new secret, answered once.
- ``DELETE /account-webhook``: removes it; pending deliveries fail.
- ``POST /account-webhook/test-events`` (``{"event_type"}``, optional): queues a test (202): ``webhook.test`` with a
  message, or a sample invoice in the state that event type leaves one in, marked ``"test": true`` (2026-10-21).
- ``POST /account-webhook/deliveries/{id}/resend``: queues that message again, same ``webhook-id`` and body.
- Both are 409 ``webhook_disabled`` while a 410 keeps the webhook disabled.

The Discord webhook (2026-10-13, PLAN.md section 8.4), the same rules (a fresh passkey check to set it, from
``POST /account-discord-webhook/change-options``):

- ``GET /account-discord-webhook``: its webhook id and thread id (never the URL or token), whether Discord deleted it,
  and the newest message's delivery.
- ``PUT /account-discord-webhook`` (``{"url", "credential"}``): sets or replaces it, at most
  ``DISCORD_WEBHOOK_CHANGES_PER_DAY`` a day per account, counted once the URL and passkey check pass (else 429
  ``rate_limited``): every new webhook can earn the API's IP invalid requests from Discord until it is found deleted,
  and those are limited per IP (store.py). 409 ``discord_webhook_in_use`` when another account has that webhook.
- ``PUT /account-discord-webhook/operator-alert-repeat`` (``{"repeat_hours"}``, 0 to 168): on the operator's account
  only (else 409 ``not_operator_account``) and with a webhook set (else 409 ``discord_webhook_not_set``), how often an
  operator alert that lasts is raised again; 0 alerts once per problem (2026-10-18). Session only: it changes no
  destination.
- ``DELETE /account-discord-webhook``: removes it; pending messages fail.
- ``POST /account-discord-webhook/test-messages``: queues a ``discord.test`` message (202).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response, status
from pydantic import JsonValue

from app.lifespan.dependencies import DatabaseSession, ResourcesDependency
from app.payments.account_webhooks.discord_service import AccountDiscordWebhookService
from app.payments.account_webhooks.schemas import (
    AccountDiscordWebhookResponse,
    AccountWebhookDeliveryResponse,
    AccountWebhookResponse,
    ChangedAccountWebhookResponse,
    RotateAccountWebhookSecretRequest,
    SendTestWebhookEventRequest,
    SetAccountDiscordWebhookRequest,
    SetAccountWebhookRequest,
    SetOperatorAlertRepeatRequest,
)
from app.payments.account_webhooks.service import AccountWebhookService
from app.payments.accounts.dependencies import AccountAccessDependency
from app.payments.accounts.router import NO_STORE_HEADERS
from app.shared.errors import RateLimitedError
from app.shared.path_params import API_PREFIX

account_webhooks_router = APIRouter(prefix=API_PREFIX)

DISCORD_WEBHOOK_CHANGES_PER_DAY = 10
SECONDS_PER_DAY = 86400


def get_account_webhook_service(session: DatabaseSession) -> AccountWebhookService:
    return AccountWebhookService.on_session(session)


AccountWebhookServiceDependency = Annotated[AccountWebhookService, Depends(get_account_webhook_service)]
# Public ids: a prefix and 22 base58 characters (app/shared/identifiers.py).
AccountWebhookDeliveryIdPath = Annotated[str, Path(pattern=r"^whd_[1-9A-HJ-NP-Za-km-z]{22}$")]


def get_account_discord_webhook_service(session: DatabaseSession) -> AccountDiscordWebhookService:
    return AccountDiscordWebhookService.on_session(session)


AccountDiscordWebhookServiceDependency = Annotated[
    AccountDiscordWebhookService, Depends(get_account_discord_webhook_service)
]


@account_webhooks_router.get("/account-webhook", tags=["webhooks"])
async def read_account_webhook(
    *,
    response: Response,
    account_access: AccountAccessDependency,
    account_webhook_service: AccountWebhookServiceDependency,
) -> AccountWebhookResponse:
    response.headers.update(NO_STORE_HEADERS)
    return await account_webhook_service.read_account_webhook(account_access.account_id)


@account_webhooks_router.post("/account-webhook/change-options", tags=["webhooks"])
async def create_account_webhook_change_options(
    *,
    response: Response,
    account_access: AccountAccessDependency,
    account_webhook_service: AccountWebhookServiceDependency,
) -> dict[str, JsonValue]:
    """WebAuthn request options for the passkey check a URL change or a secret rotation needs."""
    response.headers.update(NO_STORE_HEADERS)
    return await account_webhook_service.create_change_options(account_access)


@account_webhooks_router.put("/account-webhook", tags=["webhooks"])
async def set_account_webhook(
    set_request: SetAccountWebhookRequest,
    *,
    response: Response,
    account_access: AccountAccessDependency,
    account_webhook_service: AccountWebhookServiceDependency,
) -> ChangedAccountWebhookResponse:
    """Sets or changes the URL once the passkey check from ``POST /account-webhook/change-options`` verifies, turning
    a webhook disabled by a 410 back on. The first set answers ``secret``, once."""
    response.headers.update(NO_STORE_HEADERS)
    return await account_webhook_service.set_account_webhook(
        account_access, url=set_request.url, credential_json=set_request.credential
    )


@account_webhooks_router.post("/account-webhook/secret-rotation", tags=["webhooks"])
async def rotate_account_webhook_secret(
    rotation_request: RotateAccountWebhookSecretRequest,
    *,
    response: Response,
    account_access: AccountAccessDependency,
    account_webhook_service: AccountWebhookServiceDependency,
) -> ChangedAccountWebhookResponse:
    """A new signing secret once the passkey check verifies; the old one stops signing at once. Answers ``secret``,
    once."""
    response.headers.update(NO_STORE_HEADERS)
    return await account_webhook_service.rotate_account_webhook_secret(
        account_access, credential_json=rotation_request.credential
    )


@account_webhooks_router.delete("/account-webhook", status_code=status.HTTP_204_NO_CONTENT, tags=["webhooks"])
async def remove_account_webhook(
    *, account_access: AccountAccessDependency, account_webhook_service: AccountWebhookServiceDependency
) -> None:
    await account_webhook_service.remove_account_webhook(account_access)


@account_webhooks_router.post("/account-webhook/test-events", status_code=status.HTTP_202_ACCEPTED, tags=["webhooks"])
async def send_account_webhook_test_event(
    test_request: SendTestWebhookEventRequest | None = None,
    *,
    account_access: AccountAccessDependency,
    account_webhook_service: AccountWebhookServiceDependency,
    resources: ResourcesDependency,
) -> AccountWebhookDeliveryResponse:
    return await account_webhook_service.queue_test_delivery(
        account_access,
        event_type=(test_request or SendTestWebhookEventRequest()).event_type,
        chains_service=resources.invoice_wiring.chains_service,
        public_base_url=resources.invoice_wiring.public_base_url,
    )


@account_webhooks_router.post(
    "/account-webhook/deliveries/{delivery_id}/resend", status_code=status.HTTP_202_ACCEPTED, tags=["webhooks"]
)
async def resend_account_webhook_delivery(
    delivery_id: AccountWebhookDeliveryIdPath,
    *,
    account_access: AccountAccessDependency,
    account_webhook_service: AccountWebhookServiceDependency,
) -> AccountWebhookDeliveryResponse:
    return await account_webhook_service.resend_delivery(account_access, delivery_public_id=delivery_id)


@account_webhooks_router.get("/account-discord-webhook", tags=["webhooks"])
async def read_account_discord_webhook(
    *,
    response: Response,
    account_access: AccountAccessDependency,
    account_discord_webhook_service: AccountDiscordWebhookServiceDependency,
) -> AccountDiscordWebhookResponse:
    response.headers.update(NO_STORE_HEADERS)
    return await account_discord_webhook_service.read_account_discord_webhook(account_access.account_id)


@account_webhooks_router.post("/account-discord-webhook/change-options", tags=["webhooks"])
async def create_account_discord_webhook_change_options(
    *,
    response: Response,
    account_access: AccountAccessDependency,
    account_discord_webhook_service: AccountDiscordWebhookServiceDependency,
) -> dict[str, JsonValue]:
    """WebAuthn request options for the passkey check setting a Discord webhook needs."""
    response.headers.update(NO_STORE_HEADERS)
    return await account_discord_webhook_service.create_change_options(account_access)


@account_webhooks_router.put("/account-discord-webhook", tags=["webhooks"])
async def set_account_discord_webhook(
    set_request: SetAccountDiscordWebhookRequest,
    *,
    response: Response,
    account_access: AccountAccessDependency,
    account_discord_webhook_service: AccountDiscordWebhookServiceDependency,
    resources: ResourcesDependency,
) -> AccountDiscordWebhookResponse:
    response.headers.update(NO_STORE_HEADERS)
    discord_webhook_address = await account_discord_webhook_service.verify_discord_webhook_change(
        account_access, url=set_request.url, credential_json=set_request.credential
    )
    # Counted only once the URL and the passkey check passed: a refused attempt spends none of the day's changes.
    retry_after_seconds = await resources.rate_limiter.seconds_until_admitted(
        f"discord_webhook_change:{account_access.account_id}",
        limit=DISCORD_WEBHOOK_CHANGES_PER_DAY,
        window_seconds=SECONDS_PER_DAY,
    )
    if retry_after_seconds > 0:
        raise RateLimitedError("too many Discord webhook changes today", retry_after_seconds=retry_after_seconds)
    return await account_discord_webhook_service.store_account_discord_webhook(
        account_access, discord_webhook_address=discord_webhook_address
    )


@account_webhooks_router.put("/account-discord-webhook/operator-alert-repeat", tags=["webhooks"])
async def set_operator_alert_repeat(
    set_request: SetOperatorAlertRepeatRequest,
    *,
    response: Response,
    account_access: AccountAccessDependency,
    account_discord_webhook_service: AccountDiscordWebhookServiceDependency,
) -> AccountDiscordWebhookResponse:
    response.headers.update(NO_STORE_HEADERS)
    return await account_discord_webhook_service.set_operator_alert_repeat_hours(
        account_access, repeat_hours=set_request.repeat_hours
    )


@account_webhooks_router.delete("/account-discord-webhook", status_code=status.HTTP_204_NO_CONTENT, tags=["webhooks"])
async def remove_account_discord_webhook(
    *,
    account_access: AccountAccessDependency,
    account_discord_webhook_service: AccountDiscordWebhookServiceDependency,
) -> None:
    await account_discord_webhook_service.remove_account_discord_webhook(account_access)


@account_webhooks_router.post(
    "/account-discord-webhook/test-messages", status_code=status.HTTP_202_ACCEPTED, tags=["webhooks"]
)
async def send_account_discord_webhook_test_message(
    *,
    account_access: AccountAccessDependency,
    account_discord_webhook_service: AccountDiscordWebhookServiceDependency,
) -> AccountWebhookDeliveryResponse:
    return await account_discord_webhook_service.queue_test_message(account_access)
