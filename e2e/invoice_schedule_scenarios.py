"""Fork e2e scenario for invoice schedules (2026-10-21; PLAN.md sections 12 and 17), run by ``polygon_fork_flow.py``
after its own and alone with ``--scenario scenario_invoice_schedules``:

- A fresh account makes an API token and sets a webhook on /settings; its token makes a daily USDC schedule that
  starts tomorrow (no invoice yet), a second one with the same order id prefix is refused, and one with two ends too.
- Time travel: the schedule's start is moved a day back in SQL, so its first period is due; the watcher makes exactly
  one invoice, its order id ``<prefix>-<YYYYMMDD>`` of the period's start, with the schedule's note and metadata, and
  ``schedule.invoice_created`` arrives at the webhook, signed, carrying the invoice and its URL.
- The schedule rewound by hand (its next period and attempt set back) is billed by nobody twice: the next passes see
  the period's invoice and move on, still one invoice and one event.
- The invoice is paid on the fork until paid and swept. The next period, moved due, makes the second invoice.
- Pause: a period moved due while paused makes nothing; resume bills from the next period still to come, not the one
  missed; cancel ends it, and a resume after is 409.
- A schedule with ``ends_after_invoice_count`` 1 makes its invoice at once and completes.
- The docs' example responses for the schedule routes are captured, anonymised and compared with the committed file
  (or written with ``PAY_E2E_WRITE_DOCS_RESPONSES=1``), as merchant_operations_scenarios.py does for its keys.
"""

from datetime import UTC, datetime, timedelta
import json
import re
import secrets
import time

from account_passkey_session import account_request_headers
from account_settings_helpers import (
    read_webhook_record_type,
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
    build_erc20_transfer_data,
    check,
    create_local_account,
    fund_with_usdc,
    read_account_event_types,
    run_sql,
    send_transaction,
    to_base_units,
    wait_for_receipt,
)
from merchant_operations_scenarios import sign_up_with_token_and_webhook, write_docs_responses
from standardwebhooks import Webhook

INVOICE_SCHEDULE_PAYER = create_local_account()
INVOICE_SCHEDULE_ID_PATTERN = re.compile(r"sch_[1-9A-HJ-NP-Za-km-z]{22}")
# The watcher's schedule pass runs every 15 s (backend/app/payments/invoice_schedules/runner_service.py): two passes.
TWO_SCHEDULE_PASSES_SECONDS = 35
SCHEDULED_INVOICE_CREATED = "schedule.invoice_created"
DOCS_ORDER_ID_PREFIX = "customer-77-premium"


def move_schedule_back(context: FlowContext, invoice_schedule_id: str, *, order_id_prefix: str, days: int) -> None:
    """Time travel: everything about the schedule happened ``days`` earlier, so its next period is due now: its start
    and next attempt, and its invoices' periods and the order ids named after them (``<prefix>-<YYYYMMDD>``)."""
    check(
        INVOICE_SCHEDULE_ID_PATTERN.fullmatch(invoice_schedule_id) is not None,
        f"not a schedule id: {invoice_schedule_id}",
    )
    check(re.fullmatch(r"[a-z0-9-]+", order_id_prefix) is not None, f"not a test prefix: {order_id_prefix}")
    shift = f"interval '{days} day'"
    # One psql command string is one transaction (https://www.postgresql.org/docs/current/app-psql.html, -c): a
    # watcher pass between the two updates would read the billed invoice as the next period's and skip that period.
    # The schedule row goes first: its lock makes the watcher's SKIP LOCKED pass leave it alone until both are in.
    # The invoices move aside first (a thousand years back, the order id suffixed): the unique (account, order id) and
    # (schedule, period) indexes are checked row by row, so a day still held by the invoice before would collide.
    aside = "interval '1000 years'"
    run_sql(context, f"update invoice_schedules set starts_at = starts_at - {shift}, "  # noqa: S608
                     f"next_attempt_at = next_attempt_at - {shift} where id = '{invoice_schedule_id}'; "
                     f"update invoices set order_id = order_id || '-moving', "
                     f"invoice_schedule_period_start = invoice_schedule_period_start - {aside} "
                     f"where invoice_schedule_id = '{invoice_schedule_id}'; "
                     f"update invoices set invoice_schedule_period_start = "
                     f"invoice_schedule_period_start + {aside} - {shift}, "
                     f"order_id = '{order_id_prefix}-' || to_char((invoice_schedule_period_start + {aside} - {shift}) "
                     f"at time zone 'UTC', 'YYYYMMDD') where invoice_schedule_id = '{invoice_schedule_id}'")  # fmt: skip


def read_schedule(context: FlowContext, invoice_schedule_id: str, *, headers: dict[str, str]) -> dict[str, object]:
    reply = context.http_client.get(f"{API_URL}/invoice-schedules/{invoice_schedule_id}", headers=headers)
    check(reply.status_code == 200, f"read schedule: {reply.status_code} {reply.text[:200]}")
    return reply.json()


def list_schedule_invoices(
    context: FlowContext, invoice_schedule_id: str, *, headers: dict[str, str]
) -> list[dict[str, object]]:
    reply = context.http_client.get(f"{API_URL}/invoice-schedules/{invoice_schedule_id}/invoices", headers=headers)
    check(reply.status_code == 200, f"schedule invoices: {reply.status_code} {reply.text[:200]}")
    return reply.json()["invoices"]


def wait_for_schedule_invoices(
    context: FlowContext, invoice_schedule_id: str, *, headers: dict[str, str], count: int
) -> list[dict[str, object]]:
    deadline = time.monotonic() + SCENARIO_TIMEOUT_SECONDS
    invoices = list_schedule_invoices(context, invoice_schedule_id, headers=headers)
    while len(invoices) < count:
        check(time.monotonic() < deadline,
              f"{invoice_schedule_id}: {len(invoices)} invoices, not {count}, in {SCENARIO_TIMEOUT_SECONDS} s; "
              f"schedule {read_schedule(context, invoice_schedule_id, headers=headers)}")  # fmt: skip
        time.sleep(1)
        invoices = list_schedule_invoices(context, invoice_schedule_id, headers=headers)
    check(len(invoices) == count, f"{invoice_schedule_id}: {len(invoices)} invoices, expected exactly {count}")
    return invoices


def post_schedule_action(
    context: FlowContext, invoice_schedule_id: str, *, action: str, headers: dict[str, str], expected_status: int = 200
) -> dict[str, object]:
    reply = context.http_client.post(f"{API_URL}/invoice-schedules/{invoice_schedule_id}/{action}", headers=headers)
    check(reply.status_code == expected_status, f"{action}: {reply.status_code} {reply.text[:200]}")
    return reply.json()


def check_scheduled_invoice(
    invoice: dict[str, object], *, invoice_schedule: dict[str, object], order_id_prefix: str
) -> None:
    period_start = datetime.fromisoformat(str(invoice["invoice_schedule_period_start"]))
    expected_order_id = f"{order_id_prefix}-{period_start.astimezone(UTC):%Y%m%d}"
    check(invoice["invoice_schedule_id"] == invoice_schedule["id"] and invoice["order_id"] == expected_order_id
          and invoice["note"] == invoice_schedule["note"] and invoice["metadata"] == invoice_schedule["metadata"]
          and invoice["status"] == "awaiting_payment" and invoice["asset"]["symbol"] == "USDC"  # type: ignore[index]
          and str(invoice["url"]).endswith(f"/i/{invoice['id']}"),
          f"scheduled invoice {invoice['id']}: {invoice['order_id']} {invoice['status']}")  # fmt: skip


def pay_scheduled_invoice(context: FlowContext, invoice: dict[str, object], *, headers: dict[str, str]) -> None:
    """Sends the invoice's USDC to its deposit and waits until it is paid and its sweep final."""
    wait_for_receipt(context.http_client, send_transaction(
        context.http_client, sender=INVOICE_SCHEDULE_PAYER.address, to=USDC_ADDRESS,
        data=build_erc20_transfer_data(str(invoice["deposit_address"]),
                                       to_base_units(str(invoice["amount_due"]), decimals=6))))  # fmt: skip
    deadline = time.monotonic() + SCENARIO_TIMEOUT_SECONDS
    while True:
        current = context.http_client.get(f"{API_URL}/invoices/{invoice['id']}", headers=headers).json()
        sweeps = current.get("sweeps") or []
        if current["status"] == "paid" and sweeps and sweeps[-1]["status"] == "final":
            return
        check(time.monotonic() < deadline, f"{invoice['id']}: not paid and swept, {current['status']}")
        time.sleep(1)


def check_schedule_refusals(context: FlowContext, *, headers: dict[str, str], order_id_prefix: str) -> None:
    taken_reply = context.http_client.post(
        f"{API_URL}/invoice-schedules",
        json={"amount": "5", "currency": "USD", "chain": FORK_CHAIN_KEY, "assets": ["USDC"], "interval": "weekly",
              "order_id_prefix": order_id_prefix},
        headers=headers,
    )  # fmt: skip
    check(taken_reply.status_code == 409 and taken_reply.json()["code"] == "order_id_prefix_taken",
          f"repeated prefix: {taken_reply.status_code} {taken_reply.text[:200]}")  # fmt: skip
    two_ends_reply = context.http_client.post(
        f"{API_URL}/invoice-schedules",
        json={"amount": "5", "currency": "USD", "interval": "weekly", "ends_after_invoice_count": 3,
              "ends_at": (datetime.now(UTC) + timedelta(days=30)).isoformat()},
        headers=headers,
    )  # fmt: skip
    check(two_ends_reply.status_code == 400, f"two ends: {two_ends_reply.status_code} {two_ends_reply.text[:200]}")
    unknown_reply = context.http_client.get(f"{API_URL}/invoice-schedules/sch_{'1' * 22}", headers=headers)
    check(unknown_reply.status_code == 404 and unknown_reply.json()["code"] == "invoice_schedule_not_found",
          f"unknown schedule: {unknown_reply.status_code}")  # fmt: skip


def check_schedule_invoice_created_webhook(
    context: FlowContext, *, webhook_secret: str, invoice: dict[str, object]
) -> dict[str, object]:
    records = wait_for_webhook_records(
        context, description=f"{SCHEDULED_INVOICE_CREATED} for {invoice['id']}",
        condition=lambda records: any(read_webhook_record_type(record) == SCHEDULED_INVOICE_CREATED
                                      and json.loads(str(record["body"]))["data"]["id"] == invoice["id"]
                                      for record in records),
    )  # fmt: skip
    webhook_verifier = Webhook(webhook_secret)
    matching = [record for record in records if read_webhook_record_type(record) == SCHEDULED_INVOICE_CREATED
                and json.loads(str(record["body"]))["data"]["id"] == invoice["id"]]  # fmt: skip
    check(len(matching) == 1, f"{len(matching)} {SCHEDULED_INVOICE_CREATED} deliveries for {invoice['id']}")
    webhook_verifier.verify(str(matching[0]["body"]), dict(matching[0]["headers"]))  # type: ignore[call-overload]
    body = json.loads(str(matching[0]["body"]))
    check("test" not in body and body["data"]["url"] == invoice["url"]
          and body["data"]["invoice_schedule_id"] == invoice["invoice_schedule_id"],
          f"{SCHEDULED_INVOICE_CREATED} body {str(body)[:300]}")  # fmt: skip
    return body


def run_daily_schedule(
    context: FlowContext, *, headers: dict[str, str], webhook_secret: str
) -> tuple[dict[str, object], dict[str, object]]:
    """The daily schedule's life up to its cancel; returns its docs captures and a summary."""
    order_id_prefix = f"plan-{secrets.token_hex(4)}"
    starts_at = datetime.now(UTC) + timedelta(days=1)
    create_reply = context.http_client.post(
        f"{API_URL}/invoice-schedules",
        json={"amount": "5", "currency": "USD", "chain": FORK_CHAIN_KEY, "assets": ["USDC"], "interval": "daily",
              "starts_at": starts_at.isoformat(), "order_id_prefix": order_id_prefix, "note": "Premium plan, daily",
              "metadata": {"customer_id": 77}},
        headers=headers,
    )  # fmt: skip
    check(create_reply.status_code == 201, f"create schedule: {create_reply.status_code} {create_reply.text[:300]}")
    invoice_schedule = create_reply.json()
    schedule_id = str(invoice_schedule["id"])
    check(invoice_schedule["status"] == "active" and invoice_schedule["created_invoice_count"] == 0
          and datetime.fromisoformat(str(invoice_schedule["next_period_start"])) == starts_at,
          f"new schedule {invoice_schedule}")  # fmt: skip
    check_schedule_refusals(context, headers=headers, order_id_prefix=order_id_prefix)
    check(list_schedule_invoices(context, schedule_id, headers=headers) == [], "an invoice before the start")

    move_schedule_back(context, schedule_id, order_id_prefix=order_id_prefix, days=1)
    first_invoice = wait_for_schedule_invoices(context, schedule_id, headers=headers, count=1)[0]
    check_scheduled_invoice(first_invoice, invoice_schedule=invoice_schedule, order_id_prefix=order_id_prefix)
    webhook_body = check_schedule_invoice_created_webhook(context, webhook_secret=webhook_secret, invoice=first_invoice)

    # Rewound by hand: its next period back to the billed one, its attempt due now. The passes find the period's
    # invoice and move on.
    run_sql(context, "update invoice_schedules set next_period_index = 0, next_attempt_at = now() - interval "  # noqa: S608
                     f"'1 second' where id = '{schedule_id}'")  # fmt: skip
    time.sleep(TWO_SCHEDULE_PASSES_SECONDS)
    wait_for_schedule_invoices(context, schedule_id, headers=headers, count=1)
    rewound = read_schedule(context, schedule_id, headers=headers)
    check(rewound["created_invoice_count"] == 1
          and datetime.fromisoformat(str(rewound["next_period_start"])) > datetime.now(UTC),
          f"rewound schedule {rewound}")  # fmt: skip
    event_types = read_account_event_types(context, str(first_invoice["id"]))
    check(event_types.count(SCHEDULED_INVOICE_CREATED) == 1, f"events of the first invoice: {event_types}")

    fund_with_usdc(context.http_client, INVOICE_SCHEDULE_PAYER.address, base_units=20 * USDC_BASE_UNITS)
    pay_scheduled_invoice(context, first_invoice, headers=headers)

    move_schedule_back(context, schedule_id, order_id_prefix=order_id_prefix, days=1)
    first_invoice = context.http_client.get(f"{API_URL}/invoices/{first_invoice['id']}", headers=headers).json()
    invoices = wait_for_schedule_invoices(context, schedule_id, headers=headers, count=2)
    second_invoice = invoices[0]
    check_scheduled_invoice(second_invoice, invoice_schedule=invoice_schedule, order_id_prefix=order_id_prefix)
    check(second_invoice["order_id"] != first_invoice["order_id"] and invoices[1]["id"] == first_invoice["id"],
          f"invoices {[invoice['order_id'] for invoice in invoices]}")  # fmt: skip

    paused = post_schedule_action(context, schedule_id, action="pause", headers=headers)
    check(paused["status"] == "paused" and paused["next_period_start"] is None, f"paused {paused}")
    post_schedule_action(context, schedule_id, action="pause", headers=headers)
    move_schedule_back(context, schedule_id, order_id_prefix=order_id_prefix, days=1)
    time.sleep(TWO_SCHEDULE_PASSES_SECONDS)
    wait_for_schedule_invoices(context, schedule_id, headers=headers, count=2)
    resumed = post_schedule_action(context, schedule_id, action="resume", headers=headers)
    check(resumed["status"] == "active"
          and datetime.fromisoformat(str(resumed["next_period_start"])) > datetime.now(UTC),
          f"resumed {resumed}")  # fmt: skip
    time.sleep(TWO_SCHEDULE_PASSES_SECONDS)
    wait_for_schedule_invoices(context, schedule_id, headers=headers, count=2)
    cancelled = post_schedule_action(context, schedule_id, action="cancel", headers=headers)
    check(cancelled["status"] == "cancelled" and cancelled["ended_at"] is not None, f"cancelled {cancelled}")
    refused_resume = post_schedule_action(context, schedule_id, action="resume", headers=headers, expected_status=409)
    check(refused_resume["code"] == "invoice_schedule_state_conflict", f"resume after cancel {refused_resume}")
    post_schedule_action(context, schedule_id, action="cancel", headers=headers)
    # Remade with the cancelled one's prefix, its first period on a day the cancelled one billed: that order id is
    # taken, so it is refused rather than losing its first invoice. Reread: the moves above renamed the invoices.
    newest_billed_invoice = list_schedule_invoices(context, schedule_id, headers=headers)[0]
    remade_reply = context.http_client.post(
        f"{API_URL}/invoice-schedules",
        json={"amount": "5", "currency": "USD", "chain": FORK_CHAIN_KEY, "assets": ["USDC"], "interval": "weekly",
              "order_id_prefix": order_id_prefix, "starts_at": str(newest_billed_invoice["invoice_schedule_period_start"])},
        headers=headers,
    )  # fmt: skip
    check(remade_reply.status_code == 409 and remade_reply.json()["code"] == "order_id_prefix_taken",
          f"remade on a billed day: {remade_reply.status_code} {remade_reply.text[:200]}")  # fmt: skip
    return (
        {"pause_invoice_schedule": paused, "resume_invoice_schedule": resumed, "cancel_invoice_schedule": cancelled},
        {"daily_schedule_id": schedule_id, "invoice_ids": [invoice["id"] for invoice in invoices],
         "webhook_type": webhook_body["type"], "order_id_prefix": order_id_prefix},
    )  # fmt: skip


def run_documented_and_counted_schedules(
    context: FlowContext, *, headers: dict[str, str]
) -> tuple[dict[str, object], dict[str, str]]:
    """The docs' monthly schedule (its first invoice at once) and a one-invoice schedule that completes; returns the
    docs captures and the order ids to anonymise."""
    docs_prefix = f"{DOCS_ORDER_ID_PREFIX}-{secrets.token_hex(4)}"
    create_reply = context.http_client.post(
        f"{API_URL}/invoice-schedules",
        json={"amount": "5", "currency": "USD", "chain": FORK_CHAIN_KEY, "assets": ["USDC"], "interval": "monthly",
              "order_id_prefix": docs_prefix, "ends_after_invoice_count": 12},
        headers=headers,
    )  # fmt: skip
    check(create_reply.status_code == 201, f"docs schedule: {create_reply.status_code} {create_reply.text[:300]}")
    docs_schedule_id = str(create_reply.json()["id"])
    docs_invoices = wait_for_schedule_invoices(context, docs_schedule_id, headers=headers, count=1)
    counted_reply = context.http_client.post(
        f"{API_URL}/invoice-schedules",
        json={"amount": "1", "currency": "USD", "chain": FORK_CHAIN_KEY, "assets": ["USDC"], "interval": "weekly",
              "ends_after_invoice_count": 1},
        headers=headers,
    )  # fmt: skip
    check(counted_reply.status_code == 201, f"counted schedule: {counted_reply.status_code} {counted_reply.text[:300]}")
    counted_id = str(counted_reply.json()["id"])
    counted_invoice = wait_for_schedule_invoices(context, counted_id, headers=headers, count=1)[0]
    deadline = time.monotonic() + SCENARIO_TIMEOUT_SECONDS
    while (counted := read_schedule(context, counted_id, headers=headers))["status"] != "completed":
        check(time.monotonic() < deadline, f"one-invoice schedule not completed: {counted}")
        time.sleep(1)
    check(counted["created_invoice_count"] == 1 and counted["next_period_start"] is None
          and str(counted_invoice["order_id"]).startswith(f"{counted_id}-"),
          f"completed schedule {counted}")  # fmt: skip
    listed = context.http_client.get(f"{API_URL}/invoice-schedules", headers=headers).json()["invoice_schedules"]
    check({docs_schedule_id, counted_id} <= {item["id"] for item in listed}, "the list misses a schedule")
    docs_order_id = str(docs_invoices[0]["order_id"])
    return (
        {
            "create_invoice_schedule": create_reply.json(),
            "read_invoice_schedule": read_schedule(context, docs_schedule_id, headers=headers),
            "list_invoice_schedule_invoices": {"invoices": docs_invoices},
        },
        {docs_order_id: f"{DOCS_ORDER_ID_PREFIX}-20261020", docs_prefix: DOCS_ORDER_ID_PREFIX},
    )


def scenario_invoice_schedules(context: FlowContext) -> dict[str, object]:
    """A schedule billed once per period, its webhook event, a paid scheduled invoice, pause, resume and cancel."""
    start_webhook_receiver_container(context, receiver_environment={})
    try:
        api_token, webhook_secret, session_token = sign_up_with_token_and_webhook(
            payout_address=Account.create().address
        )
        bearer_headers = {"Authorization": f"Bearer {api_token}"}
        session_headers = account_request_headers(session_token, public_base_url=PUBLIC_BASE_URL)
        session_list = context.http_client.get(f"{API_URL}/invoice-schedules", headers=session_headers)
        check(session_list.status_code == 200 and session_list.json() == {"invoice_schedules": []},
              f"list by session: {session_list.status_code} {session_list.text[:200]}")  # fmt: skip
        action_captures, summary = run_daily_schedule(context, headers=bearer_headers, webhook_secret=webhook_secret)
        documented_captures, order_ids = run_documented_and_counted_schedules(context, headers=bearer_headers)
        docs_responses_path = write_docs_responses(
            context, captured={**documented_captures, **action_captures},
            order_ids={**order_ids, str(summary["order_id_prefix"]): "plan-77"},
            capture_name="invoice_schedules",
        )  # fmt: skip
        return {**summary, "docs_responses": str(docs_responses_path)}
    finally:
        stop_webhook_receiver()


INVOICE_SCHEDULE_SCENARIOS = [scenario_invoice_schedules]
