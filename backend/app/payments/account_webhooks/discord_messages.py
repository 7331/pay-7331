"""The Discord message an invoice event or an operator alert becomes (2026-10-13; PLAN.md section 8.4).

One line in /activity's words (frontend ``invoice_descriptions.ts``, the same titles as there):
``**Paid**: 5.00 USD (5.00 USDC) · Logo design · https://pay.7331.org/i/inv_...``. Only what the account already sees
on /activity: no payer address, no IP, no metadata or order id. The note is the maker's own text but may come from
an API token's server, so Discord markdown in it is escaped and ``allowed_mentions`` parses nothing: an ``@everyone``,
role or user mention in a note never pings anyone (message.mdx, "Allowed Mentions Object": a webhook parses user
mentions by default, ``{"parse": []}`` suppresses all). ``SUPPRESS_EMBEDS`` (``1 << 2``, message.mdx "Message
Flags") keeps the invoice link from unfurling into a preview card. ``content`` is at most 2000 characters
(webhook.mdx, "Execute Webhook"); https://github.com/discord/discord-api-docs/tree/main/developers/resources.

An ``operator.alert`` (only ever on the operator's account) is one line too: ``**Needs the operator** (arbitrum,
treasury low): <message> · Reminder every 1 h while it lasts (Settings, Discord) · https://pay.7331.org/activity``,
the chain (``host`` for the host's backup alerts, which have none), the kind in words and the alert's own sentence,
which says what is wrong and what to do, with the Safe's or key's address to send to; a reminder starts
``**Still needs the operator**`` and says since when, as a Discord timestamp (``<t:unix:f>``, shown in the reader's
own time zone; reference.mdx, "Message Formatting"). All three are our own text, so only ``*``, ``~``, ``|`` and ``\\``
are escaped: a quoted shell command shows as code, and a copied alert carries no backslashes (2026-10-18). The owner
could not tell what an alert was about (2026-10-18), hence the reminder line and the link.

Every message is sent as ``pay-7331`` (``username``, "override the default username of the webhook", e.g. Discord's
default "Spidey Bot"; webhook.mdx "Execute Webhook", which documents no limits for it; a webhook's own name follows the
nickname rules of user.mdx "Usernames and Nicknames", up to 80 characters, never "clyde" or "discord"; webhook.mdx
"Create Webhook").
"""

from datetime import datetime
import json
import re

from pydantic import JsonValue

from app.payments.account_events.schemas import AccountEventResponse

DISCORD_CONTENT_MAXIMUM_LENGTH = 2000
NOTE_MAXIMUM_LENGTH = 300
OPERATOR_ALERT_MESSAGE_MAXIMUM_LENGTH = 1500
OPERATOR_ALERT_TITLE = "Needs the operator"
OPERATOR_ALERT_REMINDER_TITLE = "Still needs the operator"
DISCORD_MESSAGE_USERNAME = "pay-7331"
# What an alert about no chain (the host's backup units, ``record-operator-alert``) names instead.
OPERATOR_ALERT_HOST_CHAIN_TEXT = "host"
SUPPRESS_EMBEDS_MESSAGE_FLAG = 1 << 2
DISCORD_TEST_MESSAGE_EVENT_TYPE = "discord.test"
# Discord markdown: emphasis, code, strike, spoiler, quote, heading, list, masked link and escape characters.
DISCORD_MARKDOWN_CHARACTER_PATTERN = re.compile(r"([\\*_~`|>#\-\[\]()<:@])")
# An operator alert is our own sentence, not a maker's (ip-api's quoted words lose their brackets and backticks first,
# ip_api_key_alert_service.py): only bold, strike, spoiler and the escape itself are escaped, so its `commands` show
# as code and a copied alert has no backslashes in its words. An inner ``_`` is safe while the
# next ``_`` is followed by a letter or digit (``inv_...``, ``PAY_IP_API_KEY``); a name like ``PAY_X_<CHAIN>`` goes in
# backticks. Mentions are off (``allowed_mentions``).
DISCORD_OPERATOR_ALERT_CHARACTER_PATTERN = re.compile(r"([\\*~|])")
# The titles of frontend invoice_descriptions.ts accountEventTitles, for the events a webhook sends.
INVOICE_EVENT_TITLES = {
    "invoice.payment_detected": "Payment seen",
    # XMR only (PLAN.md section 14).
    "invoice.seen": "Payment seen, waiting for a block",
    "invoice.payment_held": "Payment held up: the nodes disagree. The operator checks it.",
    "invoice.underpaid": "Underpaid",
    "invoice.paid": "Paid",
    "invoice.overpaid": "Overpaid",
    "invoice.expired": "Expired",
    "invoice.cancelled": "Cancelled",
    "invoice.swept": "Sent to your wallet",
    "invoice.sweep_failed": "Sending to your wallet failed",
    "invoice.late_payment": "Late payment",
    "invoice.stray_payment": "Wrong coin received",
    "invoice.refunded": "Refunded",
    "refund.waiting_for_float": "Refund waiting for float",
    "refund.needs_owner": "Refund needs the operator",
    "schedule.invoice_created": "Scheduled invoice made",
}
PAID_INVOICE_STATUSES = {"paid", "overpaid"}


def read_data_text(data: JsonValue, *path: str) -> str:
    """The string at that path of an event's data, else ""."""
    value = data
    for key in path:
        if not isinstance(value, dict):
            return ""
        value = value.get(key)
    return value if isinstance(value, str) else ""


def escape_discord_markdown(text: str) -> str:
    """Plain text as Discord shows it: each markdown character backslash-escaped, line breaks as spaces."""
    return DISCORD_MARKDOWN_CHARACTER_PATTERN.sub(r"\\\1", " ".join(text.split()))


def escape_discord_operator_alert_text(text: str) -> str:
    return DISCORD_OPERATOR_ALERT_CHARACTER_PATTERN.sub(r"\\\1", " ".join(text.split()))


def format_decimal_for_display(decimal_text: str) -> str:
    """At most 6 decimals, at least 2 (frontend formatDecimalForDisplay's rule), cut, never rounded up."""
    whole_part, _, fraction_part = decimal_text.partition(".")
    fraction_part = fraction_part[:6].rstrip("0").ljust(2, "0")
    return f"{whole_part}.{fraction_part}"


def describe_invoice_amount(invoice_data: JsonValue) -> str:
    """``5.00 USD (5.00 USDC)``, ``30.00 POL`` for a crypto invoice, ``5.00 USD`` before a coin is picked."""
    asset_symbol = read_data_text(invoice_data, "asset", "symbol")
    amount_due = read_data_text(invoice_data, "amount_due")
    crypto_amount = f"{format_decimal_for_display(amount_due)} {asset_symbol}" if amount_due and asset_symbol else ""
    fiat_amount = read_data_text(invoice_data, "fiat", "amount")
    fiat_currency = read_data_text(invoice_data, "fiat", "currency")
    if fiat_amount and fiat_currency:
        fiat_text = f"{fiat_amount} {fiat_currency}"
        return f"{fiat_text} ({crypto_amount})" if crypto_amount else fiat_text
    return crypto_amount or "Invoice"


def describe_received_amount(invoice_data: JsonValue) -> str:
    """``2.00 of 5.00 USDC received`` while money is arriving or short; "" once paid or before any arrived."""
    if read_data_text(invoice_data, "status") in PAID_INVOICE_STATUSES:
        return ""
    amount_received = read_data_text(invoice_data, "amount_received")
    amount_due = read_data_text(invoice_data, "amount_due")
    asset_symbol = read_data_text(invoice_data, "asset", "symbol")
    if not (amount_received and amount_due and asset_symbol) or not amount_received.strip("0."):
        return ""
    return (
        f"{format_decimal_for_display(amount_received)} of {format_decimal_for_display(amount_due)} "
        f"{asset_symbol} received"
    )


def describe_stray_payments(invoice_data: JsonValue) -> str:
    """``48.2346 POL`` for each coin the invoice did not ask for, no sender."""
    stray_payments = invoice_data.get("stray_payments") if isinstance(invoice_data, dict) else None
    if not isinstance(stray_payments, list):
        return ""
    return ", ".join(
        f"{format_decimal_for_display(read_data_text(stray_payment, 'amount'))} "
        f"{read_data_text(stray_payment, 'asset_symbol')}"
        for stray_payment in stray_payments
        if read_data_text(stray_payment, "amount") and read_data_text(stray_payment, "asset_symbol")
    )


def build_discord_invoice_event_content(account_event: AccountEventResponse) -> str:
    title = INVOICE_EVENT_TITLES.get(account_event.type, account_event.type)
    line_parts = [f"**{title}**: {describe_invoice_amount(account_event.data)}"]
    if account_event.type == "invoice.stray_payment":
        line_parts.append(describe_stray_payments(account_event.data))
    line_parts.append(describe_received_amount(account_event.data))
    note = read_data_text(account_event.data, "note")
    if len(note) > NOTE_MAXIMUM_LENGTH:
        note = note[: NOTE_MAXIMUM_LENGTH - 1] + "…"
    line_parts.append(escape_discord_markdown(note))
    line_parts.append(read_data_text(account_event.data, "url"))
    return " · ".join(line_part for line_part in line_parts if line_part)[:DISCORD_CONTENT_MAXIMUM_LENGTH]


def describe_operator_alert_reminders(repeat_hours: int) -> str:
    if repeat_hours == 0:
        return "No reminders (Settings, Discord)"
    return f"Reminder every {repeat_hours} h while it lasts (Settings, Discord)"


def describe_operator_alert_open_since(first_seen_at_text: str) -> str:
    """``Open since <t:1760800000:f> (<t:1760800000:R>)``: Discord shows the reader's own time and how long ago."""
    try:
        unix_seconds = int(datetime.fromisoformat(first_seen_at_text).timestamp())
    except ValueError:
        return ""
    return f"Open since <t:{unix_seconds}:f> (<t:{unix_seconds}:R>)"


def build_discord_operator_alert_content(
    account_event: AccountEventResponse, *, repeat_hours: int, activity_url: str
) -> str:
    chain_text = read_data_text(account_event.data, "chain_key") or OPERATOR_ALERT_HOST_CHAIN_TEXT
    alert_kind_text = read_data_text(account_event.data, "kind").replace("_", " ")
    context_text = ", ".join(escape_discord_operator_alert_text(part) for part in (chain_text, alert_kind_text) if part)
    # Cut once escaped, so the reminder line, the link and a timestamp tag after it always fit in the 2000.
    message_text = escape_discord_operator_alert_text(read_data_text(account_event.data, "message"))
    if len(message_text) > OPERATOR_ALERT_MESSAGE_MAXIMUM_LENGTH:
        message_text = message_text[: OPERATOR_ALERT_MESSAGE_MAXIMUM_LENGTH - 1]
        # A cut between a backslash and the character it escapes would escape the ellipsis instead.
        if (len(message_text) - len(message_text.rstrip("\\"))) % 2:
            message_text = message_text[:-1]
        message_text += "…"
    is_reminder = isinstance(account_event.data, dict) and account_event.data.get("is_reminder") is True
    title = OPERATOR_ALERT_REMINDER_TITLE if is_reminder else OPERATOR_ALERT_TITLE
    line_parts = [f"**{title}** ({context_text}): {message_text}"]
    first_seen_at_text = read_data_text(account_event.data, "first_seen_at")
    if is_reminder and first_seen_at_text:
        line_parts.append(describe_operator_alert_open_since(first_seen_at_text))
    line_parts.append(describe_operator_alert_reminders(repeat_hours))
    line_parts.append(activity_url)
    return " · ".join(line_part for line_part in line_parts if line_part)[:DISCORD_CONTENT_MAXIMUM_LENGTH]


def build_discord_message_body(content: str) -> str:
    """The Execute Webhook JSON body: the content, sent as pay-7331, no mentions parsed, no link previews."""
    return json.dumps(
        {
            "content": content[:DISCORD_CONTENT_MAXIMUM_LENGTH],
            "username": DISCORD_MESSAGE_USERNAME,
            "allowed_mentions": {"parse": []},
            "flags": SUPPRESS_EMBEDS_MESSAGE_FLAG,
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )


def build_discord_test_message_body() -> str:
    return build_discord_message_body(
        "**Test message** from pay-7331: news about this account's invoices shows up here."
    )
