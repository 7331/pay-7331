"""Fork e2e of the Python SDK (sdk/python/), run by hand when the SDK or the API routes it calls change:

    uv run --project backend --with-editable sdk/python python e2e/python_sdk_flow.py [--scratch-folder DIR] [--keep-stack]

On its own fork stack (e2e/fork_stack.py; ``PAY_E2E_STACK_NAME`` and the port variables keep it apart from others) a
fresh account makes an API token and a webhook on /settings (merchant_operations_scenarios.py's helper), then the SDK
alone, ``PayClient`` and ``AsyncPayClient`` both:

- reads ``/chains`` and ``/rates`` typed (USDC at exactly 1);
- creates a 5 USD USDC invoice with ``order_id``, ``metadata``, ``return_url``, ``success_url`` and ``callback_url``,
  reads it, finds it by ``order_id`` (sync and async), and gets the same invoice back from a repeated create;
- pays it on the fork and waits (async) until it is paid and swept; ``verify_webhook`` accepts the real
  ``invoice.paid`` delivery at the webhook and at the callback, and refuses a tampered body and a wrong secret;
- exports the CSV (sync into a file object, async as chunks; the same bytes, the invoice's rows present);
- creates, lists, reads and closes a quantity link; creates a customer address twice (one address), reads it and
  lists it by prefix;
- lists the token's events (sync page, async iteration) with the invoice's ``invoice.paid`` parsed as an ``Invoice``;
- verifies a "Send test" delivery (``is_test``), and gets ``PayNotFoundError`` for an unknown invoice (both clients)
  and ``PayAuthenticationError`` for a bad token.

The log and results go to the scratch folder; the stack is removed at the end unless ``--keep-stack``.
"""

import argparse
import asyncio
import base64
from decimal import Decimal
import io
import json
from pathlib import Path
import secrets
import sys
import tempfile
import time

from account_passkey_session import account_request_headers
from account_settings_helpers import (
    WEBHOOK_RECEIVER_CONTAINER,
    WEBHOOK_RECEIVER_PATH,
    select_webhook_records,
    start_webhook_receiver_container,
    stop_webhook_receiver,
    wait_for_webhook_records,
)
from eth_account import Account
from fork_stack import (
    API_URL,
    FORK_CHAIN_KEY,
    PUBLIC_BASE_URL,
    SCENARIO_TIMEOUT_SECONDS,
    USDC_ADDRESS,
    USDC_BASE_UNITS,
    FlowContext,
    FlowFailedError,
    build_erc20_transfer_data,
    check,
    create_local_account,
    fund_with_usdc,
    run_scenarios_on_fork_stack,
    send_transaction,
    to_base_units,
    wait_for_receipt,
)
from merchant_operations_scenarios import sign_up_with_token_and_webhook
from pay_7331 import (
    AsyncPayClient,
    Invoice,
    PayAuthenticationError,
    PayClient,
    PayNotFoundError,
    WebhookEvent,
    WebhookVerificationError,
    verify_webhook,
)

SDK_FLOW_PAYER = create_local_account()
API_ORIGIN = API_URL.removesuffix("/api/v1")
# A well-formed invoice id no account has (``inv_`` and 22 base58 characters).
UNKNOWN_INVOICE_ID = "inv_" + "1" * 22
SDK_TIMEOUT_SECONDS = 60


def read_record_headers(record: dict[str, object]) -> dict[str, str]:
    record_headers = record["headers"]
    if not isinstance(record_headers, dict):
        raise FlowFailedError("a receiver record without headers")
    return {str(name): str(value) for name, value in record_headers.items()}


def verify_receiver_record(record: dict[str, object], *, webhook_secret: str) -> WebhookEvent:
    """The SDK's check of one delivery as the receiver got it: the body's exact bytes and its three headers."""
    return verify_webhook(str(record["body"]).encode(), headers=read_record_headers(record), secret=webhook_secret)


def check_webhook_refusals(record: dict[str, object], *, webhook_secret: str) -> None:
    """A changed body and another secret are both refused."""
    headers = read_record_headers(record)
    tampered_body = str(record["body"]).encode() + b" "
    other_secret = "whsec_" + base64.b64encode(secrets.token_bytes(32)).decode()
    for payload, secret in [(tampered_body, webhook_secret), (str(record["body"]).encode(), other_secret)]:
        try:
            verify_webhook(payload, headers=headers, secret=secret)
        except WebhookVerificationError:
            continue
        check(False, "verify_webhook accepted a tampered body or a wrong secret")


def check_paid_deliveries(
    context: FlowContext, *, webhook_secret: str, invoice_id: str, callback_path: str
) -> dict[str, object]:
    """``invoice.paid`` at the account webhook and at the invoice's callback, each verified by the SDK at once (the
    5-minute window), parsed as the invoice, not a test."""

    def this_invoice_paid(records: list[dict[str, object]]) -> list[dict[str, object]]:
        return [
            record
            for record in select_webhook_records(records, event_type="invoice.paid")
            if json.loads(str(record["body"]))["data"]["id"] == invoice_id
        ]

    records = wait_for_webhook_records(
        context,
        description="invoice.paid at the webhook and the callback",
        condition=lambda records: (
            {str(record["path"]) for record in this_invoice_paid(records)} >= {WEBHOOK_RECEIVER_PATH, callback_path}
        ),
    )
    paid_records = this_invoice_paid(records)
    for record in paid_records:
        event = verify_receiver_record(record, webhook_secret=webhook_secret)
        paid_invoice = event.invoice
        check(
            not event.is_test
            and not event.webhook_id.startswith("test_")
            and paid_invoice is not None
            and paid_invoice.id == invoice_id
            and paid_invoice.is_paid,
            f"real delivery parsed as {event.type} test={event.is_test} invoice={paid_invoice and paid_invoice.id}",
        )
    check_webhook_refusals(paid_records[0], webhook_secret=webhook_secret)
    return {"paid_deliveries_verified": sorted(str(record["path"]) for record in paid_records)}


def check_test_delivery(context: FlowContext, *, webhook_secret: str, session_headers: dict[str, str]) -> str:
    """ "Send test" for ``invoice.paid`` (a session route, not the SDK's): the SDK verifies it and marks it a test."""
    reply = context.http_client.post(
        f"{API_URL}/account-webhook/test-events", json={"event_type": "invoice.paid"}, headers=session_headers
    )
    check(reply.status_code == 202, f"test event: {reply.status_code} {reply.text[:200]}")

    def test_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
        return [record for record in records if json.loads(str(record["body"])).get("test") is True]

    records = wait_for_webhook_records(
        context, description="the test delivery", condition=lambda r: bool(test_records(r))
    )
    event = verify_receiver_record(test_records(records)[0], webhook_secret=webhook_secret)
    sample_invoice = event.invoice
    check(
        event.is_test and event.webhook_id.startswith("test_") and sample_invoice is not None,
        f"test delivery parsed as is_test={event.is_test} id={event.webhook_id}",
    )
    return str(sample_invoice.id if sample_invoice else "")


def pay_invoice_on_fork(context: FlowContext, *, invoice: Invoice) -> None:
    check(invoice.deposit_address is not None and invoice.amount_due is not None, "invoice has no deposit or amount")
    fund_with_usdc(context.http_client, SDK_FLOW_PAYER.address, base_units=10 * USDC_BASE_UNITS)
    base_units = to_base_units(str(invoice.amount_due), decimals=6)
    transfer_data = build_erc20_transfer_data(str(invoice.deposit_address), base_units)
    wait_for_receipt(
        context.http_client,
        send_transaction(context.http_client, sender=SDK_FLOW_PAYER.address, to=USDC_ADDRESS, data=transfer_data),
    )


async def wait_until_paid_and_swept(async_client: AsyncPayClient, *, invoice_id: str) -> Invoice:
    deadline = time.monotonic() + SCENARIO_TIMEOUT_SECONDS
    while True:
        invoice = await async_client.read_invoice(invoice_id)
        if invoice.is_paid and invoice.sweeps and invoice.sweeps[-1].status == "final":
            return invoice
        check(time.monotonic() < deadline, f"{invoice_id}: not paid and swept in {SCENARIO_TIMEOUT_SECONDS} s")
        await asyncio.sleep(1)


async def run_async_client_steps(
    *, api_token: str, invoice_id: str, order_id: str, order_id_prefix: str
) -> tuple[Invoice, bytes]:
    """Waits for the payment, then the async reads: order lookup, prefix iteration, CSV chunks, events, a 404."""
    async with AsyncPayClient(api_token, base_url=API_ORIGIN, timeout=SDK_TIMEOUT_SECONDS) as async_client:
        paid_invoice = await wait_until_paid_and_swept(async_client, invoice_id=invoice_id)
        found_invoice = await async_client.find_invoice_by_order_id(order_id)
        check(found_invoice is not None and found_invoice.id == invoice_id, "async order lookup missed the invoice")
        prefixed_ids = [invoice.id async for invoice in async_client.iterate_invoices(order_id_prefix=order_id_prefix)]
        check(prefixed_ids == [invoice_id], f"async prefix iteration gave {prefixed_ids}")
        csv_chunks = [chunk async for chunk in async_client.iterate_invoice_export_csv()]
        event_types_by_invoice: dict[str | None, set[str]] = {}
        async for event in async_client.iterate_events(page_size=10):
            event_types_by_invoice.setdefault(event.invoice_id, set()).add(event.type)
        check("invoice.paid" in event_types_by_invoice.get(invoice_id, set()), f"async events {event_types_by_invoice}")
        try:
            await async_client.read_invoice(UNKNOWN_INVOICE_ID)
        except PayNotFoundError as error:
            check(error.status_code == 404 and bool(error.code), f"async 404 as {error.code}")
        else:
            check(False, "async read of an unknown invoice did not raise PayNotFoundError")
    return paid_invoice, b"".join(csv_chunks)


def scenario_python_sdk(context: FlowContext) -> dict[str, object]:
    payout_address = Account.create().address
    start_webhook_receiver_container(context, receiver_environment={})
    try:
        api_token, webhook_secret, session_token = sign_up_with_token_and_webhook(payout_address=payout_address)
        session_headers = account_request_headers(session_token, public_base_url=PUBLIC_BASE_URL)
        order_id_prefix = f"sdk-{secrets.token_hex(4)}:"
        order_id = f"{order_id_prefix}order-1"
        callback_path = f"/hooks/callback?order={order_id}"
        with PayClient(api_token, base_url=API_ORIGIN, timeout=SDK_TIMEOUT_SECONDS) as client:
            chains = client.list_chains()
            check(any(chain.key == FORK_CHAIN_KEY and chain.assets for chain in chains), "list_chains: no fork chain")
            usdc_rates = [
                rate for rate in client.list_exchange_rates() if (rate.chain, rate.asset) == (FORK_CHAIN_KEY, "USDC")
            ]
            check([rate.usd for rate in usdc_rates] == [Decimal(1)], f"USDC rate {usdc_rates}")
            callback_url = f"http://{WEBHOOK_RECEIVER_CONTAINER}:8080{callback_path}"

            def create_order_invoice() -> Invoice:
                return client.create_invoice(
                    amount="5", currency="USD", chain=FORK_CHAIN_KEY, assets=["USDC"], order_id=order_id,
                    note="SDK e2e", metadata={"customer_id": 77}, return_url="https://shop.example/cart",
                    success_url="https://shop.example/thanks", callback_url=callback_url,
                )  # fmt: skip

            invoice = create_order_invoice()
            check(
                invoice.status == "awaiting_payment"
                and invoice.amount_due is not None
                and isinstance(invoice.amount_due, Decimal)
                and invoice.metadata == {"customer_id": 77}
                and invoice.callback_url == callback_url,
                f"created invoice {invoice.status} {invoice.amount_due!r} {invoice.metadata}",
            )
            check(client.read_invoice(invoice.id) == invoice, "read_invoice differs from the created invoice")
            found_invoice = client.find_invoice_by_order_id(order_id)
            check(found_invoice is not None and found_invoice.id == invoice.id, "order lookup missed the invoice")
            check(client.find_invoice_by_order_id(f"{order_id_prefix}none") is None, "unknown order id found something")
            repeated_invoice = create_order_invoice()
            check(repeated_invoice.id == invoice.id, "a repeated order_id made a second invoice")

            pay_invoice_on_fork(context, invoice=invoice)
            paid_invoice, async_csv_bytes = asyncio.run(
                run_async_client_steps(
                    api_token=api_token, invoice_id=invoice.id, order_id=order_id, order_id_prefix=order_id_prefix
                )
            )
            check(paid_invoice.amount_received == invoice.amount_due, f"received {paid_invoice.amount_received}")
            delivery_summary = check_paid_deliveries(
                context, webhook_secret=webhook_secret, invoice_id=invoice.id, callback_path=callback_path
            )

            csv_file = io.BytesIO()
            written_byte_count = client.export_invoices_csv(csv_file)
            csv_bytes = csv_file.getvalue()
            check(written_byte_count == len(csv_bytes) > 0, "export_invoices_csv wrote nothing")
            check(csv_bytes == async_csv_bytes, "the async CSV differs from the sync CSV")
            csv_lines = csv_bytes.decode().splitlines()
            check(csv_lines[0].startswith("row_kind,occurred_at,invoice_id"), f"CSV header {csv_lines[0][:80]}")
            invoice_row_kinds = sorted(line.split(",")[0] for line in csv_lines[1:] if f",{invoice.id}," in line)
            check(invoice_row_kinds == ["invoice", "payment", "sweep"], f"CSV rows of the invoice {invoice_row_kinds}")

            payment_link = client.create_payment_link(
                amount="2", currency="USD", quantity=2, chain=FORK_CHAIN_KEY, assets=["USDC"], note="SDK e2e link"
            )
            check(payment_link.status == "open" and payment_link.slots_left == 2, f"link {payment_link.status}")
            check(payment_link.id in {link.id for link in client.list_payment_links()}, "list_payment_links missed it")
            read_link = client.read_payment_link(payment_link.id)
            check(read_link.invoices == [] and read_link.quantity == 2, "read_payment_link")
            closed_link = client.close_payment_link(payment_link.id)
            check(closed_link.status == "closed" and closed_link.closed_at is not None, f"closed {closed_link.status}")

            customer_reference = f"{order_id_prefix}customer-77"
            static_address = client.create_static_deposit_address(
                customer_reference=customer_reference, chain=FORK_CHAIN_KEY, assets=["USDC"]
            )
            repeated_address = client.create_static_deposit_address(
                customer_reference=customer_reference, chain=FORK_CHAIN_KEY, assets=["USDC"]
            )
            check(repeated_address.id == static_address.id, "a repeated customer_reference made a second address")
            read_address = client.read_static_deposit_address(static_address.id)
            check(read_address.deposit_address == static_address.deposit_address and read_address.payments == [],
                  "read_static_deposit_address")  # fmt: skip
            listed_ids = [
                address.id
                for address in client.iterate_static_deposit_addresses(customer_reference_prefix=order_id_prefix)
            ]
            check(listed_ids == [static_address.id], f"static addresses by prefix {listed_ids}")

            event_page = client.list_events(limit=50)
            paid_events = [
                event for event in event_page.events if (event.invoice_id, event.type) == (invoice.id, "invoice.paid")
            ]
            paid_event_invoice = paid_events[0].invoice if paid_events else None
            check(paid_event_invoice is not None and paid_event_invoice.is_paid, "list_events: no parsed invoice.paid")

            sample_invoice_id = check_test_delivery(
                context, webhook_secret=webhook_secret, session_headers=session_headers
            )
            for unknown_invoice_id in (UNKNOWN_INVOICE_ID, sample_invoice_id):
                try:
                    client.read_invoice(unknown_invoice_id)
                except PayNotFoundError as error:
                    check(error.status_code == 404 and bool(error.code) and bool(error.message), f"404 as {error}")
                else:
                    check(False, f"reading {unknown_invoice_id} did not raise PayNotFoundError")
        with PayClient("pay_tok_" + secrets.token_urlsafe(32), base_url=API_ORIGIN) as unknown_token_client:
            try:
                unknown_token_client.read_invoice(invoice.id)
            except PayAuthenticationError as error:
                check(error.status_code == 401 and error.code == "api_token_invalid", f"bad token as {error.code}")
                bad_token_code = error.code
            else:
                check(False, "an unknown token read an invoice")
                bad_token_code = ""
        return {
            "invoice_id": invoice.id,
            "csv_bytes": len(csv_bytes),
            "payment_link_id": payment_link.id,
            "static_deposit_address_id": static_address.id,
            "event_count_first_page": len(event_page.events),
            "test_delivery_invoice_id": sample_invoice_id,
            "bad_token_code": bad_token_code,
            **delivery_summary,
        }
    finally:
        stop_webhook_receiver()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--scratch-folder", type=Path, default=Path(tempfile.gettempdir()) / "pay-7331-e2e" / "python_sdk_flow"
    )
    parser.add_argument("--keep-stack", action="store_true", help="leave the stack running for a look")
    options = parser.parse_args()
    return run_scenarios_on_fork_stack(
        [("scenario_python_sdk", scenario_python_sdk)],
        scratch_folder=options.scratch_folder.resolve(),
        keep_stack=options.keep_stack,
    )


if __name__ == "__main__":
    sys.exit(main())
