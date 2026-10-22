# pay-7331 Python SDK

A small client for the [pay-7331 API](https://pay.7331.org/docs): invoices, quantity links, customer addresses,
events, the CSV export, and webhook verification. Blocking (`PayClient`) and asyncio (`AsyncPayClient`), both on
[httpx](https://www.python-httpx.org/), with typed [pydantic v2](https://docs.pydantic.dev/) responses. Python 3.10+.

It is not on PyPI. Install it from the repository:

```sh
pip install "pay-7331 @ git+https://github.com/7331/pay-7331.git#subdirectory=sdk/python"
# or, in a uv project
uv add "pay-7331 @ git+https://github.com/7331/pay-7331.git#subdirectory=sdk/python"
```

Pin a commit by putting `@<commit>` before the `#` (pip's
[VCS support](https://pip.pypa.io/en/stable/topics/vcs-support/); uv takes the same URL,
[Git dependencies](https://docs.astral.sh/uv/concepts/projects/dependencies/)).

## Quickstart

Make an API token in Settings (it asks for your passkey and shows the token once). Keep it in your secret store and
pass it in: the SDK reads no environment variable.

```python
from pay_7331 import PayClient

PAY_API_TOKEN = "pay_tok_..."

with PayClient(PAY_API_TOKEN) as pay:
    invoice = pay.create_invoice(amount="5", currency="USD", order_id="order-1042")
    print(invoice.url)  # send the customer here

    invoice = pay.read_invoice(invoice.id)
    if invoice.is_paid:  # "paid" or "overpaid": final
        ...  # fulfil order-1042
```

The same with asyncio:

```python
import asyncio

from pay_7331 import AsyncPayClient

PAY_API_TOKEN = "pay_tok_..."


async def create_checkout() -> str:
    async with AsyncPayClient(PAY_API_TOKEN) as pay:
        invoice = await pay.create_invoice(amount="5", currency="USD", order_id="order-1042")
        return invoice.url


print(asyncio.run(create_checkout()))
```

The sandbox (testnets, its own accounts and tokens) is `PayClient(token, base_url=pay_7331.SANDBOX_BASE_URL)`, i.e.
`https://pay-sandbox.7331.org`. `timeout` is 30 seconds by default (httpx's own default is 5 s,
[timeouts](https://www.python-httpx.org/advanced/timeouts/)); creating an invoice prices every offered network and
can take a few seconds.

## What it covers

Every route an API token may call, plus the public reads a server needs:

| Method | Route |
| --- | --- |
| `create_invoice`, `read_invoice`, `cancel_invoice`, `accept_partial_payment` | `POST /invoices`, `GET /invoices/{id}`, `POST /invoices/{id}/cancel`, `POST /invoices/{id}/accept-partial` |
| `list_invoices`, `iterate_invoices`, `find_invoice_by_order_id` | `GET /invoices` (`status`, `order_id_prefix`, `cursor`; `order_id`) |
| `export_invoices_csv`, `iterate_invoice_export_csv` | `GET /invoices/export.csv`, streamed |
| `create_payment_link`, `list_payment_links`, `read_payment_link`, `close_payment_link` | `/payment-links` |
| `create_static_deposit_address`, `read_static_deposit_address`, `list_static_deposit_addresses`, `iterate_static_deposit_addresses` | `/static-deposit-addresses` |
| `list_events`, `iterate_events` | `GET /events` |
| `list_chains`, `list_exchange_rates` | `GET /chains`, `GET /rates` |

`create_invoice` takes the API's fields as keyword arguments: `amount`, `currency`, `chain`, `chains`, `assets`,
`assets_by_chain`, `destination_address`, `allow_contract_destination`, `note`, `order_id`, `refresh_expired`,
`lifetime_seconds`, `accuracy_percent`, `underpaid_open_until`, `return_url`, `success_url`, `callback_url`,
`metadata`. Anything left out takes the API's default. The docs page explains each one.

Amounts are `str`, `Decimal` or `int` going out and `Decimal` coming back, built from the API's decimal strings
exactly; a float is refused. Times are timezone-aware `datetime`. Statuses are plain strings, so a status the API adds
later does not break this SDK.

`iterate_*` methods follow `next_cursor` page by page. The CSV export is streamed, never held in memory:

```python
from datetime import datetime, timezone

with PayClient(PAY_API_TOKEN) as pay, open("invoices.csv", "wb") as csv_file:
    pay.export_invoices_csv(csv_file, created_from=datetime(2026, 10, 1, tzinfo=timezone.utc))
```

## Errors

Every refusal raises a `PayApiError` subclass with the API's `code` and `message`, the HTTP `status_code`, and
`retry_after` (seconds) when the API sent `Retry-After`:

| Status | Class |
| --- | --- |
| 400 | `PayValidationError` |
| 401 | `PayAuthenticationError` (`api_token_invalid`: a malformed, unknown or revoked token) |
| 403 | `PayPermissionError` |
| 404 | `PayNotFoundError` (another account's id is 404 too) |
| 409 | `PayConflictError` (`invoice_state_conflict`, `webhook_not_set`, ...) |
| 429 | `PayRateLimitedError` (wait `retry_after`) |
| 5xx | `PayServerError` |

No answer at all (a timeout, a refused connection) is `PayTransportError`, its message naming the httpx error with
the token redacted. A 2xx answer this SDK cannot read (a proxy's page, a field of another type) is `PayResponseError`.
A token that is not `pay_tok_` and 43 to 128 base64url characters (a stray space or newline) is a `ValueError` from
the constructor, which never quotes it. All the others are `PayError`.

Nothing is retried for you. Reads are safe to repeat. A create is safe to repeat only when it names an `order_id`
(invoices) or a `customer_reference` (customer addresses): the API then answers the one already made. After a create
whose answer was lost, `find_invoice_by_order_id` tells you whether it was made.

## Webhooks

Set your webhook URL in Settings and keep its `whsec_...` secret like the token. `verify_webhook` checks a delivery
the [Standard Webhooks](https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md)
way, with the standard library only: an HMAC-SHA256 over `webhook-id.webhook-timestamp.body` keyed with the base64
after `whsec_`, any `v1,` entry of `webhook-signature` matching in constant time, and the timestamp within 5 minutes of
now. Pass the body bytes exactly as received. It returns a `WebhookEvent` (`webhook_id`, `type`, `timestamp`, `data`,
`is_test`, and `invoice`, `static_deposit_address` or `static_deposit_payment` parsed from `data`) or raises
`WebhookVerificationError`.

An event's `data` is the invoice or address as it was when the event was recorded. An event older than a field this
SDK's models need raises `PayEventDataError` from `invoice`, `static_deposit_address` or `static_deposit_payment`:
read the resource now instead (`read_invoice(event.invoice_id)`, or `read_static_deposit_address` with the address
id). The same goes for `list_events`, whose `AccountEvent` has the same three.

A plain [aiohttp](https://docs.aiohttp.org/en/stable/web_quickstart.html) server:

```python
from aiohttp import web

from pay_7331 import WebhookVerificationError, verify_webhook

PAY_WEBHOOK_SECRET = "whsec_..."
processed_webhook_ids: set[str] = set()  # use your database: the same id comes again on a retry


async def receive_pay_webhook(request: web.Request) -> web.Response:
    try:
        event = verify_webhook(await request.read(), headers=request.headers, secret=PAY_WEBHOOK_SECRET)
    except WebhookVerificationError:
        return web.Response(status=400)
    if event.is_test or event.webhook_id in processed_webhook_ids:
        return web.Response(status=204)
    processed_webhook_ids.add(event.webhook_id)
    invoice = event.invoice
    if event.type in {"invoice.paid", "invoice.overpaid"} and invoice is not None:
        ...  # queue fulfilment of invoice.order_id; reread it with read_invoice first
    return web.Response(status=204)


application = web.Application()
application.add_routes([web.post("/pay-events", receive_pay_webhook)])
web.run_app(application, port=8080)
```

`is_test` is true for a "Send test" delivery from Settings (its `webhook_id` starts with `test_` and its sample
invoice reads 404): never fulfil one. Delivery is at least once and may come out of order, so store `webhook_id` with
the work it starts, answer 2xx quickly, and reread the invoice before you fulfil.

## Development

```sh
cd sdk/python
uv sync
uv run ruff format --check . && uv run ruff check . && uv run ty check
```

There are no unit tests. `e2e/python_sdk_flow.py` runs the SDK against a real pay-7331 fork stack:

```sh
uv run --project backend --with-editable sdk/python python e2e/python_sdk_flow.py
```
