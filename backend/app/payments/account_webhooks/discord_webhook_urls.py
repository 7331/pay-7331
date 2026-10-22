"""Which Discord webhook URLs an account may save, and where a message to one is sent (2026-10-13; PLAN.md 8.4).

A Discord channel webhook is executed by ``POST /webhooks/{webhook.id}/{webhook.token}`` under the API base URL
``https://discord.com/api``, optionally versioned as ``/api/v{n}``
(https://github.com/discord/discord-api-docs/blob/main/developers/resources/webhook.mdx, "Execute Webhook";
https://github.com/discord/discord-api-docs/blob/main/developers/reference.mdx, "Base URL" and "API Versioning").
Discord's "Copy Webhook URL" gives ``https://discord.com/api/webhooks/{id}/{token}``; the PTB and Canary clients and
old webhooks give the same path on ``ptb.discord.com``, ``canary.discord.com`` or ``discordapp.com`` (those three hosts:
what the clients hand out, not documented as a list in the sources checked).

This is the SSRF guard: only such a URL is accepted, and only its id and token are kept. Every message goes to
``https://discord.com/api/v10/webhooks/{id}/{token}`` (v10, the newest available version; an unversioned path would
reach the deprecated default v6), never to the host the account typed, so no account can point the API anywhere
else. The host is still resolved and checked at send time like any webhook's (receiver_addresses.py). The fork e2e's
``PAY_E2E_DISCORD_API_BASE_URL`` (configuration.py, ``local_fork`` only) replaces ``https://discord.com/api``.

A forum or media channel's webhook takes a message only into a thread: "you must provide either ``thread_id`` in the
query string params, or ``thread_name`` in the JSON/form params" (webhook.mdx, Execute Webhook), else Discord answers
400 with JSON code 220001. So the one query the pasted URL may carry is ``?thread_id=<snowflake>``, kept beside the id
and passed on with every message.
"""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit

from pydantic import BaseModel, ConfigDict, SecretStr

from app.payments.account_webhooks.models import WEBHOOK_URL_MAXIMUM_LENGTH
from app.shared.errors import InvalidInputError

DISCORD_API_BASE_URL = "https://discord.com/api"
DISCORD_API_VERSION = 10
DISCORD_WEBHOOK_HOSTS = frozenset({"discord.com", "ptb.discord.com", "canary.discord.com", "discordapp.com"})
# A snowflake is a 64-bit integer written in decimal (reference.mdx, "Snowflakes"); a webhook token is URL-safe text
# (68 characters of [A-Za-z0-9_-] in the URLs Discord hands out today, 100 hex digits in older ones).
DISCORD_WEBHOOK_PATH_PATTERN = re.compile(
    r"/api(?:/v(?P<version>[0-9]{1,2}))?/webhooks/(?P<webhook_id>[0-9]{15,20})/(?P<webhook_token>[A-Za-z0-9_-]{20,200})/?"
)
DISCORD_SNOWFLAKE_PATTERN = re.compile(r"[0-9]{15,20}")
DISCORD_THREAD_ID_QUERY_PARAMETER = "thread_id"
DISCORD_URL_REFUSAL = (
    "url: a Discord webhook URL, https://discord.com/api/webhooks/<id>/<token> (Copy Webhook URL), optionally with "
    "?thread_id=<thread id>"
)


class DiscordWebhookAddress(BaseModel):
    model_config = ConfigDict(frozen=True)

    discord_webhook_id: str
    discord_webhook_token: SecretStr
    discord_thread_id: str | None


def read_discord_thread_id(query: str) -> str | None:
    """The ``thread_id`` of a pasted URL's query, the only parameter accepted (once, a snowflake); None for no query.
    Raises InvalidInputError for anything else."""
    if not query:
        return None
    query_parameters = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    if (
        len(query_parameters) != 1
        or query_parameters[0][0] != DISCORD_THREAD_ID_QUERY_PARAMETER
        or not DISCORD_SNOWFLAKE_PATTERN.fullmatch(query_parameters[0][1])
    ):
        raise InvalidInputError(DISCORD_URL_REFUSAL)
    return query_parameters[0][1]


def parse_discord_webhook_url(discord_webhook_url: str) -> DiscordWebhookAddress:
    """The webhook's id, token and thread id (if any), or 400 ``invalid_input`` naming what is accepted. The message
    never repeats the URL: it carries the token."""
    if len(discord_webhook_url) > WEBHOOK_URL_MAXIMUM_LENGTH:
        raise InvalidInputError(DISCORD_URL_REFUSAL)
    try:
        url_parts = urlsplit(discord_webhook_url.strip())
        hostname = url_parts.hostname
        port = url_parts.port
        discord_thread_id = read_discord_thread_id(url_parts.query)
    except ValueError as parse_error:
        raise InvalidInputError(DISCORD_URL_REFUSAL) from parse_error
    path_match = DISCORD_WEBHOOK_PATH_PATTERN.fullmatch(url_parts.path)
    if (
        url_parts.scheme != "https"
        or hostname not in DISCORD_WEBHOOK_HOSTS
        or port not in {None, 443}
        or url_parts.username is not None
        or url_parts.password is not None
        or url_parts.fragment
        or path_match is None
    ):
        raise InvalidInputError(DISCORD_URL_REFUSAL)
    return DiscordWebhookAddress(
        discord_webhook_id=path_match.group("webhook_id"),
        discord_webhook_token=SecretStr(path_match.group("webhook_token")),
        discord_thread_id=discord_thread_id,
    )


def build_discord_execute_webhook_url(
    discord_webhook_id: str, *, discord_webhook_token: SecretStr, discord_thread_id: str | None, api_base_url: str
) -> str:
    """``{api_base_url}/v10/webhooks/{id}/{token}?wait=true`` (and ``&thread_id=`` when set): Discord answers once the message is saved (200 with
    the message, never read here) or with the error that kept it from being saved. With ``wait=false`` (its default)
    "a message that is not saved does not return an error", so a 204 would count as delivered a message nobody sees
    (https://github.com/discord/discord-api-docs/blob/main/developers/resources/webhook.mdx, Execute Webhook)."""
    query_parameters = {"wait": "true"}
    if discord_thread_id is not None:
        query_parameters[DISCORD_THREAD_ID_QUERY_PARAMETER] = discord_thread_id
    return (
        f"{api_base_url}/v{DISCORD_API_VERSION}/webhooks/{discord_webhook_id}/"
        f"{discord_webhook_token.get_secret_value()}?{urlencode(query_parameters)}"
    )
