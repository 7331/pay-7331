"""Fork e2e scenario for an account's own server (2026-10-21; PLAN.md sections 7, 8.3 and 12), run by
``polygon_fork_flow.py`` after its own and alone with ``--scenario scenario_merchant_operations``:

- ``GET /rates``, signed out: every coin of the fork chain priced in USD and EUR, USDC at exactly 1 (the peg rule),
  with a 30-second public cache header.
- A fresh account makes an API token and sets a webhook on /settings; its token makes a USDC invoice and pays it on
  the fork until it is paid and swept, then an invoice whose note starts with ``=``.
- ``GET /invoices/export.csv`` by the token and by the session cookie: the same CSV, the documented header, the
  invoice's ``invoice``, ``payment`` and ``sweep`` rows with exact amounts and the payer; a formula note neutralised
  (``'=``); a creation window in the future gives the header alone. The payment's ``asset_price_usd`` is 1 or blank:
  anvil's block clock trails the wall clock, so on the fork no quote is live at the block's time and the credit took
  the feed price of the moment, which nothing stores.
- "Send test event" for ``webhook.test``, every invoice event type and every customer address event type
  (``static_address.*``, PLAN.md section 17): each arrives signed under the account's secret (Standard Webhooks),
  ``webhook-id`` ``test_...``, the body's ``"test": true``, the sample invoice or customer address in that type's
  state; the real ``invoice.paid`` carries no ``test`` key; an unknown type is 400; the sample ids read 404.
- A customer address made for ``customer-77`` and paid 5 USDC, for the docs' customer address examples
  (static_deposit_address_scenarios.py makes the requests).
- The developer docs' example responses (frontend/src/pages/developer_docs/developer_docs_responses.json) are
  captured from this run and anonymised (the fork's chain, URLs, addresses, hashes, ids and times replaced); the run
  fails when their shape (every key path and value type) differs from the committed file, so the docs cannot drift
  silently. ``PAY_E2E_WRITE_DOCS_RESPONSES=1`` writes the new capture over the committed file instead; either way the
  capture is kept in the scratch folder.
"""

import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import re
import secrets
import time

from account_passkey_session import (
    account_request_headers,
    add_virtual_authenticator,
    read_account_session_token,
    sign_up_account_on_page,
)
from account_settings_helpers import (
    close_settings_sheet,
    make_api_token_in_settings,
    read_webhook_receiver_records,
    read_webhook_record_type,
    set_webhook_in_settings,
    start_webhook_receiver_container,
    stop_webhook_receiver,
    wait_for_webhook_records,
)
from eth_account import Account
from fork_stack import (
    API_URL,
    CHAINS_TOML,
    FORK_CHAIN_KEY,
    PUBLIC_BASE_URL,
    REPOSITORY_ROOT,
    SCENARIO_TIMEOUT_SECONDS,
    USDC_ADDRESS,
    USDC_BASE_UNITS,
    FlowContext,
    FlowFailedError,
    build_erc20_transfer_data,
    check,
    create_local_account,
    expire_invoice_now,
    fund_with_usdc,
    send_transaction,
    to_base_units,
    wait_for_receipt,
)
import httpx
from playwright.sync_api import sync_playwright
from standardwebhooks import Webhook
from static_deposit_address_scenarios import capture_static_deposit_address_docs_requests

MERCHANT_OPERATIONS_PAYER = create_local_account()
DOCS_RESPONSES_PATH = REPOSITORY_ROOT / "frontend/src/pages/developer_docs/developer_docs_responses.json"
WRITE_DOCS_RESPONSES = os.environ.get("PAY_E2E_WRITE_DOCS_RESPONSES") == "1"
INVOICE_EXPORT_COLUMNS = [
    "row_kind", "occurred_at", "invoice_id", "invoice_created_at", "order_id", "invoice_status", "partial",
    "late_payment", "note", "fiat_currency", "fiat_amount", "chain", "asset", "amount_due", "amount_received",
    "destination_address", "deposit_address", "paid_at", "payment_link_id", "row_status", "row_asset", "row_amount",
    "asset_price_usd", "transaction_hash", "block_number", "from_address", "to_address",
]  # fmt: skip
SAMPLE_EVENT_TYPES = [
    "webhook.test", "invoice.payment_detected", "invoice.underpaid", "invoice.paid", "invoice.overpaid",
    "invoice.expired", "invoice.cancelled", "invoice.swept", "invoice.sweep_failed", "invoice.late_payment",
    "invoice.stray_payment", "schedule.invoice_created", "static_address.payment_received",
    "static_address.stray_payment", "static_address.swept", "static_address.sweep_failed",
]  # fmt: skip
# The status each sample invoice is in after its event (backend/app/payments/account_webhooks/sample_webhook_events.py).
SAMPLE_STATUS_BY_EVENT_TYPE = {
    "invoice.payment_detected": "confirming", "invoice.underpaid": "underpaid", "invoice.paid": "paid",
    "invoice.overpaid": "overpaid", "invoice.expired": "expired", "invoice.cancelled": "cancelled",
    "invoice.swept": "paid", "invoice.sweep_failed": "paid", "invoice.late_payment": "expired",
    "invoice.stray_payment": "awaiting_payment", "schedule.invoice_created": "awaiting_payment",
}  # fmt: skip
# Public writes are 10 a minute per network (GCRA): the sample events and the coin pick wait out a 429's Retry-After.
MAXIMUM_RATE_LIMIT_WAIT_SECONDS = 70
# A spreadsheet would run it (a full-width = up front, a plain one after a ';' separator): the export writes a ' before
# each (OWASP CSV injection).
FORMULA_NOTE = "\uff1d1+1 e2e;=2+2"
NEUTRALIZED_FORMULA_NOTE = "'\uff1d1+1 e2e;'=2+2"


def send_respecting_rate_limit(
    context: FlowContext, *, method: str, url: str, **request_options: object
) -> httpx.Response:
    deadline = time.monotonic() + SCENARIO_TIMEOUT_SECONDS
    while True:
        reply = context.http_client.request(method, url, **request_options)  # type: ignore[arg-type]
        if reply.status_code != 429:
            return reply
        retry_after_seconds = float(reply.headers.get("retry-after", "5"))
        check(time.monotonic() + retry_after_seconds < deadline, f"{method} {url}: still 429 at the deadline")
        context.write_log("rate_limited", url=url, retry_after_seconds=retry_after_seconds)
        time.sleep(min(retry_after_seconds, MAXIMUM_RATE_LIMIT_WAIT_SECONDS))


def check_rates(context: FlowContext) -> dict[str, object]:
    reply = context.http_client.get(f"{API_URL}/rates")
    check(reply.status_code == 200, f"GET /rates: {reply.status_code} {reply.text[:200]}")
    check(reply.headers.get("cache-control") == "public, max-age=10",
          f"GET /rates cache-control: {reply.headers.get('cache-control')}")  # fmt: skip
    rates = reply.json()["rates"]
    fork_rates = {rate["asset"]: rate for rate in rates if rate["chain"] == FORK_CHAIN_KEY}
    fork_asset_symbols = [asset["symbol"] for asset in CHAINS_TOML[FORK_CHAIN_KEY]["assets"]]
    check(sorted(fork_rates) == sorted(fork_asset_symbols), f"GET /rates listed {sorted(fork_rates)}")
    check(fork_rates["USDC"]["usd"] == "1", f"USDC at {fork_rates['USDC']['usd']} USD, expected exactly 1 (the peg)")
    for symbol, rate in fork_rates.items():
        check(rate["usd"] is not None and Decimal(rate["usd"]) > 0 and Decimal(rate["eur"]) > 0,
              f"{symbol}: usd {rate['usd']} eur {rate['eur']}")  # fmt: skip
    return reply.json()


def wait_for_token_invoice(
    context: FlowContext, *, invoice_id: str, bearer_headers: dict[str, str]
) -> dict[str, object]:
    """Paid, and its sweep final, read with the account's own token."""
    deadline = time.monotonic() + SCENARIO_TIMEOUT_SECONDS
    while True:
        invoice = context.http_client.get(f"{API_URL}/invoices/{invoice_id}", headers=bearer_headers).json()
        sweeps = invoice.get("sweeps") or []
        if invoice.get("status") == "paid" and sweeps and sweeps[-1]["status"] == "final":
            return invoice
        check(time.monotonic() < deadline, f"{invoice_id}: not paid and swept in {SCENARIO_TIMEOUT_SECONDS} s")
        time.sleep(1)


def read_csv_rows(csv_text: str) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(csv_text, newline=""))
    return list(reader.fieldnames or []), list(reader)


def check_invoice_export(
    context: FlowContext,
    *,
    bearer_headers: dict[str, str],
    session_headers: dict[str, str],
    invoice: dict[str, object],
    payer_address: str,
) -> str:
    """The CSV by token and by session, its header and the paid invoice's rows; returns the token's CSV."""
    token_reply = context.http_client.get(f"{API_URL}/invoices/export.csv", headers=bearer_headers)
    check(token_reply.status_code == 200, f"export by token: {token_reply.status_code} {token_reply.text[:200]}")
    check(token_reply.headers.get("content-type", "").startswith("text/csv"),
          f"export content-type {token_reply.headers.get('content-type')}")  # fmt: skip
    check("attachment" in token_reply.headers.get("content-disposition", ""), "export is not an attachment")
    session_reply = context.http_client.get(f"{API_URL}/invoices/export.csv", headers=session_headers)
    check(session_reply.status_code == 200, f"export by session: {session_reply.status_code}")
    check(session_reply.text == token_reply.text, "the session's CSV differs from the token's")
    header, rows = read_csv_rows(token_reply.text)
    check(header == INVOICE_EXPORT_COLUMNS, f"CSV header {header}")
    invoice_rows = [row for row in rows if row["invoice_id"] == invoice["id"]]
    rows_by_kind: dict[str, list[dict[str, str]]] = {}
    for row in invoice_rows:
        rows_by_kind.setdefault(row["row_kind"], []).append(row)
    check(sorted(rows_by_kind) == ["invoice", "payment", "sweep"], f"row kinds {sorted(rows_by_kind)}")
    invoice_row = rows_by_kind["invoice"][0]
    check(invoice_row["invoice_status"] == "paid" and invoice_row["amount_due"] == invoice["amount_due"]
          and invoice_row["amount_received"] == invoice["amount_received"] and invoice_row["fiat_amount"] == "5.00"
          and invoice_row["order_id"] == invoice["order_id"] and invoice_row["note"] == invoice["note"]
          and invoice_row["paid_at"].endswith("Z") and invoice_row["occurred_at"] == invoice_row["invoice_created_at"],
          f"invoice row {invoice_row}")  # fmt: skip
    payment_row = rows_by_kind["payment"][0]
    payment = invoice["payments"][0]  # type: ignore[index]
    check(payment_row["row_status"] == "final" and payment_row["row_asset"] == "USDC"
          and payment_row["row_amount"] == invoice["amount_due"] and payment_row["from_address"] == payer_address
          and payment_row["to_address"] == invoice["deposit_address"]
          and payment_row["transaction_hash"] == payment["tx_hash"]
          and payment_row["block_number"] == str(payment["block_number"]) and payment_row["asset_price_usd"] in {"1", ""}
          and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", payment_row["occurred_at"]) is not None,
          f"payment row {payment_row}")  # fmt: skip
    sweep_row = rows_by_kind["sweep"][0]
    check(sweep_row["row_status"] == "final" and sweep_row["row_amount"] == invoice["amount_due"]
          and sweep_row["to_address"] == invoice["destination_address"]
          and sweep_row["from_address"] == invoice["deposit_address"],
          f"sweep row {sweep_row}")  # fmt: skip
    formula_notes = [row["note"] for row in rows if row["row_kind"] == "invoice" and row["invoice_id"] != invoice["id"]]
    check(formula_notes == [NEUTRALIZED_FORMULA_NOTE], f"formula note exported as {formula_notes}")
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    empty_reply = context.http_client.get(
        f"{API_URL}/invoices/export.csv", params={"created_from": future}, headers=bearer_headers
    )
    check(empty_reply.status_code == 200 and read_csv_rows(empty_reply.text)[1] == [],
          f"export from tomorrow: {empty_reply.status_code} {empty_reply.text[:200]}")  # fmt: skip
    return token_reply.text


def send_sample_webhook_events(context: FlowContext, *, session_headers: dict[str, str]) -> dict[str, object]:
    """Every sample event type, then an unknown one (400); returns the first delivery view."""
    first_delivery: dict[str, object] = {}
    for event_type in SAMPLE_EVENT_TYPES:
        reply = send_respecting_rate_limit(
            context, method="POST", url=f"{API_URL}/account-webhook/test-events",
            json={"event_type": event_type}, headers=session_headers,
        )  # fmt: skip
        check(reply.status_code == 202, f"test event {event_type}: {reply.status_code} {reply.text[:200]}")
        first_delivery = first_delivery or reply.json()
    unknown_reply = send_respecting_rate_limit(
        context, method="POST", url=f"{API_URL}/account-webhook/test-events",
        json={"event_type": "invoice.unknown"}, headers=session_headers,
    )  # fmt: skip
    check(unknown_reply.status_code == 400, f"unknown test event type: {unknown_reply.status_code}")
    return first_delivery


def check_sample_webhook_deliveries(
    context: FlowContext, *, webhook_secret: str, bearer_headers: dict[str, str], real_invoice_id: str
) -> dict[str, dict[str, object]]:
    """Each sample arrives signed and marked as a test; the real ``invoice.paid`` is not marked. Returns the sample
    bodies by type."""
    records = wait_for_webhook_records(
        context, description="every sample event and the real invoice.paid",
        condition=lambda records: sum(1 for record in records
                                      if json.loads(str(record["body"])).get("test")) >= len(SAMPLE_EVENT_TYPES)
        and any(json.loads(str(record["body"]))["data"].get("id") == real_invoice_id for record in records),
    )  # fmt: skip
    webhook_verifier = Webhook(webhook_secret)
    sample_bodies: dict[str, dict[str, object]] = {}
    for record in records:
        webhook_verifier.verify(str(record["body"]), dict(record["headers"]))  # type: ignore[call-overload]
        body = json.loads(str(record["body"]))
        webhook_id = str(record["headers"]["webhook-id"])  # type: ignore[index]
        if body["data"].get("id") == real_invoice_id:
            check("test" not in body and not webhook_id.startswith("test_"),
                  f"the real {body['type']} is marked as a test")  # fmt: skip
            continue
        check(body.get("test") is True and webhook_id.startswith("test_"),
              f"sample {body['type']}: test {body.get('test')}, webhook-id {webhook_id}")  # fmt: skip
        sample_bodies[read_webhook_record_type(record)] = body
    check(sorted(sample_bodies) == sorted(SAMPLE_EVENT_TYPES), f"samples received: {sorted(sample_bodies)}")
    check(sample_bodies["webhook.test"]["data"] == {"message": "A test event from pay-7331."},
          f"webhook.test data {sample_bodies['webhook.test']['data']}")  # fmt: skip
    for event_type, expected_status in SAMPLE_STATUS_BY_EVENT_TYPE.items():
        sample_invoice = sample_bodies[event_type]["data"]
        check(sample_invoice["status"] == expected_status and sample_invoice["metadata"]["test"] is True  # type: ignore[index]
              and str(sample_invoice["id"]).startswith("inv_Test"),
              f"sample {event_type}: {sample_invoice['status']} {sample_invoice['id']}")  # fmt: skip
    # As a real paid invoice: its payment URI asks for nothing more. A late payment lists no payment row of its own,
    # only the sweep the rescan asked for.
    paid_sample: dict[str, object] = sample_bodies["invoice.paid"]["data"]  # type: ignore[assignment]
    check(str(paid_sample["payment_uri"]).endswith("&uint256=0") and paid_sample["amount_remaining"] == "0",
          f"paid sample payment_uri {paid_sample['payment_uri']}")  # fmt: skip
    late_sample: dict[str, object] = sample_bodies["invoice.late_payment"]["data"]  # type: ignore[assignment]
    check(late_sample["late_payment"] is True and late_sample["payments"] == []
          and late_sample["sweeps"] == [{"tx_hash": None, "asset_symbol": None, "amount": None, "status": "pending"}],
          f"late payment sample {late_sample['payments']} {late_sample['sweeps']}")  # fmt: skip
    sample_invoice_id = str(paid_sample["id"])
    sample_read = context.http_client.get(f"{API_URL}/invoices/{sample_invoice_id}", headers=bearer_headers)
    check(sample_read.status_code == 404, f"the sample invoice read {sample_read.status_code}, expected 404")
    # A customer address sample: the address and the payment or sweep, as the real event's data.
    payment_sample: dict[str, object] = sample_bodies["static_address.payment_received"]["data"]  # type: ignore[assignment]
    stray_sample: dict[str, object] = sample_bodies["static_address.stray_payment"]["data"]  # type: ignore[assignment]
    failed_sample: dict[str, object] = sample_bodies["static_address.sweep_failed"]["data"]  # type: ignore[assignment]
    sample_address: dict[str, object] = payment_sample["static_deposit_address"]  # type: ignore[assignment]
    check(str(sample_address["id"]).startswith("sda_Test") and payment_sample["payment"]["credited"] is True  # type: ignore[index]
          and stray_sample["payment"]["uncredited_reason"] == "wrong_coin"  # type: ignore[index]
          and failed_sample["sweep"]["status"] == "failed"  # type: ignore[index]
          and failed_sample["static_deposit_address"]["sweep_blocked"] is True,  # type: ignore[index]
          f"customer address samples: {payment_sample} {stray_sample} {failed_sample}")  # fmt: skip
    sample_address_read = context.http_client.get(
        f"{API_URL}/static-deposit-addresses/{sample_address['id']}", headers=bearer_headers
    )
    check(sample_address_read.status_code == 404,
          f"the sample customer address read {sample_address_read.status_code}, expected 404")  # fmt: skip
    return sample_bodies


# ---- The docs' example responses: captured above, anonymised here ----

ANONYMOUS_BASE_TIME = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
TIME_PATTERN = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z")
ADDRESS_PATTERN = re.compile(r"0x[0-9a-fA-F]{40}(?![0-9a-fA-F])")
HASH_PATTERN = re.compile(r"0x[0-9a-fA-F]{64}(?![0-9a-fA-F])")
# Every random public id, and a test delivery's webhook-id, which is as random.
PUBLIC_ID_PATTERN = re.compile(r"\b(?:inv|lnk|sda|sch|evt|whd|tok|pk|test)_[1-9A-HJ-NP-Za-km-z]{22}\b")
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class DocsResponseAnonymizer:
    """Replaces what only this run has with stable stand-ins, the same value for the same original everywhere: the
    fork's chain becomes ``polygon`` as production names it, the stack's URL ``https://pay.7331.org``, every address
    but the chains' token addresses a ``0x`` pattern, every hash, id and order id a fixed one, times shifted so the
    earliest is 2026-10-20 12:00 UTC (whole seconds)."""

    def __init__(self, *, kept_addresses: set[str], earliest_time: datetime, order_ids: dict[str, str]) -> None:
        self.kept_addresses = {address.lower() for address in kept_addresses}
        self.earliest_time = earliest_time
        self.order_ids = order_ids
        self.replacements_by_kind: dict[str, dict[str, str]] = {"address": {}, "hash": {}, "public_id": {}}

    def stand_in(self, original: str, *, kind: str) -> str:
        """The n-th distinct value of a kind: ``0x0101...`` for the first address or hash, a base58 run for an id."""
        replacements = self.replacements_by_kind[kind]
        if original.lower() not in replacements:
            index = len(replacements) + 1
            if kind == "address":
                replacement = "0x" + f"{index:02x}" * 20
            elif kind == "hash":
                replacement = "0x" + f"{index:02x}" * 32
            else:
                prefix = original.split("_", 1)[0]
                replacement = f"{prefix}_" + "".join(BASE58_ALPHABET[(index * 7 + step) % 58] for step in range(22))
            replacements[original.lower()] = replacement
        return replacements[original.lower()]

    def shift_time(self, match: re.Match[str]) -> str:
        moment = datetime.fromisoformat(match.group(0))
        shifted = ANONYMOUS_BASE_TIME + (moment - self.earliest_time)
        return shifted.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

    def anonymize_text(self, text: str) -> str:
        text = text.replace(PUBLIC_BASE_URL, "https://pay.7331.org")
        for original, replacement in self.order_ids.items():
            text = text.replace(original, replacement)
        text = HASH_PATTERN.sub(lambda match: self.stand_in(match.group(0), kind="hash"), text)
        text = ADDRESS_PATTERN.sub(
            lambda match: (
                match.group(0)
                if match.group(0).lower() in self.kept_addresses
                else self.stand_in(match.group(0), kind="address")
            ),
            text,
        )
        text = PUBLIC_ID_PATTERN.sub(
            lambda match: (
                match.group(0)
                if match.group(0).startswith(("inv_Test", "sda_Test", "sch_Test"))
                else self.stand_in(match.group(0), kind="public_id")
            ),
            text,
        )
        return TIME_PATTERN.sub(self.shift_time, text)

    def anonymize(self, value: object) -> object:
        match value:
            case dict():
                anonymized = {key: self.anonymize(item) for key, item in value.items()}
                if anonymized.get("key") == FORK_CHAIN_KEY and "chain_id" in anonymized:
                    anonymized.update(key="polygon", name=CHAINS_TOML["polygon"]["name"],
                                      expected_finality_seconds=CHAINS_TOML["polygon"]["expected_finality_seconds"])  # fmt: skip
                if anonymized.get("chain") == FORK_CHAIN_KEY:
                    anonymized["chain"] = "polygon"
                # An invoice schedule's offered chains (PLAN.md section 18).
                if isinstance(anonymized.get("chains"), list):
                    anonymized["chains"] = [
                        "polygon" if chain_key == FORK_CHAIN_KEY else chain_key for chain_key in anonymized["chains"]
                    ]
                return anonymized
            case list():
                return [self.anonymize(item) for item in value]
            case str():
                return self.anonymize_text(value).replace(f",{FORK_CHAIN_KEY},", ",polygon,")
            case _:
                return value


def describe_shape(value: object, *, path: str = "$") -> set[str]:
    """Every key path with its JSON type; list items merged, so the shape does not depend on how many there are."""
    match value:
        case dict():
            return {f"{path}:object"} | {
                shape for key, item in value.items() for shape in describe_shape(item, path=f"{path}.{key}")
            }
        case list():
            return {f"{path}:array"} | {shape for item in value for shape in describe_shape(item, path=f"{path}[]")}
        case None:
            return {f"{path}:null"}
        case bool():
            return {f"{path}:boolean"}
        case int() | float():
            return {f"{path}:number"}
        case _:
            return {f"{path}:string"}


def find_earliest_time(value: object) -> datetime:
    times = [datetime.fromisoformat(found) for found in TIME_PATTERN.findall(json.dumps(value))]
    return min(times)


def write_docs_responses(
    context: FlowContext, *, captured: dict[str, object], order_ids: dict[str, str], capture_name: str = "merchant"
) -> Path:
    """Anonymises the capture, keeps it in the scratch folder, and compares its shape with the same keys of the
    committed file (or writes them into it with ``PAY_E2E_WRITE_DOCS_RESPONSES=1``). Each scenario that captures owns
    its own keys (this one's, invoice_schedule_scenarios.py's), so either can run alone."""
    token_addresses = {
        asset["token_address"] for chain in CHAINS_TOML.values() for asset in chain.get("assets", [])
        if asset.get("token_address")
    }  # fmt: skip
    anonymizer = DocsResponseAnonymizer(
        kept_addresses=token_addresses, earliest_time=find_earliest_time(captured), order_ids=order_ids
    )
    anonymized = anonymizer.anonymize(captured)
    if not isinstance(anonymized, dict):
        raise FlowFailedError("the docs capture is not a JSON object")
    capture_text = json.dumps(anonymized, indent=2, ensure_ascii=False) + "\n"
    capture_path = context.scratch_folder / f"developer_docs_responses_{capture_name}.json"
    capture_path.write_text(capture_text)
    committed_responses = json.loads(DOCS_RESPONSES_PATH.read_text()) if DOCS_RESPONSES_PATH.exists() else {}
    if WRITE_DOCS_RESPONSES:
        merged_responses = {**committed_responses, **anonymized}
        DOCS_RESPONSES_PATH.write_text(json.dumps(merged_responses, indent=2, ensure_ascii=False) + "\n")
        return DOCS_RESPONSES_PATH
    committed = {key: committed_responses[key] for key in anonymized if key in committed_responses}
    missing_paths = sorted(describe_shape(anonymized) - describe_shape(committed))
    stale_paths = sorted(describe_shape(committed) - describe_shape(anonymized))
    check(not missing_paths and not stale_paths,
          f"the docs' example responses drifted from the API (new {missing_paths[:8]}, gone {stale_paths[:8]}); "
          f"rerun with PAY_E2E_WRITE_DOCS_RESPONSES=1 or copy {capture_path}")  # fmt: skip
    return capture_path


def capture_docs_requests(
    context: FlowContext, *, bearer_headers: dict[str, str], paid_invoice: dict[str, object]
) -> dict[str, object]:
    """The documented requests the scenario has not made yet, as the docs' examples send them."""
    minimal_reply = context.http_client.post(
        f"{API_URL}/invoices", json={"amount": "5", "currency": "USD"}, headers=bearer_headers
    )
    check(minimal_reply.status_code == 201, f"docs create: {minimal_reply.status_code} {minimal_reply.text[:200]}")
    minimal_invoice = minimal_reply.json()
    check(minimal_invoice["status"] == "awaiting_asset" or minimal_invoice["asset"] is not None,
          f"docs create status {minimal_invoice['status']}")  # fmt: skip
    chosen_reply = send_respecting_rate_limit(
        context, method="POST", url=f"{API_URL}/public/invoices/{minimal_invoice['id']}/asset",
        json={"chain": FORK_CHAIN_KEY, "asset": "USDC"},
    )  # fmt: skip
    check(chosen_reply.status_code == 200, f"docs choose pair: {chosen_reply.status_code} {chosen_reply.text[:200]}")
    order_reply = context.http_client.get(
        f"{API_URL}/invoices", params={"order_id": paid_invoice["order_id"]}, headers=bearer_headers
    )
    prefix_reply = context.http_client.get(
        f"{API_URL}/invoices", params={"order_id_prefix": "customer-77:", "status": "paid"}, headers=bearer_headers
    )
    check([item["id"] for item in prefix_reply.json()["invoices"]] == [paid_invoice["id"]],
          f"order_id_prefix found {[item['id'] for item in prefix_reply.json()['invoices']]}")  # fmt: skip
    return {
        "create_invoice": minimal_invoice,
        "choose_payment_pair": chosen_reply.json(),
        "find_order": order_reply.json(),
        "list_customer_invoices": prefix_reply.json(),
        "list_chains": context.http_client.get(f"{API_URL}/chains").json(),
    }


# The docs show the first events of the token's event page, as they show the first CSV rows.
DOCUMENTED_EVENT_COUNT = 2


def capture_merchant_route_docs_requests(
    context: FlowContext, *, bearer_headers: dict[str, str], refresh_order_id: str, shop_order_id: str
) -> dict[str, object]:
    """The token's merchant routes (2026-10-21) as the docs' examples send them: cancel, refresh an expired order,
    shop links with a per-invoice callback, the event list and a quantity link made and closed."""
    cancel_reply = context.http_client.post(
        f"{API_URL}/invoices", json={"amount": "5", "currency": "USD"}, headers=bearer_headers
    )
    check(cancel_reply.status_code == 201, f"docs cancel create: {cancel_reply.status_code} {cancel_reply.text[:200]}")
    cancelled_reply = context.http_client.post(
        f"{API_URL}/invoices/{cancel_reply.json()['id']}/cancel", headers=bearer_headers
    )
    check(cancelled_reply.status_code == 200 and cancelled_reply.json()["status"] == "cancelled",
          f"docs cancel: {cancelled_reply.status_code} {cancelled_reply.text[:200]}")  # fmt: skip
    expired_reply = context.http_client.post(
        f"{API_URL}/invoices", json={"amount": "5", "currency": "USD", "order_id": refresh_order_id},
        headers=bearer_headers,
    )  # fmt: skip
    check(expired_reply.status_code == 201, f"docs refresh create: {expired_reply.status_code}")
    expire_invoice_now(context, invoice_id=str(expired_reply.json()["id"]), signed_out=True)
    refreshed_reply = context.http_client.post(
        f"{API_URL}/invoices",
        json={"amount": "5", "currency": "USD", "order_id": refresh_order_id, "refresh_expired": True},
        headers=bearer_headers,
    )
    check(refreshed_reply.status_code == 201
          and refreshed_reply.json()["refreshed_from_invoice_id"] == expired_reply.json()["id"],
          f"docs refresh: {refreshed_reply.status_code} {refreshed_reply.text[:200]}")  # fmt: skip
    shop_reply = context.http_client.post(
        f"{API_URL}/invoices",
        json={"amount": "5", "currency": "USD", "order_id": shop_order_id, "return_url": "https://shop.example/cart",
              "success_url": "https://shop.example/thanks", "callback_url": "https://shop.example/pay-events"},
        headers=bearer_headers,
    )  # fmt: skip
    check(shop_reply.status_code == 201 and shop_reply.json()["callback_url"] == "https://shop.example/pay-events",
          f"docs shop links: {shop_reply.status_code} {shop_reply.text[:200]}")  # fmt: skip
    events_reply = context.http_client.get(f"{API_URL}/events", params={"limit": "50"}, headers=bearer_headers)
    check(events_reply.status_code == 200, f"docs events: {events_reply.status_code} {events_reply.text[:200]}")
    event_page = events_reply.json()
    link_reply = context.http_client.post(
        f"{API_URL}/payment-links",
        json={"amount": "5", "currency": "USD", "chain": FORK_CHAIN_KEY, "assets": ["USDC", "POL"], "quantity": 10},
        headers=bearer_headers,
    )  # fmt: skip
    check(link_reply.status_code == 201, f"docs link create: {link_reply.status_code} {link_reply.text[:200]}")
    closed_link_reply = context.http_client.post(
        f"{API_URL}/payment-links/{link_reply.json()['id']}/close", headers=bearer_headers
    )
    check(closed_link_reply.status_code == 200, f"docs link close: {closed_link_reply.status_code}")
    return {
        "cancel_invoice": cancelled_reply.json(),
        "refresh_expired_order": refreshed_reply.json(),
        "shop_links_and_callback": shop_reply.json(),
        "list_invoice_events": {**event_page, "events": event_page["events"][:DOCUMENTED_EVENT_COUNT]},
        "create_payment_link": link_reply.json(),
        "close_payment_link": closed_link_reply.json(),
    }


def sign_up_with_token_and_webhook(*, payout_address: str) -> tuple[str, str, str]:
    """A fresh account, its API token and its webhook (pointing at the receiver), made on /settings: (token, webhook
    secret, session cookie token)."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_context().new_page()
            add_virtual_authenticator(page)
            sign_up_account_on_page(page, public_base_url=PUBLIC_BASE_URL,
                                    username=f"e2e_{secrets.token_hex(4)}", payout_address=payout_address)  # fmt: skip
            page.goto(f"{PUBLIC_BASE_URL}/settings")
            api_token = make_api_token_in_settings(page)
            close_settings_sheet(page)
            webhook_secret = set_webhook_in_settings(page)
            return api_token, webhook_secret, read_account_session_token(page.context)
        finally:
            browser.close()


def scenario_merchant_operations(context: FlowContext) -> dict[str, object]:
    """Rates, the CSV export, sample webhook events and the docs' example responses, on a fresh account."""
    payout_address = Account.create().address
    rates = check_rates(context)
    start_webhook_receiver_container(context, receiver_environment={})
    try:
        api_token, webhook_secret, session_token = sign_up_with_token_and_webhook(payout_address=payout_address)
        bearer_headers = {"Authorization": f"Bearer {api_token}"}
        session_headers = account_request_headers(session_token, public_base_url=PUBLIC_BASE_URL)
        order_id = f"customer-77:order-{secrets.token_hex(4)}"
        create_reply = context.http_client.post(
            f"{API_URL}/invoices",
            json={"chain": FORK_CHAIN_KEY, "amount": "5", "currency": "USD", "assets": ["USDC"], "order_id": order_id,
                  "note": "Premium plan, 1 month", "metadata": {"customer_id": 77}},
            headers=bearer_headers,
        )  # fmt: skip
        check(create_reply.status_code == 201, f"create: {create_reply.status_code} {create_reply.text[:200]}")
        invoice_id = str(create_reply.json()["id"])
        payer_address = MERCHANT_OPERATIONS_PAYER.address
        # 5 USDC for the invoice, 5 for the docs' customer address.
        fund_with_usdc(context.http_client, payer_address, base_units=10 * USDC_BASE_UNITS)
        amount_base_units = to_base_units(str(create_reply.json()["amount_due"]), decimals=6)
        wait_for_receipt(context.http_client, send_transaction(
            context.http_client, sender=payer_address, to=USDC_ADDRESS,
            data=build_erc20_transfer_data(str(create_reply.json()["deposit_address"]), amount_base_units)))  # fmt: skip
        paid_invoice = wait_for_token_invoice(context, invoice_id=invoice_id, bearer_headers=bearer_headers)
        # Made after the paid one, so the docs' first CSV lines stay that invoice's; its note is a formula.
        formula_reply = context.http_client.post(
            f"{API_URL}/invoices", json={"amount": "1", "currency": "USD", "note": FORMULA_NOTE}, headers=bearer_headers
        )
        check(formula_reply.status_code == 201, f"formula note invoice: {formula_reply.status_code}")
        csv_text = check_invoice_export(
            context, bearer_headers=bearer_headers, session_headers=session_headers, invoice=paid_invoice,
            payer_address=payer_address,
        )  # fmt: skip
        test_delivery = send_sample_webhook_events(context, session_headers=session_headers)
        sample_bodies = check_sample_webhook_deliveries(
            context, webhook_secret=webhook_secret, bearer_headers=bearer_headers, real_invoice_id=invoice_id
        )
        refresh_order_id = f"order-{secrets.token_hex(4)}"
        shop_order_id = f"order-{secrets.token_hex(4)}"
        captured: dict[str, object] = {
            **capture_docs_requests(context, bearer_headers=bearer_headers, paid_invoice=paid_invoice),
            **capture_merchant_route_docs_requests(
                context, bearer_headers=bearer_headers, refresh_order_id=refresh_order_id, shop_order_id=shop_order_id
            ),
            **capture_static_deposit_address_docs_requests(
                context, bearer_headers=bearer_headers, payer_address=payer_address
            ),
            "read_invoice": paid_invoice,
            "list_rates": rates,
            "invoice_export_csv": "".join(csv_text.splitlines(keepends=True)[:4]),
            "test_webhook_event": test_delivery,
            "webhook_events": {event_type: sample_bodies[event_type] for event_type in SAMPLE_EVENT_TYPES},
        }
        docs_responses_path = write_docs_responses(
            context,
            captured=captured,
            order_ids={order_id: "customer-77:order-1042", refresh_order_id: "order-1042", shop_order_id: "order-1043"},
        )
        if len(read_webhook_receiver_records()) < len(SAMPLE_EVENT_TYPES) + 1:
            raise FlowFailedError("the receiver lost deliveries")
        return {"invoice_id": invoice_id, "csv_rows": len(csv_text.splitlines()) - 1,
                "sample_event_types": sorted(sample_bodies), "docs_responses": str(docs_responses_path)}  # fmt: skip
    finally:
        stop_webhook_receiver()


MERCHANT_OPERATIONS_SCENARIOS = [scenario_merchant_operations]
