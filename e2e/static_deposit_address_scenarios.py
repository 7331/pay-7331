"""Fork e2e scenario for static deposit addresses (PLAN.md section 17),
run by ``polygon_fork_flow.py`` after its own and alone with ``--scenario scenario_static_deposit_address``:

- A fresh account's server (API token) creates a customer's address twice and gets the same one; the prefix list
  finds it; its stored salt is the domain-separated HMAC of (account, chain, customer reference) under the stack's
  ``PAY_INVOICE_SALT_SEED``.
- 5 USDC to it is credited once at 5.00 USD with its sender and transaction, swept to the payout address, and
  ``static_address.payment_received`` arrives signed at the webhook; the token's ``GET /events`` lists it too.
- Moved to the cold scan tier (made and last paid over a day ago, by SQL), a 2 USDC payment is still found by the cold
  pass (every 20 s on the fork, chains.toml) and credited, and the address is back in the hot tier
  (``last_received_at`` recent).

``capture_static_deposit_address_docs_requests`` makes the docs' customer address requests for
merchant_operations_scenarios.py, which writes every captured response into the docs' file.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import hmac
import json
import re
import secrets
import time

from account_passkey_session import add_virtual_authenticator, sign_up_account_on_page
from account_settings_helpers import (
    close_settings_sheet,
    make_api_token_in_settings,
    read_webhook_receiver_records,
    read_webhook_record_type,
    select_webhook_records,
    set_webhook_in_settings,
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
    read_environment_setting,
    read_usdc_balance,
    run_sql,
    send_transaction,
    wait_for_receipt,
)
from playwright.sync_api import sync_playwright
from standardwebhooks import Webhook

STATIC_DEPOSIT_SCENARIO_PAYER = create_local_account()
# backend/app/payments/static_deposit_addresses/deposit_salts.py's domain prefix.
STATIC_DEPOSIT_SALT_DOMAIN = b"pay-7331 static deposit address v1\x00"
STATIC_DEPOSIT_ADDRESS_ID_PATTERN = re.compile(r"sda_[1-9A-HJ-NP-Za-km-z]{22}")
# Older than the hot window (chains.toml's default ``static_deposit_hot_window_seconds``, one day).
COLD_TIER_AGE = timedelta(days=2)
# The second payment is read by a cold pass, so the address's last receipt is at most this old afterwards.
RECENT_RECEIPT_AGE = timedelta(minutes=10)
DOCUMENTED_CUSTOMER_REFERENCE = "customer-77"


def derive_static_deposit_salt_hex(seed_hex: str, *, account_id: int, chain_key: str, customer_reference: str) -> str:
    """backend/app/payments/static_deposit_addresses/deposit_salts.py, written out again so the scenario checks it."""
    message = STATIC_DEPOSIT_SALT_DOMAIN
    for field in (str(account_id), chain_key, customer_reference):
        encoded = field.encode("utf-8")
        message += len(encoded).to_bytes(2, "big") + encoded
    return hmac.digest(bytes.fromhex(seed_hex), message, "sha256").hex()


def check_static_deposit_address_id(static_deposit_address_id: str) -> str:
    """The id as SQL may hold it: only a well-formed ``sda_`` id goes into a statement."""
    if STATIC_DEPOSIT_ADDRESS_ID_PATTERN.fullmatch(static_deposit_address_id) is None:
        raise FlowFailedError(f"not a static deposit address id: {static_deposit_address_id!r}")
    return static_deposit_address_id


def wait_for_static_deposit_address(
    context: FlowContext,
    *,
    static_deposit_address_id: str,
    bearer_headers: dict[str, str],
    description: str,
    condition: Callable[[dict[str, object]], bool],
) -> dict[str, object]:
    deadline = time.monotonic() + SCENARIO_TIMEOUT_SECONDS
    while True:
        reply = context.http_client.get(
            f"{API_URL}/static-deposit-addresses/{static_deposit_address_id}", headers=bearer_headers
        )
        check(reply.status_code == 200, f"static address read: {reply.status_code} {reply.text[:200]}")
        static_deposit_address = reply.json()
        if condition(static_deposit_address):
            return static_deposit_address
        if time.monotonic() > deadline:
            context.write_log("timeout", waiting_for=description, static_deposit_address=static_deposit_address)
            raise FlowFailedError(f"{static_deposit_address_id}: no {description} in {SCENARIO_TIMEOUT_SECONDS} s")
        time.sleep(2)


def wait_for_usdc_balance(context: FlowContext, *, address: str, base_units: int, description: str) -> None:
    deadline = time.monotonic() + SCENARIO_TIMEOUT_SECONDS
    while read_usdc_balance(context.http_client, address) != base_units:
        if time.monotonic() > deadline:
            raise FlowFailedError(f"{description}: {read_usdc_balance(context.http_client, address)} USDC base units, "
                                  f"expected {base_units}")  # fmt: skip
        time.sleep(2)


def count_static_deposit_events(context: FlowContext, *, static_deposit_address_id: str, event_type: str) -> int:
    checked_id = check_static_deposit_address_id(static_deposit_address_id)
    return int(run_sql(context, "SELECT count(*) FROM account_events WHERE invoice_id IS NULL "  # noqa: S608
                                f"AND event_type = '{event_type}' "
                                f"AND data->'static_deposit_address'->>'id' = '{checked_id}'"))  # fmt: skip


def wait_for_static_deposit_events(
    context: FlowContext, *, static_deposit_address_id: str, event_type: str, count: int
) -> None:
    deadline = time.monotonic() + SCENARIO_TIMEOUT_SECONDS
    while (
        count_static_deposit_events(context, static_deposit_address_id=static_deposit_address_id, event_type=event_type)
        < count
    ):
        if time.monotonic() > deadline:
            raise FlowFailedError(
                f"{static_deposit_address_id}: not {count} {event_type} in {SCENARIO_TIMEOUT_SECONDS} s"
            )
        time.sleep(2)


def pay_usdc(context: FlowContext, *, payer: str, deposit_address: str, base_units: int) -> str:
    transaction_hash = send_transaction(context.http_client, sender=payer, to=USDC_ADDRESS,
                                        data=build_erc20_transfer_data(deposit_address, base_units))  # fmt: skip
    wait_for_receipt(context.http_client, transaction_hash)
    return transaction_hash


def capture_static_deposit_address_docs_requests(
    context: FlowContext, *, bearer_headers: dict[str, str], payer_address: str
) -> dict[str, object]:
    """The docs' customer address requests as their examples send them: create, read once a 5 USDC payment is
    credited, list by prefix. The payer holds the USDC."""
    create_reply = context.http_client.post(
        f"{API_URL}/static-deposit-addresses",
        json={"customer_reference": DOCUMENTED_CUSTOMER_REFERENCE, "chain": FORK_CHAIN_KEY, "assets": ["USDC"]},
        headers=bearer_headers,
    )  # fmt: skip
    check(create_reply.status_code == 201, f"docs static create: {create_reply.status_code} {create_reply.text[:200]}")
    static_deposit_address = create_reply.json()
    pay_usdc(context, payer=payer_address, deposit_address=str(static_deposit_address["deposit_address"]),
             base_units=5 * USDC_BASE_UNITS)  # fmt: skip
    credited = wait_for_static_deposit_address(
        context, static_deposit_address_id=str(static_deposit_address["id"]), bearer_headers=bearer_headers, description="docs credited payment",
        condition=lambda address: address["payment_count"] == 1,
    )  # fmt: skip
    list_reply = context.http_client.get(
        f"{API_URL}/static-deposit-addresses", params={"customer_reference_prefix": "customer-"},
        headers=bearer_headers,
    )  # fmt: skip
    check(list_reply.status_code == 200, f"docs static list: {list_reply.status_code} {list_reply.text[:200]}")
    return {
        "create_static_deposit_address": static_deposit_address,
        "read_static_deposit_address": credited,
        "list_static_deposit_addresses": list_reply.json(),
    }


def sign_up_with_token_and_webhook(*, payout_address: str) -> tuple[str, str]:
    """A fresh account, its API token and its webhook (pointing at the receiver): (token, webhook secret)."""
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
            return api_token, set_webhook_in_settings(page)
        finally:
            browser.close()


def create_customer_address_twice(
    context: FlowContext, *, bearer_headers: dict[str, str], customer_reference: str, payout_address: str
) -> dict[str, object]:
    """The same address both times, listed by its prefix, its salt the HMAC; returns the address."""
    creation_fields = {"customer_reference": customer_reference, "chain": FORK_CHAIN_KEY, "assets": ["USDC"]}
    first_reply = context.http_client.post(
        f"{API_URL}/static-deposit-addresses", json=creation_fields, headers=bearer_headers
    )
    check(first_reply.status_code == 201, f"static address create: {first_reply.status_code} {first_reply.text}")
    static_deposit_address = first_reply.json()
    static_deposit_address_id = check_static_deposit_address_id(str(static_deposit_address["id"]))
    repeat_reply = context.http_client.post(
        f"{API_URL}/static-deposit-addresses", json=creation_fields, headers=bearer_headers
    )
    check(repeat_reply.status_code == 201 and repeat_reply.json()["id"] == static_deposit_address_id
          and repeat_reply.json()["deposit_address"] == static_deposit_address["deposit_address"],
          f"repeated create: {repeat_reply.status_code} {repeat_reply.text[:200]}, expected the same address")  # fmt: skip
    check(static_deposit_address["destination_address"] == payout_address
          and static_deposit_address["assets"] == ["USDC"] and static_deposit_address["payments"] == [],
          f"created: {static_deposit_address}")  # fmt: skip
    listed = context.http_client.get(f"{API_URL}/static-deposit-addresses",
                                     params={"customer_reference_prefix": customer_reference[:12]},
                                     headers=bearer_headers).json()  # fmt: skip
    check([row["id"] for row in listed["static_deposit_addresses"]] == [static_deposit_address_id],
          f"prefix list: {listed}")  # fmt: skip
    account_id = int(run_sql(context, "SELECT account_id FROM static_deposit_addresses "  # noqa: S608
                                      f"WHERE id = '{static_deposit_address_id}'"))  # fmt: skip
    stored_salt_hex = run_sql(context, "SELECT encode(deposit_salt, 'hex') FROM static_deposit_addresses "  # noqa: S608
                                       f"WHERE id = '{static_deposit_address_id}'")  # fmt: skip
    check(stored_salt_hex == derive_static_deposit_salt_hex(
        read_environment_setting(context, "PAY_INVOICE_SALT_SEED"), account_id=account_id,
        chain_key=FORK_CHAIN_KEY, customer_reference=customer_reference),
        "the stored salt is not the domain-separated HMAC of (account, chain, customer reference)")  # fmt: skip
    return static_deposit_address


def check_payment_webhook_and_token_events(
    context: FlowContext,
    *,
    webhook_secret: str,
    bearer_headers: dict[str, str],
    static_deposit_address_id: str,
    payment_hash: str,
) -> None:
    """``static_address.payment_received`` signed at the receiver, and in the token's ``GET /events``. Only this
    address's records count: an earlier scenario's retried delivery can land on this receiver too."""

    def select_this_address_payment_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
        return [record for record in select_webhook_records(records, event_type="static_address.payment_received")
                if json.loads(str(record["body"]))["data"]["static_deposit_address"]["id"]
                == static_deposit_address_id]  # fmt: skip

    records = wait_for_webhook_records(context, description="static_address.payment_received delivered",
        condition=lambda records: [record["answered_status"]
                                   for record in select_this_address_payment_records(records)] == [204])  # fmt: skip
    received_record = select_this_address_payment_records(records)[0]
    Webhook(webhook_secret).verify(str(received_record["body"]), dict(received_record["headers"]))  # type: ignore[call-overload]
    webhook_data = json.loads(str(received_record["body"]))["data"]
    check(webhook_data["static_deposit_address"]["id"] == static_deposit_address_id
          and webhook_data["payment"]["amount_usd"] == "5.00"
          and str(webhook_data["payment"]["tx_hash"]).lower() == payment_hash.lower(),
          f"static_address.payment_received data: {webhook_data}")  # fmt: skip
    events_reply = context.http_client.get(f"{API_URL}/events", params={"limit": 200}, headers=bearer_headers)
    check(events_reply.status_code == 200, f"token events: {events_reply.status_code} {events_reply.text[:200]}")
    static_events = [
        event for event in events_reply.json()["events"]
        if event["invoice_id"] is None and str(event["type"]).startswith("static_address.")
    ]  # fmt: skip
    check(any(event["type"] == "static_address.payment_received"
              and event["data"]["static_deposit_address"]["id"] == static_deposit_address_id  # type: ignore[index]
              for event in static_events),
          f"token events: {[event['type'] for event in static_events]}")  # fmt: skip


def move_to_cold_tier(context: FlowContext, *, static_deposit_address_id: str) -> None:
    """Made and last paid over a day ago: only the cold pass reads it now."""
    checked_id = check_static_deposit_address_id(static_deposit_address_id)
    age_seconds = int(COLD_TIER_AGE.total_seconds())
    run_sql(context, f"UPDATE static_deposit_addresses SET created_at = now() - interval '{age_seconds} seconds', "  # noqa: S608
                     f"last_received_at = now() - interval '{age_seconds} seconds' WHERE id = '{checked_id}'")  # fmt: skip


def read_last_received_at(context: FlowContext, *, static_deposit_address_id: str) -> datetime:
    checked_id = check_static_deposit_address_id(static_deposit_address_id)
    epoch_text = run_sql(context, "SELECT extract(epoch FROM last_received_at) FROM static_deposit_addresses "  # noqa: S608
                                  f"WHERE id = '{checked_id}'")  # fmt: skip
    return datetime.fromtimestamp(float(epoch_text), tz=UTC)


def scenario_static_deposit_address(context: FlowContext) -> dict[str, object]:
    """The module docstring's steps, on a fresh account."""
    payout_address = Account.create().address
    payer = STATIC_DEPOSIT_SCENARIO_PAYER.address
    customer_reference = f"customer-77-{secrets.token_hex(4)}"
    start_webhook_receiver_container(context, receiver_environment={})
    try:
        api_token, webhook_secret = sign_up_with_token_and_webhook(payout_address=payout_address)
        bearer_headers = {"Authorization": f"Bearer {api_token}"}
        static_deposit_address = create_customer_address_twice(
            context, bearer_headers=bearer_headers, customer_reference=customer_reference, payout_address=payout_address
        )
        static_deposit_address_id = str(static_deposit_address["id"])
        deposit_address = str(static_deposit_address["deposit_address"])
        first_payment_base_units = 5 * USDC_BASE_UNITS
        second_payment_base_units = 2 * USDC_BASE_UNITS
        fund_with_usdc(context.http_client, payer, base_units=first_payment_base_units + second_payment_base_units)
        first_payment_hash = pay_usdc(context, payer=payer, deposit_address=deposit_address,
                                      base_units=first_payment_base_units)  # fmt: skip
        credited = wait_for_static_deposit_address(
            context, static_deposit_address_id=static_deposit_address_id, bearer_headers=bearer_headers, description="credited payment",
            condition=lambda address: address["payment_count"] == 1)  # fmt: skip
        payment = credited["payments"][0]  # type: ignore[index]
        check(payment["credited"] is True and payment["amount"] == "5" and payment["amount_usd"] == "5.00"
              and payment["asset_symbol"] == "USDC" and str(payment["tx_hash"]).lower() == first_payment_hash.lower()
              and payment["sender"] == payer and credited["received_usd"] == "5.00",
              f"credited payment: {payment}, received_usd {credited['received_usd']}")  # fmt: skip
        wait_for_usdc_balance(
            context, address=payout_address, base_units=first_payment_base_units, description="first sweep"
        )
        wait_for_static_deposit_events(
            context, static_deposit_address_id=static_deposit_address_id, event_type="static_address.swept", count=1
        )
        check_payment_webhook_and_token_events(
            context, webhook_secret=webhook_secret, bearer_headers=bearer_headers,
            static_deposit_address_id=static_deposit_address_id, payment_hash=first_payment_hash,
        )  # fmt: skip
        move_to_cold_tier(context, static_deposit_address_id=static_deposit_address_id)
        check(datetime.now(UTC) - read_last_received_at(context, static_deposit_address_id=static_deposit_address_id) > COLD_TIER_AGE / 2,
              "the address did not move to the cold tier")  # fmt: skip
        cold_payment_hash = pay_usdc(context, payer=payer, deposit_address=deposit_address,
                                     base_units=second_payment_base_units)  # fmt: skip
        after_cold_payment = wait_for_static_deposit_address(
            context, static_deposit_address_id=static_deposit_address_id, bearer_headers=bearer_headers, description="cold tier payment",
            condition=lambda address: address["payment_count"] == 2)  # fmt: skip
        cold_payment = after_cold_payment["payments"][0]  # type: ignore[index]
        check(cold_payment["credited"] is True and cold_payment["amount_usd"] == "2.00"
              and str(cold_payment["tx_hash"]).lower() == cold_payment_hash.lower()
              and after_cold_payment["received_usd"] == "7.00",
              f"cold tier payment: {cold_payment}, received_usd {after_cold_payment['received_usd']}")  # fmt: skip
        last_received_age = datetime.now(UTC) - read_last_received_at(
            context, static_deposit_address_id=static_deposit_address_id
        )
        check(last_received_age < RECENT_RECEIPT_AGE,
              f"last_received_at is {last_received_age} old after a payment: the address is not hot again")  # fmt: skip
        wait_for_usdc_balance(context, address=payout_address, base_units=first_payment_base_units + second_payment_base_units,
                              description="sweep of the cold tier payment")  # fmt: skip
        wait_for_static_deposit_events(
            context, static_deposit_address_id=static_deposit_address_id, event_type="static_address.swept", count=2
        )
        payment_received_events = count_static_deposit_events(
            context, static_deposit_address_id=static_deposit_address_id, event_type="static_address.payment_received"
        )
        check(payment_received_events == 2, f"{payment_received_events} payment_received events, expected 2")
        records = read_webhook_receiver_records()
        return {"static_deposit_address_id": static_deposit_address_id, "deposit_address": deposit_address,
                "first_payment_tx_hash": first_payment_hash, "cold_tier_payment_tx_hash": cold_payment_hash,
                "last_received_age_seconds": round(last_received_age.total_seconds()),
                "destination_usdc_base_units": read_usdc_balance(context.http_client, payout_address),
                "received": [(read_webhook_record_type(record), record["answered_status"]) for record in records]}  # fmt: skip
    finally:
        stop_webhook_receiver()


STATIC_DEPOSIT_ADDRESS_SCENARIOS = [scenario_static_deposit_address]
