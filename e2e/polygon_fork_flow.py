"""End-to-end run on an anvil fork of Polygon mainnet (PLAN.md section 12). Run by hand when a change needs it:

    uv run --project backend python e2e/polygon_fork_flow.py [--scratch-folder DIR] [--keep-stack] [--scenario NAME]

The stack (anvil fork, factory, migrate, api, watcher, a signed-up account) comes from e2e/fork_stack.py. The
scenarios drive the API and the chain directly and run one after another. Each has its own fresh destination, except
those with refunds: those pay the operator's own address, the only receiver the float refunds for (PLAN.md section 6):

0. The operator's safety net, first while the stack is clean: `reconcile` exits 0; a sweep that reverts leaves a
   paid deposit unswept, `reconcile` exits 1 naming it and one `operator.alert` lands on the operator's account;
   swept by hand it is clean again; a frozen watcher's container turns unhealthy, then healthy once thawed.
1. USDC invoice paid exactly.
2. POL invoice priced in EUR, underpaid, then topped up.
3. POL invoice overpaid: the excess goes back to the sender by itself once final (from the operator float).
4. An invoice left to expire, paid late, swept by the rescan.
5. A third party sweeping a deposit before the watcher does.
6. A payment seen at ``latest`` and reorged out before finality (an abandoned transaction id): nothing is credited,
   then a new payment is credited once.
7. A refund attempt whose nonce another operator transaction takes (the attempt is abandoned): the refund waits for
   the owner as ``needs_owner`` with its amount reserved, the owner's ``refunds mark-unpaid --nonce-taken-by`` (the
   transaction that took the nonce; refused without it or with the attempt's own hash) frees it, no automatic
   second refund follows, and the payer's signed request pays it.
8. A refund larger than the operator float waits as ``waiting_for_float`` while a later sweep still goes out; topped
   up, the float pays it.
9. A refund to an EIP-7702 account (receiving runs code, so 21,000 gas would revert).
10. No operator key can pay a sweep's gas (the sweepers and the refund key): the sweep waits, then goes out once one
    has gas.
11. Underpaid: no refund inside the payment window; after it a signed request pays it back (replay and wrong signer
    refused).
12. One address pays in three inputs until past the amount; the rest goes back by itself once final.
13. Underpaid, swept by a third party, then accept-partial: no sweep of an empty deposit; it closes as paid.
14. A payment mined while its quote is live but final only after it lapsed: credited at the quote, paid.
15. A USDC payment seen at ``latest``, re-mined by a reorg behind another transfer at a new log index: one final
    row at its canonical block and log index, credited once.
16. The POL price feed reverts while wrong-coin money and an overpay go final: no refund, a deferral logged, the
    overpaid invoice swept and closed; once the feed answers again each goes back by itself, exactly once.
17. Wrong-coin POL sent while the watcher is stopped, 300 blocks before it comes back (past its native block scan):
    a final row with no sender, `owner_settles`, the event recorded and nothing sent; the operator's
    `record-stray-payment-from-transaction` then attributes it and it goes back by itself, exactly once.
18-20. An operation's outcome and its invoice effect commit together: the watcher, made to fail between the two
    (`PAY_E2E_FAIL_AFTER_OPERATION_RESOLVE`, read on `local_fork` only), leaves a refund `mined` (going `final`), a
    refund `broadcast` (going `needs_owner`) and a sweep `pending` (failing) with no event; restarted without it, each
    resolves and `invoice.refunded`, `refund.needs_owner`, `invoice.sweep_failed` (with `sweep_blocked`) land once.
21. A deposit whose invoice row is lost before the sweep (Postgres gone, the host alive): the stored salt is the
    HMAC of the id under the stack's `PAY_INVOICE_SALT_SEED`, the API's `invoice_create` line names the deposit and
    destination (never the salt or seed), `recover-deposit --from-log -` fed that line (with an unreachable database)
    predicts the same address and shows its USDC and POL, and its printed sweep call, sent by a third party, moves
    both to the destination (PLAN.md section 3, 2026-10-12).
22. API tokens and signed webhooks (PLAN.md section 8.3, 2026-10-12): a fresh account makes a token and sets a
    webhook (a receiver container on the stack's network) on /settings and sends a test event; the token creates a
    USDC invoice with an order_id and metadata (a repeat returns the same invoice), finds it by ``?order_id=`` and is
    refused on a cookie-only route; paid on the fork, ``invoice.paid`` arrives signed (Standard Webhooks), answered
    500 once and delivered again under the same webhook-id; revoked, the token gets 401.
23. Discord messages (PLAN.md section 8.4, 2026-10-13): a fresh account sets a Discord webhook on /settings (the
    receiver container stands in for discord.com, `PAY_E2E_DISCORD_API_BASE_URL`, read on `local_fork` only) and
    sends a test message; its paid invoice's "Paid" line arrives with no mentions parsed and the note's markdown
    escaped, answered 429 once and sent again after `retry_after`; a webhook Discord answers 404 Unknown Webhook for
    is marked deleted, /settings says so, and nothing more is sent to it. The operator's account, whose passkey went
    with the browser that signed it up, gets one through `mint-passkey-enrollment-link` and sets its own Discord
    webhook; `record-operator-alert --kind backup_failed` then lands there as a "Needs the operator" line, escaped, no
    mentions parsed (2026-10-13). On /settings it then adds a passkey and deletes the enrolled one, each after a
    passkey check of the account, and both land as account events; the session-only `DELETE` of a passkey is gone.
24. A frozen token (USDC blacklists the destination) reverts a USDC deposit's sweep, POL included: the invoice is
    ``sweep_blocked``; ``sweep-invoice --skip-token USDC`` moves the POL and leaves the USDC (the native coin cannot be
    skipped, an unknown symbol is refused), the next sweep listing USDC blocks it again, and once unblacklisted a plain
    ``sweep-invoice`` moves the USDC (2026-10-13).
25. An expired order refreshed (``refresh_expired``, 2026-10-21): a new invoice with a new deposit, paid; money sent
    to the old deposit afterwards is late money on the old invoice, swept, and the new one does not move; an order
    whose expired invoice got late money is refused.
26. API token merchant routes and a per-invoice callback (2026-10-21): cancel, accept-partial, quantity links and
    the invoice events by token; ``callback_url`` refused without an account webhook, then ``invoice.paid`` delivered
    there signed with the webhook secret; ``return_url`` and ``success_url`` in the answers.
27-31. Operator keys and the treasury Safe (2026-10-13, ``operator_key_scenarios.py``): sweeps paid in one block go
    to different sweepers; a treasury Safe made by ``treasury create-safe`` and set up by ``treasury execute-setup``
    tops a drained refund key back to its target; a used-up allowance pulls nothing and alerts once; a sweeper used
    outside the gateway is frozen, alerted and skipped, then unfrozen; a retired sweeper returns what it holds to the
    Safe and gets no more sweeps.
30. Merchant operations (2026-10-21, ``merchant_operations_scenarios.py``): ``GET /rates``; a fresh account's paid
    invoice in the CSV export by token and by session; a sample of every webhook event type, signed and marked as a
    test; the developer docs' example responses captured, anonymised and compared with the committed file.
31. Static deposit addresses (PLAN.md section 17, 2026-10-21, ``static_deposit_address_scenarios.py``): an API
    token creates a customer's address twice and gets the same one, its salt the domain-separated HMAC; a USDC payment
    is credited once at 5.00 USD, swept to the payout address, signed at the webhook and listed by the token's
    ``GET /events``; moved to the cold scan tier, a second payment is still credited and puts the address back in the
    hot tier.
32. Invoice schedules (PLAN.md section 18, 2026-10-21, ``invoice_schedule_scenarios.py``): a daily schedule moved due
    in SQL bills each period exactly once (a rewound schedule included), ``schedule.invoice_created`` reaches the
    webhook, a scheduled invoice is paid, pause skips a period, resume bills from the next, cancel ends it; a
    one-invoice schedule completes; the schedule routes' example responses for the docs.

Payers are fresh local keys, the refund key is anvil's dev account #0 and the three sweeper keys are fresh local keys;
nothing is sent to a real network. The log
goes to the scratch folder; the stack is removed at the end unless --keep-stack.
"""

import argparse
from collections.abc import Callable
from decimal import Decimal
import hmac
import json
from pathlib import Path
import re
import secrets
import sys
import tempfile
import time

from account_passkey_session import (
    VIRTUAL_AUTHENTICATOR_OPTIONS,
    account_request_headers,
    add_virtual_authenticator,
    sign_up_account_on_page,
)
from account_settings_helpers import (
    PASSKEY_STEP_TIMEOUT_MILLISECONDS,
    WEBHOOK_RECEIVER_CONTAINER,
    WEBHOOK_RECEIVER_PATH,
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
from eth_abi import encode
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount
from eth_utils import keccak
from fork_stack import (
    API_URL,
    DEV_ADDRESSES,
    DEV_PRIVATE_KEYS,
    FORK_CHAIN_ID,
    FORK_CHAIN_KEY,
    NATIVE_ATTRIBUTION_WINDOW_BLOCKS,
    NATIVE_SYMBOL,
    NATIVE_UNIT_WEI,
    NATIVE_USD_FEED_ADDRESS,
    PUBLIC_BASE_URL,
    SCENARIO_TIMEOUT_SECONDS,
    SWEEPER_ACCOUNTS,
    USDC_ADDRESS,
    USDC_BASE_UNITS,
    FlowContext,
    FlowFailedError,
    build_erc20_transfer_data,
    call_anvil,
    check,
    compose_arguments,
    compose_environment,
    create_invoice,
    create_local_account,
    expire_invoice_now,
    fund_with_usdc,
    last_refund_final,
    last_sweep_final,
    pause_mining,
    read_account_event_types,
    read_environment_setting,
    read_invoice,
    read_native_balance,
    read_public_invoice,
    read_usdc_balance,
    resume_mining,
    run_command,
    run_operator_cli,
    run_scenarios_on_fork_stack,
    run_sql,
    send_transaction,
    sign_local_transaction,
    to_base_units,
    wait_for_account_events,
    wait_for_invoice,
    wait_for_receipt,
)
import httpx
from invoice_schedule_scenarios import INVOICE_SCHEDULE_SCENARIOS
from merchant_operations_scenarios import MERCHANT_OPERATIONS_SCENARIOS
from operator_key_scenarios import OPERATOR_KEY_SCENARIOS
from playwright.sync_api import Browser, CDPSession, Page, Route, expect, sync_playwright
from standardwebhooks import Webhook
from static_deposit_address_scenarios import STATIC_DEPOSIT_ADDRESS_SCENARIOS

HARNESS_ACCOUNTS = [create_local_account() for _ in range(26)]
API_TOKEN_SCENARIO_PAYER = create_local_account()
DISCORD_SCENARIO_PAYER = create_local_account()
FROZEN_TOKEN_SCENARIO_PAYER = create_local_account()
LAPSED_FROZEN_TOKEN_SCENARIO_PAYER = create_local_account()
REFRESH_SCENARIO_PAYER = create_local_account()
MERCHANT_ROUTES_SCENARIO_PAYER = create_local_account()


def scenario_usdc_paid_exactly(context: FlowContext) -> dict[str, object]:
    destination = Account.create().address
    payer = HARNESS_ACCOUNTS[0].address
    invoice = create_invoice(
        context, amount="10", currency="USD", assets=["USDC"], destination_address=destination, note="e2e usdc"
    )
    invoice_id = str(invoice["id"])
    amount_base_units = to_base_units(str(invoice["amount_due"]), decimals=6)
    fund_with_usdc(context.http_client, payer, base_units=100 * USDC_BASE_UNITS)
    transfer_data = "0xa9059cbb" + encode(["address", "uint256"], [invoice["deposit_address"], amount_base_units]).hex()
    wait_for_receipt(
        context.http_client, send_transaction(context.http_client, sender=payer, to=USDC_ADDRESS, data=transfer_data)
    )
    invoice = wait_for_invoice(context, invoice_id, description="final sweep", condition=last_sweep_final)
    destination_balance = read_usdc_balance(context.http_client, destination)
    check(invoice["status"] == "paid", f"status {invoice['status']}, expected paid")
    check(
        destination_balance == amount_base_units,
        f"destination holds {destination_balance}, expected {amount_base_units}",
    )
    account_event_types = wait_for_account_events(context, invoice_id, expected_types={"invoice.paid", "invoice.swept"})
    return {"invoice_id": invoice_id, "amount_due": invoice["amount_due"], "fiat": invoice["fiat"],
            "destination_usdc_base_units": destination_balance, "account_events": account_event_types}  # fmt: skip


def scenario_eur_underpaid_then_topped_up(context: FlowContext) -> dict[str, object]:
    destination = Account.create().address
    payer = HARNESS_ACCOUNTS[1].address
    invoice = create_invoice(
        context, amount="2", currency="EUR", assets=[NATIVE_SYMBOL], destination_address=destination, note="e2e eur"
    )
    invoice_id = str(invoice["id"])
    amount_due_wei = to_base_units(str(invoice["amount_due"]), decimals=18)
    first_payment_wei = amount_due_wei // 2
    wait_for_receipt(
        context.http_client,
        send_transaction(
            context.http_client, sender=payer, to=str(invoice["deposit_address"]), value_wei=first_payment_wei
        ),
    )
    invoice = wait_for_invoice(
        context, invoice_id, description="underpaid", condition=lambda current: current["status"] == "underpaid"
    )
    remaining_wei = to_base_units(str(invoice["amount_remaining"]), decimals=18)
    wait_for_receipt(
        context.http_client,
        send_transaction(
            context.http_client, sender=payer, to=str(invoice["deposit_address"]), value_wei=remaining_wei
        ),
    )
    invoice = wait_for_invoice(context, invoice_id, description="final sweep", condition=last_sweep_final)
    destination_balance = read_native_balance(context.http_client, destination)
    check(invoice["status"] == "paid", f"status {invoice['status']}, expected paid")
    check(destination_balance == first_payment_wei + remaining_wei, f"destination holds {destination_balance} wei")
    account_event_types = wait_for_account_events(
        context, invoice_id, expected_types={"invoice.underpaid", "invoice.paid", "invoice.swept"}
    )
    return {"invoice_id": invoice_id, "amount_due": invoice["amount_due"], "fiat": invoice["fiat"],
            "payments_wei": [first_payment_wei, remaining_wei], "destination_wei": destination_balance,
            "account_events": account_event_types}  # fmt: skip


def scenario_overpaid_then_refunded(context: FlowContext) -> dict[str, object]:
    """Overpaid: once final, the excess goes back to its sender by itself once
    final), and the whole deposit goes to the destination (the operator's own: the only one refunds are for)."""
    destination = context.operator_destination_address
    destination_balance_before = read_native_balance(context.http_client, destination)
    payer = HARNESS_ACCOUNTS[2].address
    invoice = create_invoice(
        context,
        amount="2",
        currency=NATIVE_SYMBOL,
        assets=[NATIVE_SYMBOL],
        destination_address=destination,
        note="e2e overpaid",
    )
    invoice_id = str(invoice["id"])
    paid_wei = 40 * NATIVE_UNIT_WEI
    wait_for_receipt(
        context.http_client,
        send_transaction(context.http_client, sender=payer, to=str(invoice["deposit_address"]), value_wei=paid_wei),
    )
    payer_balance_before_refund = read_native_balance(context.http_client, payer)
    invoice = wait_for_invoice(
        context, invoice_id, description="final sweep and refund",
        condition=lambda current: last_sweep_final(current) and last_refund_final(current),
    )  # fmt: skip
    check(invoice["status"] == "overpaid", f"status {invoice['status']}, expected overpaid")
    refunded_wei = read_native_balance(context.http_client, payer) - payer_balance_before_refund
    expected_refund_wei = paid_wei - 2 * NATIVE_UNIT_WEI
    destination_balance = read_native_balance(context.http_client, destination) - destination_balance_before
    check(refunded_wei == expected_refund_wei, f"payer got {refunded_wei} wei back, expected {expected_refund_wei}")
    check(destination_balance == paid_wei, f"destination got {destination_balance} wei, expected {paid_wei}")
    check(len(invoice["refunds"]) == 1, f"refunds {invoice['refunds']}")
    check(invoice["refund_offer"]["reason"] == "already_refunded", f"refund offer {invoice['refund_offer']}")
    second_offer_reply = context.http_client.post(f"{API_URL}/public/invoices/{invoice_id}/refund-offer")
    check(
        second_offer_reply.status_code >= 400, f"a refund-offer after the refund got {second_offer_reply.status_code}"
    )
    account_event_types = wait_for_account_events(
        context, invoice_id, expected_types={"invoice.overpaid", "invoice.swept", "invoice.refunded"}
    )
    return {"invoice_id": invoice_id, "refunded_wei": refunded_wei, "destination_wei": destination_balance,
            "refunds": invoice["refunds"], "account_events": account_event_types}  # fmt: skip


def scenario_expired_then_paid_late(context: FlowContext) -> dict[str, object]:
    destination = Account.create().address
    payer = HARNESS_ACCOUNTS[3].address
    invoice = create_invoice(
        context, amount="1", currency=NATIVE_SYMBOL, assets=[NATIVE_SYMBOL], destination_address=destination,
        note="e2e late", lifetime_seconds=300,
    )  # fmt: skip
    invoice_id = str(invoice["id"])
    # Five minutes is the shortest lifetime; move the deadline instead of waiting it out.
    run_sql(context, f"update invoices set expires_at = now() - interval '1 second' where id = '{invoice_id}'")  # noqa: S608
    wait_for_invoice(
        context, invoice_id, description="expired", condition=lambda current: current["status"] == "expired"
    )
    paid_wei = NATIVE_UNIT_WEI
    wait_for_receipt(
        context.http_client,
        send_transaction(context.http_client, sender=payer, to=str(invoice["deposit_address"]), value_wei=paid_wei),
    )
    invoice = wait_for_invoice(
        context, invoice_id, description="late payment swept",
        condition=lambda current: bool(current["late_payment"]) and last_sweep_final(current),
    )  # fmt: skip
    destination_balance = read_native_balance(context.http_client, destination)
    check(destination_balance == paid_wei, f"destination holds {destination_balance} wei, expected {paid_wei}")
    account_event_types = wait_for_account_events(
        context, invoice_id, expected_types={"invoice.expired", "invoice.late_payment", "invoice.swept"}
    )
    return {"invoice_id": invoice_id, "status": invoice["status"], "late_payment": invoice["late_payment"],
            "destination_wei": destination_balance, "account_events": account_event_types}  # fmt: skip


def scenario_third_party_sweep(context: FlowContext) -> dict[str, object]:
    destination = Account.create().address
    payer = HARNESS_ACCOUNTS[4].address
    third_party = HARNESS_ACCOUNTS[5].address
    invoice = create_invoice(
        context,
        amount="1",
        currency=NATIVE_SYMBOL,
        assets=[NATIVE_SYMBOL],
        destination_address=destination,
        note="e2e third party",
    )
    invoice_id = str(invoice["id"])
    invoice_salt = bytes.fromhex(
        run_sql(context, f"select encode(invoice_salt, 'hex') from invoices where id = '{invoice_id}'")
    )
    sweep_data = "0x" + keccak(text="sweepToDestination(bytes32,address,address[])")[:4].hex()
    sweep_data += encode(["bytes32", "address", "address[]"], [invoice_salt, destination, []]).hex()
    paid_wei = NATIVE_UNIT_WEI
    # Payment and sweep land in one block, before the watcher can see the payment final.
    call_anvil(context.http_client, "evm_setIntervalMining", 0)
    try:
        payment_hash = send_transaction(
            context.http_client, sender=payer, to=str(invoice["deposit_address"]), value_wei=paid_wei, gas=50_000
        )
        sweep_hash = send_transaction(
            context.http_client, sender=third_party, to=context.factory_address, data=sweep_data, gas=400_000
        )
        call_anvil(context.http_client, "evm_mine")
    finally:
        call_anvil(context.http_client, "evm_setIntervalMining", 1)
    wait_for_receipt(context.http_client, payment_hash)
    wait_for_receipt(context.http_client, sweep_hash)
    invoice = wait_for_invoice(
        context, invoice_id, description="paid", condition=lambda current: current["status"] == "paid"
    )
    # Give the watcher a few ticks to (not) sweep an empty deposit.
    time.sleep(8)
    invoice = read_invoice(context, invoice_id)
    destination_balance = read_native_balance(context.http_client, destination)
    check(destination_balance == paid_wei, f"destination holds {destination_balance} wei, expected {paid_wei}")
    check(invoice["amount_received"] == "1", f"amount_received {invoice['amount_received']}, expected 1")
    # The sweep hid the rise at `latest`; the payment is still attributed once final (2026-10-09).
    payment_hashes = [payment["tx_hash"] for payment in invoice["payments"]]
    check(payment_hashes == [payment_hash], f"payments {invoice['payments']}, expected {payment_hash}")
    account_event_types = wait_for_account_events(context, invoice_id, expected_types={"invoice.paid"})
    return {"invoice_id": invoice_id, "status": invoice["status"], "sweeps": invoice["sweeps"],
            "destination_wei": destination_balance, "account_events": account_event_types}  # fmt: skip


def request_signed_refund(context: FlowContext, invoice_id: str, *, payer: LocalAccount) -> httpx.Response:
    """Ask for an offer, sign the payer's with ``personal_sign`` and send the request. Another caller asking for an
    offer meanwhile gets the same nonce back: it cannot void the one the payer is signing."""
    offer_reply = context.http_client.post(f"{API_URL}/public/invoices/{invoice_id}/refund-offer")
    check(offer_reply.status_code == 200, f"refund-offer: {offer_reply.status_code} {offer_reply.text}")
    offer = next(item for item in offer_reply.json()["offers"] if item["sender"] == payer.address)
    second_reply = context.http_client.post(f"{API_URL}/public/invoices/{invoice_id}/refund-offer")
    second_offer = next(item for item in second_reply.json()["offers"] if item["sender"] == payer.address)
    check(second_offer["nonce"] == offer["nonce"], "a second refund-offer call replaced the payer's nonce")
    signature = Account.sign_message(encode_defunct(text=offer["message"]), private_key=payer.key).signature
    return context.http_client.post(
        f"{API_URL}/public/invoices/{invoice_id}/refund-requests",
        json={"nonce": offer["nonce"], "signature": "0x" + signature.hex().removeprefix("0x")},
    )


def scenario_payment_reorged_out_before_finality(context: FlowContext) -> dict[str, object]:
    payer = HARNESS_ACCOUNTS[6].address
    invoice = create_invoice(
        context, amount="2", currency=NATIVE_SYMBOL, assets=[NATIVE_SYMBOL], destination_address=Account.create().address,
        note="e2e reorged payment",
    )  # fmt: skip
    invoice_id = str(invoice["id"])
    deposit_address = str(invoice["deposit_address"])
    pause_mining(context.http_client)
    try:
        abandoned_hash = send_transaction(
            context.http_client, sender=payer, to=deposit_address, value_wei=2 * NATIVE_UNIT_WEI
        )
        # One block: `latest` holds the payment, `finalized` (two behind on the fork) does not.
        call_anvil(context.http_client, "evm_mine")
        wait_for_invoice(
            context, invoice_id, description="confirming", condition=lambda current: current["status"] == "confirming"
        )
        call_anvil(context.http_client, "anvil_reorg", 1, [])
        check(
            call_anvil(context.http_client, "eth_getTransactionReceipt", abandoned_hash) is None,
            "the reorged-out payment still has a receipt",
        )
    finally:
        resume_mining(context.http_client)
    wait_for_invoice(
        context,
        invoice_id,
        description="awaiting_payment again",
        condition=lambda current: current["status"] == "awaiting_payment",
    )
    # Past the replaced block's finality: still nothing credited, no payment row kept.
    time.sleep(5)
    invoice = read_invoice(context, invoice_id)
    check(
        invoice["status"] == "awaiting_payment"
        and invoice["amount_received"] in {"0", None}
        and not invoice["payments"],
        f"after the reorg: {invoice['status']}, received {invoice['amount_received']}, payments {invoice['payments']}",
    )
    payment_hash = send_transaction(
        context.http_client, sender=payer, to=deposit_address, value_wei=2 * NATIVE_UNIT_WEI
    )
    wait_for_receipt(context.http_client, payment_hash)
    invoice = wait_for_invoice(context, invoice_id, description="final sweep", condition=last_sweep_final)
    payments = invoice["payments"]
    check(invoice["status"] == "paid", f"status {invoice['status']}, expected paid")
    check(
        isinstance(payments, list) and [payment["tx_hash"] for payment in payments] == [payment_hash],
        f"payments {payments}, expected only {payment_hash}",
    )
    account_event_types = wait_for_account_events(
        context, invoice_id, expected_types={"invoice.payment_detected", "invoice.paid", "invoice.swept"}
    )
    check(account_event_types.count("invoice.paid") == 1, f"account events {account_event_types}")
    return {"invoice_id": invoice_id, "abandoned_transaction": abandoned_hash, "payment": payment_hash,
            "account_events": account_event_types}  # fmt: skip


def abandon_automatic_refund_attempt(
    context: FlowContext, *, payer: LocalAccount, note: str
) -> tuple[str, str, str, str]:
    """An overpaid invoice whose automatic refund's only attempt loses its nonce to another operator transaction:
    (invoice id, refund operation id, the abandoned attempt's hash, the nonce-taking transaction's hash). The watcher
    ends it as ``needs_owner`` ``NONCE_TAKEN_ELSEWHERE_AFTER`` (5 min) after the attempt."""
    operator = Account.from_key(DEV_PRIVATE_KEYS[0])
    operator_balance = read_native_balance(context.http_client, operator.address)
    # A float too small for the refund holds it as waiting_for_float until the race is set up.
    call_anvil(context.http_client, "anvil_setBalance", operator.address, hex(2 * NATIVE_UNIT_WEI))
    invoice = create_invoice(
        context, amount="2", currency=NATIVE_SYMBOL, assets=[NATIVE_SYMBOL], destination_address=context.operator_destination_address,
        note=note,
    )  # fmt: skip
    invoice_id = str(invoice["id"])
    try:
        wait_for_receipt(
            context.http_client,
            send_transaction(context.http_client, sender=payer.address, to=str(invoice["deposit_address"]),
                             value_wei=40 * NATIVE_UNIT_WEI),
        )  # fmt: skip
        wait_for_invoice(
            context, invoice_id, description="swept, refund waiting for float",
            condition=lambda current: last_sweep_final(current)
            and [refund["status"] for refund in current["refunds"]] == ["waiting_for_float"],
        )  # fmt: skip
        pause_mining(context.http_client)
    finally:
        call_anvil(context.http_client, "anvil_setBalance", operator.address, hex(operator_balance))
    try:
        deadline = time.monotonic() + 30
        attempt_row = ""
        while not attempt_row:
            check(time.monotonic() < deadline, "the watcher signed no refund attempt in 30 s")
            time.sleep(0.5)
            attempt_row = run_sql(
                context,
                "SELECT o.id, o.nonce, a.transaction_hash FROM outgoing_operations o JOIN outgoing_attempts a ON "
                f"a.operation_id = o.id WHERE o.invoice_id = '{invoice_id}' AND o.kind = 'refund' ORDER BY a.id LIMIT 1",
            )
        operation_id, nonce_text, attempt_hash = attempt_row.split("|")
        # The operator key used elsewhere at the same nonce, with fees no bump reaches: the attempt is abandoned. The
        # same nonce, so the key's nonce never passes what the allocator handed out and the key is not frozen.
        latest_block = call_anvil(context.http_client, "eth_getBlockByNumber", "latest", False)
        base_fee = int(latest_block["baseFeePerGas"], 16) if isinstance(latest_block, dict) else 0
        tip = 2_000 * 10**9
        taking = operator.sign_transaction({
            "type": 2, "chainId": FORK_CHAIN_ID, "nonce": int(nonce_text), "to": Account.create().address, "value": 0,
            "gas": 21_000, "maxPriorityFeePerGas": tip, "maxFeePerGas": 4 * base_fee + tip,
        })  # fmt: skip
        taking_hash = str(
            call_anvil(
                context.http_client, "eth_sendRawTransaction", "0x" + taking.raw_transaction.hex().removeprefix("0x")
            )
        )
        call_anvil(context.http_client, "evm_mine")
    finally:
        resume_mining(context.http_client)
    check(wait_for_receipt(context.http_client, taking_hash)["status"] == "0x1", "the nonce-taking transaction failed")
    check(
        call_anvil(context.http_client, "eth_getTransactionReceipt", attempt_hash) is None,
        "the abandoned refund attempt mined",
    )
    return invoice_id, operation_id, attempt_hash, taking_hash


def scenario_refund_attempt_abandoned(context: FlowContext) -> dict[str, object]:
    """The automatic refund's attempt loses its nonce to another operator transaction: ``needs_owner`` with the amount
    reserved and no endless bumps, the operator's ``mark-paid`` refuses the wrong transaction, ``mark-unpaid`` refuses
    no proof and the attempt's own hash and frees it with ``--nonce-taken-by`` the nonce-taking transaction,
    no automatic second refund follows, and the payer's signed request pays it."""
    payer = HARNESS_ACCOUNTS[7]
    invoice_id, operation_id, attempt_hash, taking_hash = abandon_automatic_refund_attempt(
        context, payer=payer, note="e2e abandoned refund attempt"
    )
    # The watcher waits NONCE_TAKEN_ELSEWHERE_AFTER (5 min) from its newest attempt before it decides.
    invoice = wait_for_invoice(
        context,
        invoice_id,
        description="needs_owner",
        condition=lambda current: current["refunds"][-1]["status"] == "needs_owner",
        timeout_seconds=420,
    )
    check(
        not invoice["refund_offer"]["available"],
        f"needs_owner must keep the amount reserved: {invoice['refund_offer']}",
    )
    attempt_count = int(run_sql(context, f"SELECT count(*) FROM outgoing_attempts WHERE operation_id = {operation_id}"))
    check(attempt_count <= 3, f"{attempt_count} attempts: the watcher kept bumping a used nonce")
    wait_for_account_events(context, invoice_id, expected_types={"refund.needs_owner"})
    mark_paid_code, mark_paid_output = run_operator_cli(
        context, "refunds", "mark-paid", operation_id, "--transaction-hash", taking_hash
    )
    check(
        mark_paid_code == 1 and "base units to" in mark_paid_output, "mark-paid accepted a transaction that paid nobody"
    )
    # mark-unpaid needs positive proof that another transaction took the nonce (2026-10-09): no flag, or the
    # abandoned attempt's own hash, is refused; the nonce-taking transaction (finalized by now) is accepted.
    no_proof_code, no_proof_output = run_operator_cli(context, "refunds", "mark-unpaid", operation_id)
    check(
        no_proof_code == 1 and "--nonce-taken-by" in no_proof_output,
        f"mark-unpaid without --nonce-taken-by was not refused: {no_proof_output[-400:]}",
    )
    own_attempt_code, own_attempt_output = run_operator_cli(
        context, "refunds", "mark-unpaid", operation_id, "--nonce-taken-by", attempt_hash
    )
    check(
        own_attempt_code == 1 and "own attempts" in own_attempt_output,
        f"mark-unpaid accepted the abandoned attempt as proof: {own_attempt_output[-400:]}",
    )
    mark_unpaid_code, mark_unpaid_output = run_operator_cli(
        context, "refunds", "mark-unpaid", operation_id, "--nonce-taken-by", taking_hash
    )
    check(mark_unpaid_code == 0 and "is now failed" in mark_unpaid_output, f"mark-unpaid: {mark_unpaid_output[-400:]}")
    time.sleep(6)  # a few ticks: no automatic second refund to a sender that had one
    invoice = read_invoice(context, invoice_id)
    check(len(invoice["refunds"]) == 1, f"an automatic second refund followed mark-unpaid: {invoice['refunds']}")
    check(invoice["refund_offer"]["available"], f"the freed amount is not offered again: {invoice['refund_offer']}")
    payer_balance_before = read_native_balance(context.http_client, payer.address)
    second_request = request_signed_refund(context, invoice_id, payer=payer)
    check(
        second_request.status_code == 200, f"second refund request: {second_request.status_code} {second_request.text}"
    )
    invoice = wait_for_invoice(context, invoice_id, description="final refund", condition=last_refund_final)
    refunded_wei = read_native_balance(context.http_client, payer.address) - payer_balance_before
    check(refunded_wei == 38 * NATIVE_UNIT_WEI, f"payer got {refunded_wei} wei back, expected 38 {NATIVE_SYMBOL}")
    check(invoice["refund_offer"]["reason"] == "already_refunded", f"refund offer {invoice['refund_offer']}")
    wait_for_account_events(context, invoice_id, expected_types={"invoice.refunded"})
    return {"invoice_id": invoice_id, "abandoned_attempt": attempt_hash, "nonce_taken_by": taking_hash,
            "attempts_on_abandoned_operation": attempt_count, "refunded_wei": refunded_wei,
            "refunds": invoice["refunds"], "account_events": read_account_event_types(context, invoice_id)}  # fmt: skip


def scenario_refund_beyond_the_float_waits_while_sweeps_go_on(context: FlowContext) -> dict[str, object]:
    """An automatic refund larger than the operator float waits as ``waiting_for_float`` and a sweep made after it
    still goes out; topped up, the float pays the refund. (Its gas estimate's "insufficient funds" wedged every
    operation, 2026-10-09.)"""
    payer = HARNESS_ACCOUNTS[8]
    operator = DEV_ADDRESSES[0]
    operator_balance = read_native_balance(context.http_client, operator)
    call_anvil(context.http_client, "anvil_setBalance", operator, hex(2 * NATIVE_UNIT_WEI))
    try:
        overpaid = create_invoice(
            context, amount="2", currency=NATIVE_SYMBOL, assets=[NATIVE_SYMBOL],
            destination_address=context.operator_destination_address, note="e2e refund beyond the float",
        )  # fmt: skip
        overpaid_id = str(overpaid["id"])
        wait_for_receipt(
            context.http_client,
            send_transaction(context.http_client, sender=payer.address, to=str(overpaid["deposit_address"]),
                             value_wei=40 * NATIVE_UNIT_WEI),
        )  # fmt: skip
        wait_for_invoice(
            context, overpaid_id, description="swept, refund waiting for float",
            condition=lambda current: last_sweep_final(current)
            and [refund["status"] for refund in current["refunds"]] == ["waiting_for_float"],
        )  # fmt: skip
        later = create_invoice(
            context, amount="2", currency=NATIVE_SYMBOL, assets=[NATIVE_SYMBOL], destination_address=Account.create().address,
            note="e2e sweep after a refund beyond the float",
        )  # fmt: skip
        wait_for_receipt(
            context.http_client,
            send_transaction(context.http_client, sender=payer.address, to=str(later["deposit_address"]),
                             value_wei=2 * NATIVE_UNIT_WEI),
        )  # fmt: skip
        later = wait_for_invoice(context, str(later["id"]), description="the later sweep", condition=last_sweep_final)
        payer_balance_before = read_native_balance(context.http_client, payer.address)
    finally:
        call_anvil(context.http_client, "anvil_setBalance", operator, hex(operator_balance))
    overpaid = wait_for_invoice(context, overpaid_id, description="final refund", condition=last_refund_final)
    refunded_wei = read_native_balance(context.http_client, payer.address) - payer_balance_before
    check(refunded_wei == 38 * NATIVE_UNIT_WEI, f"payer got {refunded_wei} wei back, expected 38 {NATIVE_SYMBOL}")
    wait_for_account_events(context, overpaid_id, expected_types={"refund.waiting_for_float", "invoice.refunded"})
    return {"overpaid_invoice": overpaid_id, "later_invoice": later["id"], "refunds": overpaid["refunds"],
            "account_events": read_account_event_types(context, overpaid_id)}  # fmt: skip


def scenario_refund_to_an_eip7702_account(context: FlowContext) -> dict[str, object]:
    """The payer is an EIP-7702 account (code ``0xef0100`` + delegate): receiving POL runs code, so 21,000 gas reverts.
    The automatic refund must still arrive."""
    payer = HARNESS_ACCOUNTS[9]
    delegate = Account.create().address
    call_anvil(context.http_client, "anvil_setCode", delegate, "0x00")  # STOP: accepts any call with value
    call_anvil(context.http_client, "anvil_setCode", payer.address, "0xef0100" + delegate.removeprefix("0x").lower())
    invoice = create_invoice(
        context, amount="2", currency=NATIVE_SYMBOL, assets=[NATIVE_SYMBOL], destination_address=context.operator_destination_address,
        note="e2e refund to an EIP-7702 account",
    )  # fmt: skip
    invoice_id = str(invoice["id"])
    wait_for_receipt(
        context.http_client,
        send_transaction(context.http_client, sender=payer.address, to=str(invoice["deposit_address"]),
                         value_wei=40 * NATIVE_UNIT_WEI),
    )  # fmt: skip
    payer_balance_before = read_native_balance(context.http_client, payer.address)
    invoice = wait_for_invoice(context, invoice_id, description="final refund", condition=last_refund_final)
    refunded_wei = read_native_balance(context.http_client, payer.address) - payer_balance_before
    check(refunded_wei == 38 * NATIVE_UNIT_WEI, f"payer got {refunded_wei} wei back, expected 38 {NATIVE_SYMBOL}")
    gas_limit = run_sql(
        context, "SELECT a.gas_limit FROM outgoing_attempts a JOIN outgoing_operations o ON o.id = a.operation_id "
        f"WHERE o.invoice_id = '{invoice_id}' AND o.kind = 'refund' ORDER BY a.id DESC LIMIT 1",
    )  # fmt: skip
    return {"invoice_id": invoice_id, "refunds": invoice["refunds"], "refund_gas_limit": gas_limit.strip()}


def scenario_operator_out_of_gas_sweep_waits(context: FlowContext) -> dict[str, object]:
    """The operator cannot pay a sweep's gas: the sweep waits (no failure, no ``sweep_blocked``) and goes out once the
    operator has gas again."""
    payer = HARNESS_ACCOUNTS[10]
    # Every key a sweep may go to: the sweepers, and the refund key they fall back to.
    operator_balances = {
        address: read_native_balance(context.http_client, address)
        for address in [DEV_ADDRESSES[0], *(account.address for account in SWEEPER_ACCOUNTS)]
    }
    for address in operator_balances:
        call_anvil(context.http_client, "anvil_setBalance", address, hex(10**12))
    try:
        invoice = create_invoice(
            context, amount="2", currency=NATIVE_SYMBOL, assets=[NATIVE_SYMBOL], destination_address=Account.create().address,
            note="e2e operator out of gas",
        )  # fmt: skip
        invoice_id = str(invoice["id"])
        wait_for_receipt(
            context.http_client,
            send_transaction(context.http_client, sender=payer.address, to=str(invoice["deposit_address"]),
                             value_wei=2 * NATIVE_UNIT_WEI),
        )  # fmt: skip
        invoice = wait_for_invoice(
            context, invoice_id, description="paid", condition=lambda current: current["status"] == "paid"
        )
        time.sleep(20)  # ten ticks without gas
        invoice = read_invoice(context, invoice_id)
        check(not invoice["sweep_blocked"], "a sweep without gas blocked the invoice")
        # A node's estimate skips the gas balance check, so the attempt is signed and stored ("broadcast"); the
        # send is refused and retried each pass. Nothing is mined and nothing fails.
        check(
            all(
                sweep["status"] in {"pending", "broadcast"} and sweep["tx_hash"] is None for sweep in invoice["sweeps"]
            ),
            f"sweeps without gas: {invoice['sweeps']}",
        )
    finally:
        for address, balance in operator_balances.items():
            call_anvil(context.http_client, "anvil_setBalance", address, hex(balance))
    invoice = wait_for_invoice(context, invoice_id, description="final sweep", condition=last_sweep_final)
    return {"invoice_id": invoice_id, "sweeps": invoice["sweeps"]}


def scenario_underpaid_refund_only_after_the_payment_window(context: FlowContext) -> dict[str, object]:
    """Underpaid: no refund while the payment window is open (top-ups only); after it, the sender's signed request
    pays it back once, a replay and a wrong signer are refused, and the deposit goes to the destination."""
    payer = HARNESS_ACCOUNTS[11]
    destination = context.operator_destination_address
    destination_balance_before = read_native_balance(context.http_client, destination)
    invoice = create_invoice(
        context, amount="40", currency=NATIVE_SYMBOL, assets=[NATIVE_SYMBOL], destination_address=destination,
        note="e2e underpaid refund after the window",
    )  # fmt: skip
    invoice_id = str(invoice["id"])
    wait_for_receipt(
        context.http_client,
        send_transaction(context.http_client, sender=payer.address, to=str(invoice["deposit_address"]),
                         value_wei=20 * NATIVE_UNIT_WEI),
    )  # fmt: skip
    invoice = wait_for_invoice(
        context, invoice_id, description="underpaid", condition=lambda current: current["status"] == "underpaid"
    )
    check(invoice["refund_offer"]["reason"] == "payment_window_open", f"refund offer {invoice['refund_offer']}")
    early_reply = context.http_client.post(f"{API_URL}/public/invoices/{invoice_id}/refund-offer")
    check(early_reply.status_code >= 400, f"a refund-offer inside the payment window got {early_reply.status_code}")
    # The fork cannot wait out a real window; the window ends in the database instead.
    run_sql(context, f"UPDATE invoices SET expires_at = now() - interval '1 second' WHERE id = '{invoice_id}'")
    offer_reply = context.http_client.post(f"{API_URL}/public/invoices/{invoice_id}/refund-offer")
    check(
        offer_reply.status_code == 200, f"refund-offer after the window: {offer_reply.status_code} {offer_reply.text}"
    )
    offer = next(item for item in offer_reply.json()["offers"] if item["sender"] == payer.address)
    wrong_signature = Account.sign_message(
        encode_defunct(text=offer["message"]), private_key=Account.create().key
    ).signature
    wrong_reply = context.http_client.post(
        f"{API_URL}/public/invoices/{invoice_id}/refund-requests",
        json={"nonce": offer["nonce"], "signature": "0x" + wrong_signature.hex().removeprefix("0x")},
    )
    check(wrong_reply.status_code == 400, f"a wrong signer got {wrong_reply.status_code}")
    payer_balance_before = read_native_balance(context.http_client, payer.address)
    signature = Account.sign_message(encode_defunct(text=offer["message"]), private_key=payer.key).signature
    request_body = {"nonce": offer["nonce"], "signature": "0x" + signature.hex().removeprefix("0x")}
    refund_reply = context.http_client.post(
        f"{API_URL}/public/invoices/{invoice_id}/refund-requests", json=request_body
    )
    check(refund_reply.status_code == 200, f"refund-requests: {refund_reply.status_code} {refund_reply.text}")
    replay_reply = context.http_client.post(
        f"{API_URL}/public/invoices/{invoice_id}/refund-requests", json=request_body
    )
    check(replay_reply.status_code >= 400, f"a replayed refund request got {replay_reply.status_code}")
    invoice = wait_for_invoice(
        context, invoice_id, description="final refund and sweep",
        condition=lambda current: last_refund_final(current) and last_sweep_final(current),
    )  # fmt: skip
    refunded_wei = read_native_balance(context.http_client, payer.address) - payer_balance_before
    check(refunded_wei == 20 * NATIVE_UNIT_WEI, f"payer got {refunded_wei} wei back, expected 20 {NATIVE_SYMBOL}")
    destination_balance = read_native_balance(context.http_client, destination) - destination_balance_before
    check(
        destination_balance == 20 * NATIVE_UNIT_WEI,
        f"destination got {destination_balance} wei, expected 20 {NATIVE_SYMBOL}",
    )
    return {"invoice_id": invoice_id, "status": invoice["status"], "refunds": invoice["refunds"],
            "replay_status": replay_reply.status_code, "wrong_signer_status": wrong_reply.status_code}  # fmt: skip


def scenario_three_inputs_then_the_rest_refunded(context: FlowContext) -> dict[str, object]:
    """One address pays in three parts until past the amount (several inputs
    count until the amount is reached, then the rest goes back by itself once final): underpaid, underpaid, overpaid; the excess of the
    last input goes back by itself and the destination gets everything paid."""
    payer = HARNESS_ACCOUNTS[12]
    destination = context.operator_destination_address
    destination_balance_before = read_native_balance(context.http_client, destination)
    invoice = create_invoice(
        context, amount="2", currency=NATIVE_SYMBOL, assets=[NATIVE_SYMBOL], destination_address=destination,
        note="e2e three inputs then the rest refunded",
    )  # fmt: skip
    invoice_id = str(invoice["id"])
    deposit = str(invoice["deposit_address"])
    statuses: list[str] = []
    for input_wei, expected_status in [
        (NATIVE_UNIT_WEI, "underpaid"), (NATIVE_UNIT_WEI // 2, "underpaid"), (NATIVE_UNIT_WEI // 2 + 20 * NATIVE_UNIT_WEI, "overpaid")
    ]:  # fmt: skip
        wait_for_receipt(
            context.http_client,
            send_transaction(context.http_client, sender=payer.address, to=deposit, value_wei=input_wei),
        )
        payer_balance_after_input = read_native_balance(context.http_client, payer.address)
        invoice = wait_for_invoice(
            context, invoice_id, description=expected_status,
            condition=lambda current, wanted=expected_status: current["status"] == wanted,
        )  # fmt: skip
        statuses.append(str(invoice["status"]))
    check(len(invoice["payments"]) == 3, f"payments {invoice['payments']}")
    invoice = wait_for_invoice(
        context, invoice_id, description="final sweep and refund",
        condition=lambda current: last_sweep_final(current) and last_refund_final(current),
    )  # fmt: skip
    refunded_wei = read_native_balance(context.http_client, payer.address) - payer_balance_after_input
    check(refunded_wei == 20 * NATIVE_UNIT_WEI, f"payer got {refunded_wei} wei back, expected 20 {NATIVE_SYMBOL}")
    check(len(invoice["refunds"]) == 1 and invoice["refunds"][0]["amount"] == "20", f"refunds {invoice['refunds']}")
    destination_balance = read_native_balance(context.http_client, destination) - destination_balance_before
    check(
        destination_balance == 22 * NATIVE_UNIT_WEI,
        f"destination got {destination_balance} wei, expected 22 {NATIVE_SYMBOL}",
    )
    return {"invoice_id": invoice_id, "statuses": statuses, "refunds": invoice["refunds"], "refunded_wei": refunded_wei,
            "destination_wei": destination_balance}  # fmt: skip


def scenario_accept_partial_after_a_third_party_sweep(context: FlowContext) -> dict[str, object]:
    """Underpaid, then a third party sweeps the deposit (anyone may run the factory's sweep): accept-partial must not
    pay for a sweep of an empty deposit, and the invoice closes as paid (2026-10-09, seen live)."""
    payer = HARNESS_ACCOUNTS[13]
    destination = Account.create().address
    invoice = create_invoice(
        context, amount="2", currency=NATIVE_SYMBOL, assets=[NATIVE_SYMBOL], destination_address=destination,
        note="e2e accept-partial after a third-party sweep",
    )  # fmt: skip
    invoice_id = str(invoice["id"])
    wait_for_receipt(
        context.http_client,
        send_transaction(context.http_client, sender=payer.address, to=str(invoice["deposit_address"]),
                         value_wei=NATIVE_UNIT_WEI),
    )  # fmt: skip
    wait_for_invoice(
        context, invoice_id, description="underpaid", condition=lambda current: current["status"] == "underpaid"
    )
    invoice_salt = bytes.fromhex(
        run_sql(context, f"select encode(invoice_salt, 'hex') from invoices where id = '{invoice_id}'")
    )
    sweep_data = "0x" + keccak(text="sweepToDestination(bytes32,address,address[])")[:4].hex()
    sweep_data += encode(["bytes32", "address", "address[]"], [invoice_salt, destination, []]).hex()
    wait_for_receipt(
        context.http_client,
        send_transaction(context.http_client, sender=payer.address, to=context.factory_address, data=sweep_data,
                         gas=400_000),
    )  # fmt: skip
    time.sleep(6)  # a few ticks: the watcher reads the emptied deposit
    reply = context.http_client.post(f"{API_URL}/invoices/{invoice_id}/accept-partial", headers=context.account_headers)
    check(reply.status_code == 200, f"accept-partial: {reply.status_code} {reply.text}")
    time.sleep(6)
    operations = int(run_sql(context, f"SELECT count(*) FROM outgoing_operations WHERE invoice_id = '{invoice_id}'"))
    check(operations == 0, f"{operations} operations for an empty deposit")
    closed = run_sql(context, f"SELECT closed_at IS NOT NULL FROM invoices WHERE id = '{invoice_id}'")
    invoice = read_invoice(context, invoice_id)
    check(invoice["status"] == "paid" and closed == "t", f"status {invoice['status']}, closed {closed}")
    check(
        read_native_balance(context.http_client, destination) == NATIVE_UNIT_WEI,
        f"the destination lacks the 1 {NATIVE_SYMBOL}",
    )
    return {"invoice_id": invoice_id, "status": invoice["status"], "operations": operations}


def scenario_paid_inside_the_quote_final_after_it(context: FlowContext) -> dict[str, object]:
    """Mined while the quote is live, final after it lapsed: valued at the quote (the payment's block time), not the
    feed at the finalized head's time (2026-10-09). The fork's feeds never move, so the quote's POL price is doubled
    and its amount halved in the database: at the quote that pays the invoice, at the feed it is half short."""
    payer = HARNESS_ACCOUNTS[14].address
    invoice = create_invoice(
        context, amount="2", currency="USD", assets=[NATIVE_SYMBOL], destination_address=Account.create().address,
        note="e2e paid inside the quote",
    )  # fmt: skip
    invoice_id = str(invoice["id"])
    halved_amount_due_wei = -(-to_base_units(str(invoice["amount_due"]), decimals=18) // 2)
    run_sql(
        context,
        f"UPDATE price_quotes SET asset_price_usd = asset_price_usd * 2, amount_base_units = {halved_amount_due_wei}, "
        f"created_at = now() - interval '1 minute' WHERE invoice_id = '{invoice_id}'",
    )
    run_sql(context, f"UPDATE invoices SET amount_due_base_units = {halved_amount_due_wei} WHERE id = '{invoice_id}'")
    pause_mining(context.http_client)
    try:
        payment_hash = send_transaction(
            context.http_client, sender=payer, to=str(invoice["deposit_address"]), value_wei=halved_amount_due_wei
        )
        call_anvil(context.http_client, "evm_mine")
        receipt = wait_for_receipt(context.http_client, payment_hash)
        payment_block = call_anvil(context.http_client, "eth_getBlockByNumber", receipt["blockNumber"], False)
        payment_block_timestamp = int(str(payment_block["timestamp"]), 16) if isinstance(payment_block, dict) else 0
        check(payment_block_timestamp > 0, f"no block {receipt['blockNumber']}")
        # The quote lapses one second after the payment's block, before that block can be final.
        run_sql(
            context,
            f"UPDATE price_quotes SET expires_at = to_timestamp({payment_block_timestamp + 1}) "
            f"WHERE invoice_id = '{invoice_id}'",
        )
        time.sleep(max(0.0, payment_block_timestamp + 2 - time.time()))
    finally:
        resume_mining(context.http_client)
    invoice = wait_for_invoice(
        context, invoice_id, description="credited",
        condition=lambda current: current["status"] in {"paid", "underpaid", "overpaid"},
    )  # fmt: skip
    check(invoice["status"] == "paid", f"status {invoice['status']}, expected paid (credited at the quote)")
    stored_timestamp = run_sql(
        context, f"SELECT block_timestamp FROM invoice_payments WHERE invoice_id = '{invoice_id}'"
    )
    check(
        stored_timestamp == str(payment_block_timestamp),
        f"payment row block_timestamp {stored_timestamp}, expected {payment_block_timestamp}",
    )
    account_event_types = wait_for_account_events(context, invoice_id, expected_types={"invoice.paid"})
    return {"invoice_id": invoice_id, "status": invoice["status"], "payment_block_timestamp": payment_block_timestamp,
            "paid_wei": halved_amount_due_wei, "account_events": account_event_types}  # fmt: skip


def find_transfer_log_index(receipt: object, *, recipient: str) -> int:
    """The log index of the USDC Transfer to ``recipient`` in a receipt."""
    transfer_topic = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
    recipient_topic = "0x" + "0" * 24 + recipient.lower().removeprefix("0x")
    logs = receipt["logs"] if isinstance(receipt, dict) else []
    for entry in logs:
        if entry["address"].lower() == USDC_ADDRESS.lower() and entry["topics"][:1] + entry["topics"][2:3] == [
            transfer_topic,
            recipient_topic,
        ]:
            return int(entry["logIndex"], 16)
    raise FlowFailedError(f"no USDC Transfer to {recipient} in the receipt")


def scenario_token_payment_reorged_to_another_log_index(context: FlowContext) -> dict[str, object]:
    """A USDC payment seen at ``latest``, then re-mined by a reorg in the replacing block behind another USDC
    transfer, so its log index changes: it ends as one final row at its canonical block and log index, credited
    once (2026-10-09)."""
    payer = HARNESS_ACCOUNTS[15].address
    other_sender = HARNESS_ACCOUNTS[16].address
    invoice = create_invoice(
        context, amount="5", currency="USDC", assets=["USDC"], destination_address=Account.create().address,
        note="e2e token payment reorged to another log index",
    )  # fmt: skip
    invoice_id = str(invoice["id"])
    deposit_address = str(invoice["deposit_address"])
    amount_base_units = to_base_units(str(invoice["amount_due"]), decimals=6)
    fund_with_usdc(context.http_client, payer, base_units=amount_base_units)
    fund_with_usdc(context.http_client, other_sender, base_units=USDC_BASE_UNITS)
    pause_mining(context.http_client)
    try:
        payment_raw = sign_local_transaction(
            context.http_client, sender=payer, to=USDC_ADDRESS,
            data=build_erc20_transfer_data(deposit_address, amount_base_units),
        )  # fmt: skip
        # Signed now, mined only in the replacing block, ahead of the payment.
        other_raw = sign_local_transaction(
            context.http_client, sender=other_sender, to=USDC_ADDRESS,
            data=build_erc20_transfer_data(Account.create().address, USDC_BASE_UNITS),
        )  # fmt: skip
        payment_hash = str(call_anvil(context.http_client, "eth_sendRawTransaction", payment_raw))
        call_anvil(context.http_client, "evm_mine")
        original_receipt = call_anvil(context.http_client, "eth_getTransactionReceipt", payment_hash)
        original_log_index = find_transfer_log_index(original_receipt, recipient=deposit_address)
        wait_for_invoice(
            context, invoice_id, description="confirming with the payment row",
            condition=lambda current: current["status"] == "confirming" and bool(current["payments"]),
        )  # fmt: skip
        call_anvil(context.http_client, "anvil_reorg", 1, [[other_raw, 0], [payment_raw, 0]])
    finally:
        resume_mining(context.http_client)
    canonical_receipt = call_anvil(context.http_client, "eth_getTransactionReceipt", payment_hash)
    check(isinstance(canonical_receipt, dict), "the replayed payment has no receipt")
    canonical_log_index = find_transfer_log_index(canonical_receipt, recipient=deposit_address)
    check(
        canonical_log_index != original_log_index,
        f"the reorg kept log index {original_log_index}; the scenario did not move the payment",
    )
    invoice = wait_for_invoice(context, invoice_id, description="final sweep", condition=last_sweep_final)
    rows = run_sql(
        context,
        "SELECT log_index || ' ' || block_hash || ' ' || final FROM invoice_payments "
        f"WHERE invoice_id = '{invoice_id}' ORDER BY id",
    ).splitlines()
    expected_row = f"{canonical_log_index} {canonical_receipt['blockHash']} true"
    check(rows == [expected_row], f"payment rows {rows}, expected only [{expected_row}]")
    check(invoice["status"] == "paid", f"status {invoice['status']}, expected paid")
    check(
        to_base_units(str(invoice["amount_received"]), decimals=6) == amount_base_units,
        f"received {invoice['amount_received']}, expected {invoice['amount_due']} once",
    )
    return {"invoice_id": invoice_id, "payment": payment_hash, "original_log_index": original_log_index,
            "canonical_log_index": canonical_log_index, "rows": rows}  # fmt: skip


# The fork chain's native/USD Chainlink feed (fork_stack.NATIVE_USD_FEED_ADDRESS); one feed reverting fails the whole price read.
# PUSH1 0 PUSH1 0 REVERT: every call to the feed reverts.
ALWAYS_REVERTING_CODE = "0x60006000fd"
# The price cache's 30 s lifetime (payments/price_quotes) plus a margin: after it no copy of a good price is left.
PRICE_CACHE_SECONDS = 35


def read_watcher_log_records(context: FlowContext, *, operation: str) -> list[dict[str, object]]:
    """The watcher's JSON log records with that ``operation``."""
    output = run_command(
        compose_arguments(context.environment_file, "logs", "--no-color", "--no-log-prefix", "watcher"),
        environment=compose_environment(context.environment_file),
        log_path=context.scratch_folder / "commands.log",
    )
    records = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    return [record for record in records if record.get("operation") == operation]


def count_refund_operations(context: FlowContext, invoice_id: str) -> int:
    statement = f"SELECT count(*) FROM outgoing_operations WHERE invoice_id = '{invoice_id}' AND kind = 'refund'"  # noqa: S608
    return int(run_sql(context, statement))


def read_invoice_flag(context: FlowContext, invoice_id: str, *, condition: str) -> bool:
    return run_sql(context, f"SELECT {condition} FROM invoices WHERE id = '{invoice_id}'") == "t"  # noqa: S608


def scenario_refunds_wait_for_a_price_then_go_once(context: FlowContext) -> dict[str, object]:
    """A missing price defers an automatic refund, never drops it (2026-10-12, after a wrong-coin refund silently never
    fired): with the POL feed reverting, wrong-coin POL on a USDC invoice and a POL overpay go final with no refund
    and a WARNING deferral, and the overpaid deposit is swept and the invoice closed; with the feed back, the
    watcher's per-tick pass sends each back, exactly once."""
    stray_payer = HARNESS_ACCOUNTS[17].address
    overpay_payer = HARNESS_ACCOUNTS[18].address
    stray_invoice = create_invoice(
        context, amount="1", currency="USD", assets=["USDC"], destination_address=context.operator_destination_address,
        note="e2e stray refund waits for a price",
    )  # fmt: skip
    overpay_invoice = create_invoice(
        context, amount="1", currency="USD", assets=[NATIVE_SYMBOL], destination_address=context.operator_destination_address,
        note="e2e overpay refund waits for a price",
    )  # fmt: skip
    stray_invoice_id, overpay_invoice_id = str(stray_invoice["id"]), str(overpay_invoice["id"])
    # The fork's block times trail the wall clock, so the payment's block would predate its quote and no price at all
    # would credit it (a credit without a price waits too); start the quote earlier so the quote values it.
    run_sql(
        context,
        f"UPDATE price_quotes SET created_at = now() - interval '10 minutes' WHERE invoice_id = '{overpay_invoice_id}'",  # noqa: S608
    )
    stray_wei = 20 * NATIVE_UNIT_WEI
    overpay_wei = to_base_units(str(overpay_invoice["amount_due"]), decimals=18) + 20 * NATIVE_UNIT_WEI
    original_feed_code = str(call_anvil(context.http_client, "eth_getCode", NATIVE_USD_FEED_ADDRESS, "latest"))
    call_anvil(context.http_client, "anvil_setCode", NATIVE_USD_FEED_ADDRESS, ALWAYS_REVERTING_CODE)
    try:
        time.sleep(PRICE_CACHE_SECONDS)
        wait_for_receipt(
            context.http_client,
            send_transaction(context.http_client, sender=stray_payer, to=str(stray_invoice["deposit_address"]),
                             value_wei=stray_wei),
        )  # fmt: skip
        wait_for_receipt(
            context.http_client,
            send_transaction(context.http_client, sender=overpay_payer, to=str(overpay_invoice["deposit_address"]),
                             value_wei=overpay_wei),
        )  # fmt: skip
        wait_for_invoice(
            context, stray_invoice_id, description="final stray payment",
            condition=lambda current: bool(current["stray_payments"])
            and all(stray["final"] for stray in current["stray_payments"]),  # ty: ignore[not-iterable]
        )  # fmt: skip
        wait_for_invoice(context, overpay_invoice_id, description="overpaid and swept", condition=last_sweep_final)
        deadline = time.monotonic() + 30
        while not read_invoice_flag(context, overpay_invoice_id, condition="closed_at IS NOT NULL"):
            check(time.monotonic() < deadline, "the overpaid invoice did not close after its sweep")
            time.sleep(1)
        time.sleep(5)  # a few more ticks: nothing may fire while no price is readable
        stray_invoice = read_invoice(context, stray_invoice_id)
        overpay_invoice = read_invoice(context, overpay_invoice_id)
        check(overpay_invoice["status"] == "overpaid", f"status {overpay_invoice['status']}, expected overpaid")
        for invoice_id in (stray_invoice_id, overpay_invoice_id):
            check(count_refund_operations(context, invoice_id) == 0, f"{invoice_id}: a refund without a price")
            check(
                read_invoice_flag(context, invoice_id, condition="automatic_refund_deferred_since IS NOT NULL"),
                f"{invoice_id}: no deferral recorded",
            )
        stray_handling = stray_invoice["stray_payments"][0]["refund_handling"]  # ty: ignore[not-subscriptable]
        check(stray_handling == "automatic", f"stray refund_handling {stray_handling} without a price")
        offer_reason = overpay_invoice["refund_offer"]["reason"]  # ty: ignore[not-subscriptable]
        check(offer_reason == "refund_in_progress", f"overpay refund_offer.reason {offer_reason} without a price")
        warned_invoice_ids = {
            record["invoice_id"]
            for record in read_watcher_log_records(context, operation="invoice_auto_refund_deferred")
            if record.get("level") == "WARNING"
        }
        check(
            {stray_invoice_id, overpay_invoice_id} <= warned_invoice_ids,
            f"no WARNING deferral logged for both invoices; warned for {warned_invoice_ids}",
        )
    finally:
        call_anvil(context.http_client, "anvil_setCode", NATIVE_USD_FEED_ADDRESS, original_feed_code)
    stray_payer_balance = read_native_balance(context.http_client, stray_payer)
    overpay_payer_balance = read_native_balance(context.http_client, overpay_payer)
    stray_invoice = wait_for_invoice(
        context, stray_invoice_id, description="final stray refund", condition=last_refund_final
    )
    overpay_invoice = wait_for_invoice(
        context, overpay_invoice_id, description="final overpay refund", condition=last_refund_final
    )
    time.sleep(5)  # a few more ticks: the per-tick pass must not send a second refund
    stray_refunded_wei = read_native_balance(context.http_client, stray_payer) - stray_payer_balance
    overpay_refunded_wei = read_native_balance(context.http_client, overpay_payer) - overpay_payer_balance
    check(stray_refunded_wei == stray_wei, f"stray payer got {stray_refunded_wei} wei back, expected {stray_wei}")
    overpay_refund_wei = to_base_units(str(overpay_invoice["refunds"][0]["amount"]), decimals=18)  # ty: ignore[not-subscriptable]
    check(
        overpay_refunded_wei == overpay_refund_wei and 19 * NATIVE_UNIT_WEI < overpay_refund_wei < 21 * NATIVE_UNIT_WEI,
        f"overpay payer got {overpay_refunded_wei} wei back, the refund says {overpay_refund_wei}",
    )
    for invoice_id in (stray_invoice_id, overpay_invoice_id):
        check(count_refund_operations(context, invoice_id) == 1, f"{invoice_id}: not exactly one refund")
        check(
            read_invoice_flag(context, invoice_id, condition="automatic_refund_deferred_since IS NULL"),
            f"{invoice_id}: still marked deferred after its refund",
        )
    decided_records = [
        record
        for record in read_watcher_log_records(context, operation="invoice_auto_refund_deferred")
        if record.get("level") == "INFO"
    ]
    return {"stray_invoice": stray_invoice_id, "overpay_invoice": overpay_invoice_id,
            "stray_refunded_wei": stray_refunded_wei, "overpay_refunded_wei": overpay_refunded_wei,
            "stray_refunds": stray_invoice["refunds"], "overpay_refunds": overpay_invoice["refunds"],
            "decided_after_deferral": decided_records}  # fmt: skip


def run_watcher_compose_command(context: FlowContext, command: str) -> None:
    run_command(
        compose_arguments(context.environment_file, command, "watcher"),
        environment=compose_environment(context.environment_file),
        log_path=context.scratch_folder / "commands.log",
    )


def scenario_stray_payment_past_the_block_scan_then_attributed(context: FlowContext) -> dict[str, object]:
    """Native money the watcher's block scan cannot reach (it reads at most 200 blocks back) is recorded with no sender
    and handed to the owner (`owner_settles`, "Needs you" on /activity), never silently kept; once the operator
    proves its transaction the row is attributed and the per-sender refund goes out once (2026-10-12)."""
    payer = HARNESS_ACCOUNTS[19].address
    invoice = create_invoice(
        context, amount="1", currency="USD", assets=["USDC"], destination_address=context.operator_destination_address,
        note="e2e stray payment past the block scan",
    )  # fmt: skip
    invoice_id = str(invoice["id"])
    stray_wei = 20 * NATIVE_UNIT_WEI
    time.sleep(3)  # the watcher reads the new deposit once before it stops
    run_watcher_compose_command(context, "stop")
    try:
        transaction_hash = send_transaction(
            context.http_client, sender=payer, to=str(invoice["deposit_address"]), value_wei=stray_wei
        )
        wait_for_receipt(context.http_client, transaction_hash)
        call_anvil(context.http_client, "anvil_mine", hex(NATIVE_ATTRIBUTION_WINDOW_BLOCKS + 100))
    finally:
        run_watcher_compose_command(context, "start")
    invoice = wait_for_invoice(
        context, invoice_id, description="a final stray row",
        condition=lambda current: bool(current["stray_payments"])
        and all(stray["final"] for stray in current["stray_payments"]),  # ty: ignore[not-iterable]
    )  # fmt: skip
    stray_rows = list(invoice["stray_payments"])  # ty: ignore[invalid-argument-type]
    check(
        len(stray_rows) == 1
        and stray_rows[0]["sender"] is None
        and stray_rows[0]["refund_handling"] == "owner_settles",
        f"stray rows {stray_rows}, expected one with no sender for the owner",
    )
    wait_for_account_events(context, invoice_id, expected_types={"invoice.stray_payment"})
    time.sleep(5)  # a few more ticks: nothing goes to an unknown sender
    check(count_refund_operations(context, invoice_id) == 0, "a refund for money with no sender")
    # Read before the command: the refund it queues can be final by the time it returns.
    payer_balance = read_native_balance(context.http_client, payer)
    exit_code, output = run_operator_cli(
        context, "record-stray-payment-from-transaction", transaction_hash, "--chain", FORK_CHAIN_KEY
    )
    check(exit_code == 0, f"record-stray-payment-from-transaction exited {exit_code}: {output[-500:]}")
    invoice = wait_for_invoice(context, invoice_id, description="final stray refund", condition=last_refund_final)
    time.sleep(5)  # a few more ticks: no second refund
    stray_rows = list(read_invoice(context, invoice_id)["stray_payments"])  # ty: ignore[invalid-argument-type]
    check(
        len(stray_rows) == 1 and stray_rows[0]["sender"] == payer and stray_rows[0]["tx_hash"] == transaction_hash
        and stray_rows[0]["refund_handling"] == "automatic",
        f"stray rows {stray_rows}, expected only the attributed one",
    )  # fmt: skip
    refunded_wei = read_native_balance(context.http_client, payer) - payer_balance
    check(refunded_wei == stray_wei, f"payer got {refunded_wei} wei back, expected {stray_wei}")
    check(count_refund_operations(context, invoice_id) == 1, "not exactly one refund")
    return {"invoice_id": invoice_id, "transaction_hash": transaction_hash, "stray_rows": stray_rows,
            "refunds": invoice["refunds"], "refunded_wei": refunded_wei}  # fmt: skip


def count_operator_alerts(context: FlowContext, *, alert_kind: str, invoice_id: str) -> int:
    statement = (
        "SELECT count(*) FROM account_events WHERE event_type = 'operator.alert' AND account_id IS NOT NULL "  # noqa: S608
        f"AND data->>'kind' = '{alert_kind}' AND data->>'invoice_id' = '{invoice_id}'"
    )
    return int(run_sql(context, statement))


def wait_for_operator_alert_by_problem_key(context: FlowContext, problem_key: str, *, timeout_seconds: float) -> float:
    """Seconds until an ``operator.alert`` with ``problem_key`` is recorded on the operator account."""
    statement = (
        "SELECT count(*) FROM account_events WHERE event_type = 'operator.alert' AND account_id IS NOT NULL "  # noqa: S608
        f"AND data->>'problem_key' = '{problem_key}'"
    )
    started = time.monotonic()
    while int(run_sql(context, statement)) == 0:
        check(time.monotonic() - started < timeout_seconds, f"no operator.alert {problem_key}")
        time.sleep(2)
    return round(time.monotonic() - started, 1)


def read_watcher_health(context: FlowContext, container_id: str) -> str:
    return run_command(
        ["docker", "inspect", "--format", "{{.State.Health.Status}}", container_id],
        log_path=context.scratch_folder / "commands.log",
    ).strip()


def wait_for_watcher_health(context: FlowContext, container_id: str, *, status: str, timeout_seconds: float) -> float:
    """Seconds until the watcher container's health is ``status``."""
    started = time.monotonic()
    while (current_status := read_watcher_health(context, container_id)) != status:
        check(time.monotonic() - started < timeout_seconds, f"watcher health {current_status}, not {status}")
        time.sleep(2)
    return round(time.monotonic() - started, 1)


def scenario_reconcile_alerts_and_watcher_health(context: FlowContext) -> dict[str, object]:
    """The operator's safety net (2026-10-12). On a clean stack `reconcile` exits 0. A sweep that reverts (the
    destination rejects POL) leaves a paid invoice's money in its deposit with no sweep under way: `reconcile` exits 1
    naming it, and the watcher records one `operator.alert` (`sweep_failed`) on the operator's account, not repeated on
    its next check. Swept by hand, `reconcile` is clean again. A watcher whose process is frozen stops its heartbeat and
    its container turns unhealthy, then healthy once it ticks again. Runs first: later scenarios leave work behind."""
    exit_code, clean_output = run_operator_cli(context, "reconcile", "--chain", FORK_CHAIN_KEY)
    check(exit_code == 0, f"reconcile on a clean stack exited {exit_code}: {clean_output[-1500:]}")
    destination = Account.create().address
    invoice = create_invoice(
        context, amount="2", currency=NATIVE_SYMBOL, assets=[NATIVE_SYMBOL], destination_address=destination,
        note="e2e reconcile: a sweep that reverts",
    )  # fmt: skip
    invoice_id = str(invoice["id"])
    call_anvil(context.http_client, "anvil_setCode", destination, ALWAYS_REVERTING_CODE)
    try:
        wait_for_receipt(
            context.http_client,
            send_transaction(context.http_client, sender=HARNESS_ACCOUNTS[25].address,
                             to=str(invoice["deposit_address"]), value_wei=2 * NATIVE_UNIT_WEI),
        )  # fmt: skip
        invoice = wait_for_invoice(
            context, invoice_id, description="paid with its sweep blocked",
            condition=lambda current: current["status"] == "paid" and bool(current["sweep_blocked"]),
        )  # fmt: skip
        wait_for_account_events(context, invoice_id, expected_types={"invoice.paid", "invoice.sweep_failed"})
        exit_code, flagged_output = run_operator_cli(context, "reconcile", "--chain", FORK_CHAIN_KEY)
        check(exit_code == 1, f"reconcile with an unswept paid deposit exited {exit_code}")
        check(
            "== deposit_balances: 1 need attention" in flagged_output
            and f"sweep-invoice {invoice_id}" in flagged_output,
            f"reconcile did not name {invoice_id}: {flagged_output[-2000:]}",
        )
        # The watcher checks once a minute.
        deadline = time.monotonic() + 100
        while count_operator_alerts(context, alert_kind="sweep_failed", invoice_id=invoice_id) == 0:
            check(time.monotonic() < deadline, f"no operator.alert sweep_failed for {invoice_id}")
            time.sleep(2)
        time.sleep(70)  # past the next check: a problem still open is not alerted again before its reminder
        alert_count = count_operator_alerts(context, alert_kind="sweep_failed", invoice_id=invoice_id)
        check(alert_count == 1, f"{alert_count} sweep_failed alerts for {invoice_id}, expected 1")
    finally:
        call_anvil(context.http_client, "anvil_setCode", destination, "0x")
    exit_code, sweep_output = run_operator_cli(context, "sweep-invoice", invoice_id)
    check(exit_code == 0, f"sweep-invoice exited {exit_code}: {sweep_output[-500:]}")
    # The failed sweep stays listed (the API lists it after the final one), so look for any final sweep.
    invoice = wait_for_invoice(
        context, invoice_id, description="final sweep by hand, sweep_blocked cleared",
        condition=lambda current: not current["sweep_blocked"]
        and any(sweep["status"] == "final" for sweep in current["sweeps"]),  # ty: ignore[not-iterable]
    )  # fmt: skip
    exit_code, cleaned_output = run_operator_cli(context, "reconcile", "--chain", FORK_CHAIN_KEY)
    check(exit_code == 0, f"reconcile after the sweep by hand exited {exit_code}: {cleaned_output[-1500:]}")
    container_id = run_command(
        compose_arguments(context.environment_file, "ps", "-q", "watcher"),
        environment=compose_environment(context.environment_file),
        log_path=context.scratch_folder / "commands.log",
    ).strip()
    process_id = run_command(
        ["docker", "inspect", "--format", "{{.State.Pid}}", container_id],
        log_path=context.scratch_folder / "commands.log",
    ).strip()
    check(read_watcher_health(context, container_id) == "healthy", "the watcher was not healthy before the freeze")
    run_command(["kill", "-STOP", process_id], log_path=context.scratch_folder / "commands.log")
    try:
        seconds_to_unhealthy = wait_for_watcher_health(context, container_id, status="unhealthy", timeout_seconds=180)
    finally:
        run_command(["kill", "-CONT", process_id], log_path=context.scratch_folder / "commands.log")
    seconds_to_healthy = wait_for_watcher_health(context, container_id, status="healthy", timeout_seconds=60)
    return {"invoice_id": invoice_id, "sweeps": invoice["sweeps"], "alert_count": alert_count,
            "flagged_reconcile": flagged_output[-2500:], "seconds_to_unhealthy": seconds_to_unhealthy,
            "seconds_to_healthy": seconds_to_healthy}  # fmt: skip


def call_usdc_blacklister(context: FlowContext, function_signature: str, blacklisted_address: str) -> None:
    """``blacklist`` or ``unBlacklist`` an address on Polygon's USDC, as its blacklister (impersonated on the fork):
    Circle's FiatToken ``transfer`` is ``notBlacklisted(msg.sender)`` and ``notBlacklisted(to)``
    (https://github.com/circlefin/stablecoin-evm, ``contracts/v1/FiatTokenV1.sol``, ``Blacklistable.sol``)."""
    blacklister_reply = call_anvil(
        context.http_client, "eth_call", {"to": USDC_ADDRESS, "data": "0x" + keccak(text="blacklister()")[:4].hex()},
        "latest",
    )  # fmt: skip
    blacklister_address = "0x" + str(blacklister_reply)[-40:]
    call_anvil(context.http_client, "anvil_impersonateAccount", blacklister_address)
    call_anvil(context.http_client, "anvil_setBalance", blacklister_address, hex(NATIVE_UNIT_WEI))
    call_data = "0x" + (keccak(text=function_signature)[:4] + encode(["address"], [blacklisted_address])).hex()
    wait_for_receipt(
        context.http_client,
        send_transaction(context.http_client, sender=blacklister_address, to=USDC_ADDRESS, data=call_data),
    )


def count_sweeps(invoice: dict[str, object], *, status: str, asset_symbol: str | None = None) -> int:
    sweeps = invoice["sweeps"]
    check(isinstance(sweeps, list), f"sweeps is {sweeps!r}")
    return sum(
        1
        for sweep in sweeps  # ty: ignore[not-iterable]
        if sweep["status"] == status and (asset_symbol is None or sweep["asset_symbol"] == asset_symbol)
    )


def scenario_frozen_token_skipped_by_hand(context: FlowContext) -> dict[str, object]:
    """A frozen token holds up a whole deposit (2026-10-13): USDC blacklists the destination, so the sweep of a USDC
    invoice that also got 1 POL reverts and the invoice is ``sweep_blocked``. ``sweep-invoice --skip-token POL`` and
    ``--skip-token NOPE`` are refused; ``--skip-token USDC`` moves the POL and leaves the USDC, and the next sweep
    that lists USDC fails again, so the invoice is blocked again. Unblacklisted, a plain ``sweep-invoice`` moves the
    USDC and clears it."""
    destination = Account.create().address
    payer = FROZEN_TOKEN_SCENARIO_PAYER.address
    invoice = create_invoice(
        context, amount="2", currency="USD", assets=["USDC"], destination_address=destination,
        note="e2e frozen token skipped by hand",
    )  # fmt: skip
    invoice_id = str(invoice["id"])
    deposit_address = str(invoice["deposit_address"])
    amount_base_units = to_base_units(str(invoice["amount_due"]), decimals=6)
    call_usdc_blacklister(context, "blacklist(address)", destination)
    try:
        fund_with_usdc(context.http_client, payer, base_units=10 * USDC_BASE_UNITS)
        # One block for both, so no sweep moves the POL before the USDC is there to revert it.
        pause_mining(context.http_client)
        try:
            payment_hashes = [
                send_transaction(context.http_client, sender=payer, to=deposit_address, value_wei=NATIVE_UNIT_WEI),
                send_transaction(context.http_client, sender=payer, to=USDC_ADDRESS,
                                 data=build_erc20_transfer_data(deposit_address, amount_base_units)),
            ]  # fmt: skip
        finally:
            resume_mining(context.http_client)
        for payment_hash in payment_hashes:
            wait_for_receipt(context.http_client, payment_hash)
        wait_for_invoice(
            context, invoice_id, description="paid with its sweep blocked by the frozen destination",
            condition=lambda current: current["status"] == "paid" and bool(current["sweep_blocked"]),
        )  # fmt: skip
        native_exit_code, native_output = run_operator_cli(
            context, "sweep-invoice", invoice_id, "--skip-token", NATIVE_SYMBOL
        )
        check(
            native_exit_code == 1 and "native coin" in native_output,
            f"--skip-token {NATIVE_SYMBOL}: {native_output[-500:]}",
        )
        unknown_exit_code, unknown_output = run_operator_cli(
            context, "sweep-invoice", invoice_id, "--skip-token", "NOPE"
        )
        check(unknown_exit_code == 1 and "refused" in unknown_output, f"--skip-token NOPE: {unknown_output[-500:]}")
        exit_code, skip_output = run_operator_cli(context, "sweep-invoice", invoice_id, "--skip-token", "usdc")
        check(exit_code == 0, f"sweep-invoice --skip-token usdc exited {exit_code}: {skip_output[-500:]}")
        deadline = time.monotonic() + SCENARIO_TIMEOUT_SECONDS
        while read_native_balance(context.http_client, destination) != NATIVE_UNIT_WEI:
            check(time.monotonic() < deadline, f"{invoice_id}: the {NATIVE_SYMBOL} never reached the destination")
            time.sleep(2)
        check(
            read_usdc_balance(context.http_client, deposit_address) == amount_base_units,
            f"{invoice_id}: the skipped USDC left the deposit",
        )
        # The API lists final sweeps per asset first, then those under way, then the failed ones.
        reblocked_invoice = wait_for_invoice(
            context, invoice_id, description="blocked again by the next sweep that lists USDC",
            condition=lambda current: bool(current["sweep_blocked"])
            and count_sweeps(current, status="final", asset_symbol=NATIVE_SYMBOL) == 1
            and count_sweeps(current, status="failed") == 2,
        )  # fmt: skip
    finally:
        call_usdc_blacklister(context, "unBlacklist(address)", destination)
    exit_code, sweep_output = run_operator_cli(context, "sweep-invoice", invoice_id)
    check(exit_code == 0, f"sweep-invoice exited {exit_code}: {sweep_output[-500:]}")
    invoice = wait_for_invoice(
        context, invoice_id, description="the USDC swept by hand, sweep_blocked cleared",
        condition=lambda current: not current["sweep_blocked"]
        and count_sweeps(current, status="final", asset_symbol="USDC") == 1,
    )  # fmt: skip
    destination_usdc_base_units = read_usdc_balance(context.http_client, destination)
    check(
        destination_usdc_base_units == amount_base_units,
        f"destination holds {destination_usdc_base_units} USDC base units, expected {amount_base_units}",
    )
    # The USDC the skipped sweeps left was the invoice's own payment, never a late one (2026-10-13).
    check(not invoice["late_payment"], f"{invoice_id}: the skipped USDC was flagged as a late payment")
    return {"invoice_id": invoice_id, "sweeps_while_frozen": reblocked_invoice["sweeps"], "sweeps": invoice["sweeps"],
            "destination_usdc_base_units": destination_usdc_base_units}  # fmt: skip


def scenario_skipped_token_on_a_lapsed_underpaid_invoice_is_not_late(context: FlowContext) -> dict[str, object]:
    """The 2026-10-13 false late payment: a USDC invoice underpaid (and sent 1 POL besides) lapses, so it closes and
    its closing sweep reverts on the frozen destination (``sweep_blocked``). ``sweep-invoice --skip-token usdc``
    moves the POL; the credited USDC stays in the closed deposit, and the rescan asks for its sweep again (blocked
    again) without calling it a late payment."""
    destination = Account.create().address
    payer = LAPSED_FROZEN_TOKEN_SCENARIO_PAYER.address
    invoice = create_invoice(
        context, amount="2", currency="USD", assets=["USDC"], destination_address=destination,
        note="e2e frozen token on a lapsed underpaid invoice", lifetime_seconds=300,
    )  # fmt: skip
    invoice_id = str(invoice["id"])
    deposit_address = str(invoice["deposit_address"])
    underpaid_base_units = USDC_BASE_UNITS
    call_usdc_blacklister(context, "blacklist(address)", destination)
    try:
        fund_with_usdc(context.http_client, payer, base_units=5 * USDC_BASE_UNITS)
        pause_mining(context.http_client)
        try:
            payment_hashes = [
                send_transaction(context.http_client, sender=payer, to=deposit_address, value_wei=NATIVE_UNIT_WEI),
                send_transaction(context.http_client, sender=payer, to=USDC_ADDRESS,
                                 data=build_erc20_transfer_data(deposit_address, underpaid_base_units)),
            ]  # fmt: skip
        finally:
            resume_mining(context.http_client)
        for payment_hash in payment_hashes:
            wait_for_receipt(context.http_client, payment_hash)
        wait_for_invoice(
            context, invoice_id, description="underpaid", condition=lambda current: current["status"] == "underpaid"
        )
        # An underpaid invoice closes at underpaid_open_until; move it (and the payment window) instead of waiting.
        run_sql(
            context,
            "update invoices set expires_at = now() - interval '1 second', "
            f"underpaid_open_until = now() - interval '1 second' where id = '{invoice_id}'",
        )
        wait_for_invoice(
            context, invoice_id, description="lapsed, its closing sweep blocked by the frozen destination",
            condition=lambda current: bool(current["sweep_blocked"]) and count_sweeps(current, status="failed") >= 1,
        )  # fmt: skip
        exit_code, skip_output = run_operator_cli(context, "sweep-invoice", invoice_id, "--skip-token", "usdc")
        check(exit_code == 0, f"sweep-invoice --skip-token usdc exited {exit_code}: {skip_output[-500:]}")
        failed_before_rescan = count_sweeps(
            wait_for_invoice(
                context, invoice_id, description=f"the {NATIVE_SYMBOL} swept past the skipped USDC",
                condition=lambda current: count_sweeps(current, status="final", asset_symbol=NATIVE_SYMBOL) == 1,
            ),
            status="failed",
        )  # fmt: skip
        # The rescan finds the USDC on the closed invoice and asks for its sweep, which the frozen token fails again.
        rescanned_invoice = wait_for_invoice(
            context, invoice_id, description="the rescan's sweep of the left USDC, blocked again",
            condition=lambda current: bool(current["sweep_blocked"])
            and count_sweeps(current, status="failed") > failed_before_rescan,
        )  # fmt: skip
        check(
            read_usdc_balance(context.http_client, deposit_address) == underpaid_base_units,
            f"{invoice_id}: the skipped USDC left the deposit",
        )
        check(
            not rescanned_invoice["late_payment"],
            f"{invoice_id}: the credited USDC the skipped sweeps left was flagged as a late payment",
        )
    finally:
        call_usdc_blacklister(context, "unBlacklist(address)", destination)
    exit_code, sweep_output = run_operator_cli(context, "sweep-invoice", invoice_id)
    check(exit_code == 0, f"sweep-invoice exited {exit_code}: {sweep_output[-500:]}")
    invoice = wait_for_invoice(
        context, invoice_id, description="the USDC swept by hand, still not late",
        condition=lambda current: not current["sweep_blocked"]
        and count_sweeps(current, status="final", asset_symbol="USDC") == 1,
    )  # fmt: skip
    check(not invoice["late_payment"], f"{invoice_id}: flagged late once the USDC was swept")
    return {"invoice_id": invoice_id, "status": invoice["status"], "sweeps": invoice["sweeps"]}


# The watcher's test-only hook (backend settlements/configuration.py): resolutions named `kind:status` raise after the
# operation's new status is written and before its invoice effect, so the transaction rolls back whole.
RESOLVE_FAILURE_HOOK_SETTING = "PAY_E2E_FAIL_AFTER_OPERATION_RESOLVE"


def restart_watcher_with_resolve_failures(context: FlowContext, failing_resolutions: str) -> None:
    """Rewrite the hook's line of the stack's env file (empty: no line) and recreate the watcher on it: a restart."""
    kept_lines = [
        line
        for line in context.environment_file.read_text().splitlines()
        if not line.startswith(f"{RESOLVE_FAILURE_HOOK_SETTING}=")
    ]
    if failing_resolutions:
        kept_lines.append(f"{RESOLVE_FAILURE_HOOK_SETTING}={failing_resolutions}")
    context.environment_file.write_text("".join(f"{line}\n" for line in kept_lines))
    run_command(
        compose_arguments(context.environment_file, "up", "-d", "--no-deps", "--force-recreate", "watcher"),
        environment=compose_environment(context.environment_file),
        log_path=context.scratch_folder / "commands.log",
    )


def wait_for_injected_resolve_failure(context: FlowContext, operation_id: str, *, timeout_seconds: float) -> int:
    """Wait until the watcher logged the hook's failure for that operation; how many times it has so far."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        failures = [
            record
            for record in read_watcher_log_records(context, operation="settlement_step")
            if str(record.get("operation_id")) == operation_id and RESOLVE_FAILURE_HOOK_SETTING in json.dumps(record)
        ]
        if failures:
            return len(failures)
        check(time.monotonic() < deadline, f"operation {operation_id}: no injected resolve failure logged")
        time.sleep(3)


def read_operation_status(context: FlowContext, operation_id: str) -> str:
    return run_sql(context, f"SELECT status FROM outgoing_operations WHERE id = {int(operation_id)}")  # noqa: S608


def wait_for_operation_id(context: FlowContext, invoice_id: str, *, kind: str) -> str:
    deadline = time.monotonic() + 120
    while not (
        operation_id := run_sql(
            context,
            f"SELECT id FROM outgoing_operations WHERE invoice_id = '{invoice_id}' AND kind = '{kind}' ORDER BY id LIMIT 1",  # noqa: S608
        )
    ):
        check(time.monotonic() < deadline, f"{invoice_id}: no {kind} recorded")
        time.sleep(1)
    return operation_id


def count_account_events(context: FlowContext, invoice_id: str, *, event_type: str) -> int:
    return read_account_event_types(context, invoice_id).count(event_type)


def scenario_refund_final_applied_once_after_a_failed_resolve(context: FlowContext) -> dict[str, object]:
    """A refund's ``final`` and its ``invoice.refunded`` commit together (2026-10-12): with the watcher failing every
    time between the two, the refund stays ``mined`` and no event is recorded; restarted without the failure, the
    refund goes final and ``invoice.refunded`` is recorded exactly once."""
    payer = HARNESS_ACCOUNTS[20].address
    restart_watcher_with_resolve_failures(context, "refund:final")
    try:
        invoice = create_invoice(
            context, amount="2", currency=NATIVE_SYMBOL, assets=[NATIVE_SYMBOL],
            destination_address=context.operator_destination_address, note="e2e refund final after a failed resolve",
        )  # fmt: skip
        invoice_id = str(invoice["id"])
        wait_for_receipt(
            context.http_client,
            send_transaction(context.http_client, sender=payer, to=str(invoice["deposit_address"]),
                             value_wei=40 * NATIVE_UNIT_WEI),
        )  # fmt: skip
        payer_balance_before = read_native_balance(context.http_client, payer)  # the refund waits for finality
        operation_id = wait_for_operation_id(context, invoice_id, kind="refund")
        failures = wait_for_injected_resolve_failure(context, operation_id, timeout_seconds=180)
        time.sleep(4)  # a few more failing passes
        status_while_failing = read_operation_status(context, operation_id)
        check(
            status_while_failing == "mined", f"refund {operation_id} is {status_while_failing} while its resolve fails"
        )
        check(
            count_account_events(context, invoice_id, event_type="invoice.refunded") == 0,
            "invoice.refunded recorded while the refund's resolve rolled back",
        )
        # Ten failed passes in a row alert the operator at the next alert check (once a minute).
        step_failing_alert_seconds = wait_for_operator_alert_by_problem_key(
            context, f"operation_step_failing:{operation_id}", timeout_seconds=150
        )
    finally:
        restart_watcher_with_resolve_failures(context, "")
    invoice = wait_for_invoice(context, invoice_id, description="final refund", condition=last_refund_final)
    wait_for_account_events(context, invoice_id, expected_types={"invoice.refunded"})
    time.sleep(5)  # a few more ticks: nothing applied twice
    refunded_events = count_account_events(context, invoice_id, event_type="invoice.refunded")
    check(refunded_events == 1, f"invoice.refunded recorded {refunded_events} times")
    check(count_refund_operations(context, invoice_id) == 1, "not exactly one refund")
    refunded_wei = read_native_balance(context.http_client, payer) - payer_balance_before
    check(refunded_wei == 38 * NATIVE_UNIT_WEI, f"payer got {refunded_wei} wei back, expected 38 {NATIVE_SYMBOL}")
    return {"invoice_id": invoice_id, "operation_id": operation_id, "failed_resolves_logged": failures,
            "status_while_failing": status_while_failing, "step_failing_alert_seconds": step_failing_alert_seconds,
            "refunds": invoice["refunds"],
            "account_events": read_account_event_types(context, invoice_id)}  # fmt: skip


def scenario_refund_needs_owner_applied_once_after_a_failed_resolve(context: FlowContext) -> dict[str, object]:
    """A refund's ``needs_owner`` and its ``refund.needs_owner`` commit together (2026-10-12): with the watcher failing
    between the two, the abandoned refund stays ``broadcast`` with no event; restarted without the failure, it waits
    for the owner and ``refund.needs_owner`` is recorded exactly once."""
    payer = HARNESS_ACCOUNTS[21]
    restart_watcher_with_resolve_failures(context, "refund:needs_owner")
    try:
        invoice_id, operation_id, _, taking_hash = abandon_automatic_refund_attempt(
            context, payer=payer, note="e2e needs_owner after a failed resolve"
        )
        # The watcher decides NONCE_TAKEN_ELSEWHERE_AFTER (5 min) after its newest attempt.
        failures = wait_for_injected_resolve_failure(context, operation_id, timeout_seconds=420)
        time.sleep(4)
        status_while_failing = read_operation_status(context, operation_id)
        check(
            status_while_failing == "broadcast",
            f"refund {operation_id} is {status_while_failing} while its resolve fails",
        )
        check(
            count_account_events(context, invoice_id, event_type="refund.needs_owner") == 0,
            "refund.needs_owner recorded while the refund's resolve rolled back",
        )
    finally:
        restart_watcher_with_resolve_failures(context, "")
    wait_for_invoice(
        context, invoice_id, description="needs_owner",
        condition=lambda current: current["refunds"][-1]["status"] == "needs_owner",
    )  # fmt: skip
    wait_for_account_events(context, invoice_id, expected_types={"refund.needs_owner"})
    time.sleep(5)
    needs_owner_events = count_account_events(context, invoice_id, event_type="refund.needs_owner")
    check(needs_owner_events == 1, f"refund.needs_owner recorded {needs_owner_events} times")
    # Free the reserved amount, so the float is whole for what follows.
    exit_code, output = run_operator_cli(
        context, "refunds", "mark-unpaid", operation_id, "--nonce-taken-by", taking_hash
    )
    check(exit_code == 0 and "is now failed" in output, f"mark-unpaid: {output[-400:]}")
    return {"invoice_id": invoice_id, "operation_id": operation_id, "failed_resolves_logged": failures,
            "status_while_failing": status_while_failing,
            "account_events": read_account_event_types(context, invoice_id)}  # fmt: skip


def scenario_sweep_failure_applied_once_after_a_failed_resolve(context: FlowContext) -> dict[str, object]:
    """A sweep's ``failed`` and its ``sweep_blocked`` plus ``invoice.sweep_failed`` commit together (2026-10-12): the
    destination reverts on receipt, so the sweep's estimate reverts; with the watcher failing between the two, the
    sweep stays ``pending`` with no event and no second sweep; restarted without the failure, it fails once, the
    invoice is blocked and ``invoice.sweep_failed`` is recorded exactly once."""
    payer = HARNESS_ACCOUNTS[22].address
    reverting_destination = Account.create().address
    call_anvil(context.http_client, "anvil_setCode", reverting_destination, ALWAYS_REVERTING_CODE)
    restart_watcher_with_resolve_failures(context, "sweep:failed")
    try:
        invoice = create_invoice(
            context, amount="2", currency=NATIVE_SYMBOL, assets=[NATIVE_SYMBOL], destination_address=reverting_destination,
            allow_contract_destination=True, note="e2e sweep failure after a failed resolve",
        )  # fmt: skip
        invoice_id = str(invoice["id"])
        wait_for_receipt(
            context.http_client,
            send_transaction(context.http_client, sender=payer, to=str(invoice["deposit_address"]),
                             value_wei=2 * NATIVE_UNIT_WEI),
        )  # fmt: skip
        operation_id = wait_for_operation_id(context, invoice_id, kind="sweep")
        failures = wait_for_injected_resolve_failure(context, operation_id, timeout_seconds=180)
        time.sleep(4)
        status_while_failing = read_operation_status(context, operation_id)
        check(
            status_while_failing == "pending", f"sweep {operation_id} is {status_while_failing} while its resolve fails"
        )
        check(not read_invoice_flag(context, invoice_id, condition="sweep_blocked"), "sweep_blocked set on a rollback")
        check(
            count_account_events(context, invoice_id, event_type="invoice.sweep_failed") == 0,
            "invoice.sweep_failed recorded while the sweep's resolve rolled back",
        )
    finally:
        restart_watcher_with_resolve_failures(context, "")
    deadline = time.monotonic() + 120
    while read_operation_status(context, operation_id) != "failed":
        check(time.monotonic() < deadline, f"sweep {operation_id} did not fail after the restart")
        time.sleep(1)
    wait_for_account_events(context, invoice_id, expected_types={"invoice.sweep_failed"})
    time.sleep(5)  # a few more ticks: blocked, so no second sweep and no second event
    check(
        read_invoice_flag(context, invoice_id, condition="sweep_blocked"), "the failed sweep left sweep_blocked unset"
    )
    sweep_failed_events = count_account_events(context, invoice_id, event_type="invoice.sweep_failed")
    check(sweep_failed_events == 1, f"invoice.sweep_failed recorded {sweep_failed_events} times")
    statement = f"SELECT count(*) FROM outgoing_operations WHERE invoice_id = '{invoice_id}' AND kind = 'sweep'"  # noqa: S608
    sweep_count = int(run_sql(context, statement))
    check(sweep_count == 1, f"{sweep_count} sweeps recorded for a blocked invoice")
    return {"invoice_id": invoice_id, "operation_id": operation_id, "failed_resolves_logged": failures,
            "status_while_failing": status_while_failing,
            "account_events": read_account_event_types(context, invoice_id)}  # fmt: skip


def delete_invoice_rows(context: FlowContext, invoice_id: str) -> None:
    """The invoice and every row naming it, as if Postgres had lost them: no row keeps its salt."""
    check(re.fullmatch(r"inv_[0-9A-Za-z]+", invoice_id) is not None, f"not an invoice id: {invoice_id!r}")
    run_sql(
        context,
        "DO $$ DECLARE table_name_with_invoice text; BEGIN "
        "FOR table_name_with_invoice IN SELECT table_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND column_name = 'invoice_id' LOOP "
        f"EXECUTE format('DELETE FROM %I WHERE invoice_id = %L', table_name_with_invoice, '{invoice_id}'); END LOOP; "
        f"DELETE FROM invoices WHERE id = '{invoice_id}'; END $$",
    )


def read_recovered_field(output: str, field: str) -> str:
    """A ``\t<field>\t<value>`` line of ``recover-deposit``; for ``balance``, ``field`` is ``balance\t<symbol>``."""
    for line in output.splitlines():
        if line.startswith(f"\t{field}\t"):
            return line.removeprefix(f"\t{field}\t")
    raise FlowFailedError(f"recover-deposit printed no {field!r} line: {output[-800:]}")


def scenario_deposit_recovered_after_its_invoice_row_is_lost(context: FlowContext) -> dict[str, object]:
    destination = Account.create().address
    payer = HARNESS_ACCOUNTS[23].address
    third_party = HARNESS_ACCOUNTS[24].address
    native_payment_wei = NATIVE_UNIT_WEI
    # Stopped first, so nothing sweeps or records the deposit before its row is lost.
    run_watcher_compose_command(context, "stop")
    try:
        invoice = create_invoice(
            context, amount="5", currency="USD", assets=["USDC"], destination_address=destination,
            note="e2e deposit recovered without its row",
        )  # fmt: skip
        invoice_id = str(invoice["id"])
        deposit_address = str(invoice["deposit_address"])
        amount_base_units = to_base_units(str(invoice["amount_due"]), decimals=6)
        stored_salt_hex = run_sql(
            context, f"SELECT encode(invoice_salt, 'hex') FROM invoices WHERE id = '{invoice_id}'"
        )
        invoice_salt_seed = read_environment_setting(context, "PAY_INVOICE_SALT_SEED")
        derived_salt_hex = hmac.digest(bytes.fromhex(invoice_salt_seed), invoice_id.encode("ascii"), "sha256").hex()
        check(stored_salt_hex == derived_salt_hex, "the stored salt is not HMAC-SHA256(seed, invoice id)")
        fund_with_usdc(context.http_client, payer, base_units=amount_base_units)
        transfer_data = build_erc20_transfer_data(deposit_address, amount_base_units)
        wait_for_receipt(
            context.http_client,
            send_transaction(context.http_client, sender=payer, to=USDC_ADDRESS, data=transfer_data),
        )
        wait_for_receipt(
            context.http_client,
            send_transaction(context.http_client, sender=payer, to=deposit_address, value_wei=native_payment_wei),
        )
        delete_invoice_rows(context, invoice_id)
        remaining_rows = run_sql(
            context,
            f"SELECT count(*) FROM invoices WHERE id = '{invoice_id}' OR deposit_address = '{deposit_address}'",  # noqa: S608
        )
        check(remaining_rows == "0", f"{remaining_rows} invoice rows left after the wipe")
        api_log = run_command(
            compose_arguments(context.environment_file, "logs", "--no-color", "--no-log-prefix", "api"),
            environment=compose_environment(context.environment_file),
            log_path=context.scratch_folder / "commands.log",
        )
        check(invoice_salt_seed not in api_log and derived_salt_hex not in api_log, "the API log holds a seed or salt")
        captured_lines = [line for line in api_log.splitlines() if invoice_id in line and "invoice_create" in line]
        check(len(captured_lines) == 1, f"{len(captured_lines)} invoice_create lines for {invoice_id}")
        captured_record = json.loads(captured_lines[0])
        check(
            captured_record.get("deposit_address") == deposit_address
            and captured_record.get("destination_address") == destination,
            f"the invoice_create line names {captured_record}",
        )
        (context.scratch_folder / "recovery_invoice_create.jsonl").write_text(captured_lines[0] + "\n")
        # An unreachable database: recovery must not need one.
        exit_code, output = run_operator_cli(
            context, "recover-deposit", "--from-log", "-", input_text=captured_lines[0] + "\n",
            run_options=["-e", "DATABASE_URL=postgresql+psycopg://nobody:nothing@database-is-gone:5432/pay"],
        )  # fmt: skip
        check(exit_code == 0, f"recover-deposit exited {exit_code}: {output[-800:]}")
        check(f"deposit {deposit_address}" in output, f"recover-deposit predicted another address: {output[-800:]}")
        recovered_usdc = Decimal(read_recovered_field(output, "balance\tUSDC"))
        recovered_pol = Decimal(read_recovered_field(output, "balance\tPOL"))
        check(
            recovered_usdc == Decimal(amount_base_units).scaleb(-6) and recovered_pol == Decimal(1),
            f"recover-deposit shows {recovered_usdc} USDC and {recovered_pol} {NATIVE_SYMBOL}",
        )
        sweep_target = read_recovered_field(output, "sweep to")
        sweep_data = read_recovered_field(output, "sweep data")
        check(sweep_target == context.factory_address, f"sweep goes to {sweep_target}, not the factory")
        wait_for_receipt(
            context.http_client,
            send_transaction(context.http_client, sender=third_party, to=sweep_target, data=sweep_data, gas=600_000),
        )
        destination_usdc = read_usdc_balance(context.http_client, destination)
        destination_wei = read_native_balance(context.http_client, destination)
        check(destination_usdc == amount_base_units, f"destination holds {destination_usdc} USDC base units")
        check(destination_wei == native_payment_wei, f"destination holds {destination_wei} wei")
        check(
            read_usdc_balance(context.http_client, deposit_address) == 0
            and read_native_balance(context.http_client, deposit_address) == 0,
            "money left in the deposit after the sweep",
        )
        # By hand, after the sweep: the same address, nothing left.
        exit_code, typed_output = run_operator_cli(
            context,
            "recover-deposit",
            "--invoice-id",
            invoice_id,
            "--destination",
            destination,
            "--chain",
            FORK_CHAIN_KEY,
        )
        check(
            exit_code == 0 and f"deposit {deposit_address}" in typed_output and "nothing to sweep" in typed_output,
            f"recover-deposit by hand after the sweep: {exit_code} {typed_output[-800:]}",
        )
    finally:
        run_watcher_compose_command(context, "start")
    return {"invoice_id": invoice_id, "deposit_address": deposit_address, "recovered_usdc": str(recovered_usdc),
            "recovered_pol": str(recovered_pol), "destination_usdc_base_units": destination_usdc,
            "destination_wei": destination_wei}  # fmt: skip


DISCORD_ACTIVE_WEBHOOK_ID = "123456789012345678"
DISCORD_DELETED_WEBHOOK_ID = "876543210987654321"
# A forum channel's post: the active webhook's URL names it, and every message must carry it.
DISCORD_ACTIVE_THREAD_ID = "998877665544332211"
DISCORD_OPERATOR_WEBHOOK_ID = "112233445566778899"
# The API's public-write budget, 10 a minute per IP (GCRA), is full again a minute after the last write: the operator's
# passkey steps (enroll a passkey and set the Discord webhook, four writes; add a passkey and delete one, five, with
# three refused replays between) each start on a full budget.
PUBLIC_WRITE_BUDGET_REFILL_SECONDS = 60


def start_webhook_receiver(context: FlowContext) -> None:
    """The receiver answering 500 to the first ``invoice.paid`` of each webhook-id; as Discord, 429 once to the first
    "Paid" message and 404 to ``DISCORD_DELETED_WEBHOOK_ID``."""
    start_webhook_receiver_container(context, receiver_environment={
        "WEBHOOK_RECEIVER_FAIL_ONCE_TYPES": "invoice.paid",
        "DISCORD_RECEIVER_DELETED_WEBHOOK_IDS": DISCORD_DELETED_WEBHOOK_ID,
        "DISCORD_RECEIVER_RATE_LIMIT_ONCE_TEXT": "**Paid**",
    })  # fmt: skip


def check_api_token_invoice_routes(
    context: FlowContext, *, bearer_headers: dict[str, str], invoice_fields: dict[str, object]
) -> dict[str, object]:
    """Bearer create, a repeat with the same order_id, ``?order_id=``, a cookie-only route, an unknown token, and
    creates with only ``amount`` and ``currency`` (USD, then a POL amount)."""
    first_reply = context.http_client.post(f"{API_URL}/invoices", json=invoice_fields, headers=bearer_headers)
    check(first_reply.status_code == 201, f"bearer create: {first_reply.status_code} {first_reply.text}")
    invoice = first_reply.json()
    invoice_id = str(invoice["id"])
    repeat_reply = context.http_client.post(f"{API_URL}/invoices", json=invoice_fields, headers=bearer_headers)
    check(repeat_reply.status_code == 201 and repeat_reply.json()["id"] == invoice_id,
          f"repeated order_id: {repeat_reply.status_code} {repeat_reply.text[:200]}, expected the same invoice")  # fmt: skip
    found_reply = context.http_client.get(
        f"{API_URL}/invoices", params={"order_id": invoice_fields["order_id"]}, headers=bearer_headers
    )
    found_invoice_ids = [item["id"] for item in found_reply.json()["invoices"]]
    check(found_invoice_ids == [invoice_id], f"?order_id= found {found_invoice_ids}")
    cookie_only_reply = context.http_client.get(f"{API_URL}/account-api-tokens", headers=bearer_headers)
    check(cookie_only_reply.status_code == 401, f"bearer on a cookie-only route: {cookie_only_reply.status_code}")
    unknown_token_reply = context.http_client.get(
        f"{API_URL}/invoices/{invoice_id}", headers={"Authorization": f"Bearer pay_tok_{'A' * 43}"}
    )
    check(unknown_token_reply.status_code == 401
          and unknown_token_reply.headers.get("www-authenticate", "").startswith("Bearer"),
          f"unknown token: {unknown_token_reply.status_code}")  # fmt: skip
    minimal_reply = context.http_client.post(
        f"{API_URL}/invoices", json={"amount": "5.00", "currency": "USD"}, headers=bearer_headers
    )
    check(minimal_reply.status_code == 201,
          f"amount and currency only: {minimal_reply.status_code} {minimal_reply.text}")  # fmt: skip
    minimal_options = [(option["chain"]["key"], option["asset"]["symbol"])
                       for option in minimal_reply.json()["payment_options"]]  # fmt: skip
    check(bool(minimal_options) and all(chain_key == FORK_CHAIN_KEY for chain_key, _ in minimal_options),
          f"amount and currency only offered {minimal_options}, expected the account's coins on {FORK_CHAIN_KEY}")  # fmt: skip
    coin_amount_reply = context.http_client.post(
        f"{API_URL}/invoices", json={"amount": "1", "currency": "POL"}, headers=bearer_headers
    )
    check(coin_amount_reply.status_code == 201,
          f"POL amount only: {coin_amount_reply.status_code} {coin_amount_reply.text}")  # fmt: skip
    coin_amount_invoice = coin_amount_reply.json()
    coin_amount_pair = (coin_amount_invoice["chain"]["key"], (coin_amount_invoice["asset"] or {}).get("symbol"))
    check(coin_amount_pair == (FORK_CHAIN_KEY, "POL") and coin_amount_invoice["status"] == "awaiting_payment",
          f"POL amount only: {coin_amount_pair} {coin_amount_invoice['status']}, expected POL picked on {FORK_CHAIN_KEY}")  # fmt: skip
    return invoice


def wait_for_bearer_invoice_paid(
    context: FlowContext, *, invoice_id: str, bearer_headers: dict[str, str]
) -> dict[str, object]:
    deadline = time.monotonic() + SCENARIO_TIMEOUT_SECONDS
    while True:
        invoice_reply = context.http_client.get(f"{API_URL}/invoices/{invoice_id}", headers=bearer_headers)
        check(invoice_reply.status_code == 200, f"bearer read: {invoice_reply.status_code}")
        invoice = invoice_reply.json()
        if invoice["status"] == "paid":
            return invoice
        check(time.monotonic() < deadline, f"{invoice_id}: not paid in {SCENARIO_TIMEOUT_SECONDS} s")
        time.sleep(1)


def check_signed_webhook_deliveries(
    context: FlowContext, *, webhook_secret: str, invoice_fields: dict[str, object], invoice_id: str
) -> dict[str, object]:
    """``invoice.paid`` answered 500 then delivered again under the same webhook-id; every delivery verifies."""
    records = wait_for_webhook_records(
        context, description="invoice.paid delivered after a 500",
        condition=lambda records: [record["answered_status"]
                                   for record in select_webhook_records(records, event_type="invoice.paid")] == [500, 204],
    )  # fmt: skip
    paid_records = select_webhook_records(records, event_type="invoice.paid")
    test_records = select_webhook_records(records, event_type="webhook.test")
    check(len(test_records) == 1, f"{len(test_records)} webhook.test deliveries, expected 1")
    paid_webhook_ids = {str(record["headers"]["webhook-id"]) for record in paid_records}  # type: ignore[index]
    check(len(paid_webhook_ids) == 1, f"invoice.paid retried under webhook-ids {paid_webhook_ids}")
    webhook_verifier = Webhook(webhook_secret)
    for record in records:
        webhook_verifier.verify(str(record["body"]), dict(record["headers"]))  # type: ignore[call-overload]
    check(all(record["path"] == "/hooks/pay?source=e2e" for record in records), "a delivery lost the URL's path")
    paid_data = json.loads(str(paid_records[-1]["body"]))["data"]
    check(paid_data["id"] == invoice_id and paid_data["order_id"] == invoice_fields["order_id"]
          and paid_data["metadata"] == invoice_fields["metadata"] and paid_data["status"] == "paid",
          f"invoice.paid data: {paid_data.get('id')} {paid_data.get('order_id')} {paid_data.get('status')}")  # fmt: skip
    return {"invoice_paid_webhook_id": paid_webhook_ids.pop(),
            "invoice_paid_answered_statuses": [record["answered_status"] for record in paid_records],
            "received": [(read_webhook_record_type(record), record["answered_status"]) for record in records]}  # fmt: skip


PUBLIC_EVENT_ID_PATTERN = re.compile(r"evt_[1-9A-HJ-NP-Za-km-z]{22}")


def read_stream_until_caught_up(context: FlowContext, *, last_event_id: str) -> dict[str, object]:
    """Opens the operator account's event stream with that ``Last-Event-ID`` and reads to ``caught_up``: the SSE ids
    sent before it and its ``latest_event_id``."""
    sent_ids: list[str] = []
    stream_headers = {**context.account_headers, "Last-Event-ID": last_event_id}
    with context.http_client.stream("GET", f"{API_URL}/events/stream", headers=stream_headers, timeout=30) as reply:
        check(reply.status_code == 200, f"stream with Last-Event-ID {last_event_id!r}: {reply.status_code}")
        event_name = ""
        for line in reply.iter_lines():
            if line.startswith("event:"):
                event_name = line.removeprefix("event:").strip()
            elif line.startswith("id:"):
                sent_ids.append(line.removeprefix("id:").strip())
            elif line.startswith("data:") and event_name == "caught_up":
                return {"sent_ids": sent_ids, **json.loads(line.removeprefix("data:"))}
    raise FlowFailedError("the event stream ended before caught_up")


def check_public_event_ids(
    context: FlowContext, *, bearer_headers: dict[str, str], invoice_paid_webhook_id: str
) -> None:
    """Events go out under random ``evt_`` ids: the token's ``GET /events`` names ``invoice.paid`` by the webhook-id it
    was sent with and pages by the last event's id (an unknown one is 400); the stream catches up at once on a
    ``Last-Event-ID`` that is not an ``evt_`` id, starts over on an unknown one and resumes after one it sent."""
    events_reply = context.http_client.get(f"{API_URL}/events", headers=bearer_headers)
    check(events_reply.status_code == 200, f"GET /events by token: {events_reply.status_code}")
    event_ids = [str(event["id"]) for event in events_reply.json()["events"]]
    check(all(PUBLIC_EVENT_ID_PATTERN.fullmatch(event_id) for event_id in event_ids), f"event ids {event_ids[:5]}")
    check(invoice_paid_webhook_id in event_ids, f"webhook-id {invoice_paid_webhook_id} is not an event id")
    first_page_reply = context.http_client.get(f"{API_URL}/events", params={"limit": 1}, headers=bearer_headers)
    next_cursor = str(first_page_reply.json()["next_cursor"])
    first_page_event_id = first_page_reply.json()["events"][0]["id"]
    check(next_cursor == first_page_event_id and PUBLIC_EVENT_ID_PATTERN.fullmatch(next_cursor) is not None,
          f"next_cursor {next_cursor}, page event {first_page_event_id}")  # fmt: skip
    older_page_reply = context.http_client.get(f"{API_URL}/events", params={"limit": 1, "cursor": next_cursor},
                                               headers=bearer_headers)  # fmt: skip
    older_event_ids = [event["id"] for event in older_page_reply.json().get("events", [])]
    check(older_page_reply.status_code == 200 and len(older_event_ids) == 1 and older_event_ids[0] != next_cursor
          and older_event_ids[0] in event_ids, f"page after {next_cursor}: {older_page_reply.text[:200]}")  # fmt: skip
    unknown_cursor_reply = context.http_client.get(f"{API_URL}/events", params={"cursor": "evt_" + "1" * 22},
                                                   headers=bearer_headers)  # fmt: skip
    check(unknown_cursor_reply.status_code == 400, f"unknown event cursor: {unknown_cursor_reply.status_code}")
    # Not an evt_ id (a tab from before random ids): caught up at once, with no backlog.
    for malformed_last_event_id in ("1", "evt_not-an-id"):
        caught_up = read_stream_until_caught_up(context, last_event_id=malformed_last_event_id)
        check(caught_up["sent_ids"] == [] and caught_up["latest_event_id"] is None,
              f"Last-Event-ID {malformed_last_event_id!r}: {caught_up}")  # fmt: skip
    # Well formed but not one of the account's events: a first connect.
    caught_up = read_stream_until_caught_up(context, last_event_id="evt_" + "1" * 22)
    sent_ids = [str(sent_id) for sent_id in caught_up["sent_ids"]]  # type: ignore[union-attr]
    check(all(PUBLIC_EVENT_ID_PATTERN.fullmatch(sent_id) for sent_id in sent_ids), f"unknown id sent {sent_ids[:3]}")
    if caught_up["latest_event_id"] is not None:
        resumed = read_stream_until_caught_up(context, last_event_id=str(caught_up["latest_event_id"]))
        check(PUBLIC_EVENT_ID_PATTERN.fullmatch(str(resumed["latest_event_id"])) is not None,
              f"resumed after {caught_up['latest_event_id']}: {resumed}")  # fmt: skip


def scenario_api_token_invoice_paid_with_signed_webhook(context: FlowContext) -> dict[str, object]:
    """A fresh account makes an API token and sets a webhook on /settings; its server's bearer token creates an
    invoice with an order_id (a repeat returns the same one), the payment's ``invoice.paid`` arrives signed, again
    under the same webhook-id after a 500, and a revoked token gets 401."""
    payout_address = Account.create().address
    start_webhook_receiver(context)
    try:
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
                page.click("#settings_send_test_webhook_button")
                expect(page.get_by_test_id("settings_webhook_delivery_row").first).to_be_visible()
                bearer_headers = {"Authorization": f"Bearer {api_token}"}
                invoice_fields: dict[str, object] = {
                    "chain": FORK_CHAIN_KEY, "amount": "5", "currency": "USD", "assets": ["USDC"],
                    "note": "e2e api token", "order_id": f"shop-order-{secrets.token_hex(4)}",
                    "metadata": {"customer_id": 42},
                }  # fmt: skip
                invoice = check_api_token_invoice_routes(
                    context, bearer_headers=bearer_headers, invoice_fields=invoice_fields
                )
                invoice_id = str(invoice["id"])
                check(invoice["destination_address"] == payout_address, "token invoice does not pay the payout address")
                payer = API_TOKEN_SCENARIO_PAYER.address
                fund_with_usdc(context.http_client, payer, base_units=10 * USDC_BASE_UNITS)
                amount_base_units = to_base_units(str(invoice["amount_due"]), decimals=6)
                wait_for_receipt(context.http_client, send_transaction(
                    context.http_client, sender=payer, to=USDC_ADDRESS,
                    data=build_erc20_transfer_data(str(invoice["deposit_address"]), amount_base_units)))  # fmt: skip
                wait_for_bearer_invoice_paid(context, invoice_id=invoice_id, bearer_headers=bearer_headers)
                delivery_summary = check_signed_webhook_deliveries(
                    context, webhook_secret=webhook_secret, invoice_fields=invoice_fields, invoice_id=invoice_id
                )
                check_public_event_ids(context, bearer_headers=bearer_headers,
                                       invoice_paid_webhook_id=str(delivery_summary["invoice_paid_webhook_id"]))  # fmt: skip
                close_settings_sheet(page)
                page.click("#settings_api_tokens_row")
                page.get_by_test_id("settings_revoke_api_token_button").click()
                page.click("#settings_confirm_revoke_api_token_button")
                expect(page.get_by_test_id("settings_api_token_row")).to_have_count(0)
                revoked_reply = context.http_client.get(f"{API_URL}/invoices/{invoice_id}", headers=bearer_headers)
                check(revoked_reply.status_code == 401, f"revoked token: {revoked_reply.status_code}, expected 401")
                return {"invoice_id": invoice_id, "order_id": invoice_fields["order_id"], **delivery_summary}
            finally:
                browser.close()
    finally:
        stop_webhook_receiver()


def read_discord_records(records: list[dict[str, object]], *, discord_webhook_id: str) -> list[dict[str, object]]:
    return [record for record in records
            if record.get("discord") and f"/webhooks/{discord_webhook_id}/" in str(record["path"])]  # fmt: skip


def read_discord_content(record: dict[str, object]) -> str:
    return str(json.loads(str(record["body"]))["content"])


def set_discord_webhook_in_settings(
    page: Page, *, discord_webhook_id: str, discord_thread_id: str | None = None
) -> None:
    """Sets the Discord webhook in the Discord row's sheet, opening it unless it is open; the sheet stays open."""
    thread_query = f"?thread_id={discord_thread_id}" if discord_thread_id else ""
    if page.locator("#settings_set_discord_webhook_button").count() == 0:
        page.click("#settings_discord_row")
    page.click("#settings_set_discord_webhook_button")
    page.fill("#settings_discord_webhook_url_input",
              f"https://discord.com/api/webhooks/{discord_webhook_id}/e2e-{secrets.token_urlsafe(48)}{thread_query}")  # fmt: skip
    page.click("#settings_confirm_discord_webhook_button")
    expect(page.locator("#settings_discord_webhook_state")).to_contain_text(
        f"Webhook {discord_webhook_id}" + (f", thread {discord_thread_id}" if discord_thread_id else ""),
        timeout=PASSKEY_STEP_TIMEOUT_MILLISECONDS,
    )


def enroll_operator_passkey_on_page(context: FlowContext, *, page: Page) -> None:
    """The operator's account lost its only passkey with the browser that signed it up: the operator mints an
    enrollment link from the shell and the page adds a passkey through it, which signs the page in on /settings."""
    session_reply = context.http_client.get(f"{API_URL}/sessions/current", headers=context.account_headers)
    check(session_reply.status_code == 200, f"GET /sessions/current: {session_reply.status_code}")
    exit_code, output = run_operator_cli(context, "mint-passkey-enrollment-link", session_reply.json()["username"])
    check(exit_code == 0, f"mint-passkey-enrollment-link exited {exit_code}")
    link_match = re.search(r"/enroll-passkey#([A-Za-z0-9_-]{43})", output)
    if link_match is None:
        raise FlowFailedError("mint-passkey-enrollment-link printed no link")
    page.goto(f"{PUBLIC_BASE_URL}/enroll-passkey#{link_match.group(1)}")
    page.click("#enroll_passkey_button")
    page.wait_for_url(f"{PUBLIC_BASE_URL}/settings", timeout=PASSKEY_STEP_TIMEOUT_MILLISECONDS)


def add_and_delete_operator_passkey_in_settings(
    context: FlowContext, *, page: Page, cdp_session: CDPSession, authenticator_id: str
) -> None:
    """Adds a passkey and deletes the one the page signed in with, each after a passkey check of the account. The
    page's one authenticator approves the addition with the enrolled passkey, then hands it over (the new passkey's
    options exclude the account's passkeys, so it would refuse to make another): as a second device would, it holds
    only the new passkey, which then approves deleting the enrolled one. That signs the page out. In between, the
    stack's session (the cookie alone, on another passkey of the account) replays the addition's approval: spent, it
    adds nothing, and it deletes nothing either (a check made for another purpose); with no approval at all the
    registration options are refused outright."""
    approval_credentials: list[object] = []

    def drop_approving_passkey_before_registration(route: Route) -> None:
        approval_credentials.append(route.request.post_data_json["credential"])
        cdp_session.send("WebAuthn.clearCredentials", {"authenticatorId": authenticator_id})
        route.continue_()

    page.route("**/api/v1/account-passkeys/registration-options", drop_approving_passkey_before_registration, times=1)
    page.click("#settings_passkeys_row")
    passkey_rows = page.get_by_test_id("settings_passkey_row")
    expect(passkey_rows).to_have_count(2, timeout=PASSKEY_STEP_TIMEOUT_MILLISECONDS)
    page.click("#settings_add_passkey_button")
    expect(passkey_rows).to_have_count(3, timeout=PASSKEY_STEP_TIMEOUT_MILLISECONDS)
    check_passkey_changes_refused_on_session_alone(context, approval_credential=approval_credentials[0])
    enrolled_passkey_row = passkey_rows.filter(has=page.get_by_test_id("settings_current_passkey_badge"))
    enrolled_passkey_row.get_by_test_id("settings_delete_passkey_button").click()
    page.get_by_test_id("settings_confirm_delete_passkey_button").click()
    expect(page.locator("#gate_sign_in_button")).to_be_visible(timeout=PASSKEY_STEP_TIMEOUT_MILLISECONDS)
    passkeys_reply = context.http_client.get(f"{API_URL}/account-passkeys", headers=context.account_headers)
    check(passkeys_reply.status_code == 200, f"GET /account-passkeys: {passkeys_reply.status_code}")
    remaining_passkeys = passkeys_reply.json()["passkeys"]
    check(len(remaining_passkeys) == 2, f"{len(remaining_passkeys)} passkeys left, expected 2")
    events_reply = context.http_client.get(f"{API_URL}/events", headers=context.account_headers)
    check(events_reply.status_code == 200, f"GET /events: {events_reply.status_code}")
    account_event_types = [event["type"] for event in events_reply.json()["events"]]
    for expected_type in ("account.passkey_enrolled_by_link", "account.passkey_added", "account.passkey_deleted"):
        check(expected_type in account_event_types, f"no {expected_type} event: {account_event_types[:10]}")
    # The session alone no longer deletes a passkey: the old route is gone.
    delete_reply = context.http_client.delete(
        f"{API_URL}/account-passkeys/{remaining_passkeys[0]['id']}", headers=context.account_headers
    )
    check(delete_reply.status_code == 405, f"DELETE /account-passkeys/{{id}} answered {delete_reply.status_code}")


def check_passkey_changes_refused_on_session_alone(context: FlowContext, *, approval_credential: object) -> None:
    """The stack's session cookie, with an addition's approval it did not sign and that was already used: no new
    passkey's options (spent), no deletion (made for another purpose), and none without an approval."""
    replayed_reply = context.http_client.post(f"{API_URL}/account-passkeys/registration-options",
                                              json={"credential": approval_credential}, headers=context.account_headers)  # fmt: skip
    check(replayed_reply.status_code == 400 and replayed_reply.json().get("code") == "passkey_challenge_expired",
          f"replayed approval for new passkey options: {replayed_reply.status_code} {replayed_reply.text[:200]}")  # fmt: skip
    passkeys_reply = context.http_client.get(f"{API_URL}/account-passkeys", headers=context.account_headers)
    session_passkey_id = next(passkey["id"] for passkey in passkeys_reply.json()["passkeys"]
                              if passkey["used_by_current_session"])  # fmt: skip
    deletion_reply = context.http_client.post(f"{API_URL}/account-passkeys/{session_passkey_id}/deletion",
                                              json={"credential": approval_credential}, headers=context.account_headers)  # fmt: skip
    check(deletion_reply.status_code == 400,
          f"addition approval sent to delete a passkey: {deletion_reply.status_code} {deletion_reply.text[:200]}")  # fmt: skip
    unapproved_reply = context.http_client.post(f"{API_URL}/account-passkeys/registration-options", json={},
                                                headers=context.account_headers)  # fmt: skip
    # A request body that fails validation is 400 `validation_error` here (app/shared/exception_handlers.py), not 422.
    check(unapproved_reply.status_code == 400 and unapproved_reply.json().get("code") == "validation_error",
          f"new passkey options with no approval: {unapproved_reply.status_code} {unapproved_reply.text[:200]}")  # fmt: skip


def check_operator_alert_reaches_discord(context: FlowContext, *, browser: Browser) -> str:
    """The operator's account (the stack's, its passkey gone with the browser that signed it up) gets a passkey
    through an enrollment link and sets a Discord webhook with it, then adds and deletes a passkey on /settings; a host
    alert recorded by the CLI arrives at the webhook. Answers its content."""
    time.sleep(PUBLIC_WRITE_BUDGET_REFILL_SECONDS)
    operator_browser_context = browser.new_context()
    try:
        page = operator_browser_context.new_page()
        cdp_session = page.context.new_cdp_session(page)
        cdp_session.send("WebAuthn.enable")
        authenticator_id = str(
            cdp_session.send("WebAuthn.addVirtualAuthenticator", {"options": VIRTUAL_AUTHENTICATOR_OPTIONS})[
                "authenticatorId"
            ]
        )
        enroll_operator_passkey_on_page(context, page=page)
        set_discord_webhook_in_settings(page, discord_webhook_id=DISCORD_OPERATOR_WEBHOOK_ID)
        close_settings_sheet(page)
        time.sleep(PUBLIC_WRITE_BUDGET_REFILL_SECONDS)
        add_and_delete_operator_passkey_in_settings(
            context, page=page, cdp_session=cdp_session, authenticator_id=authenticator_id
        )
    finally:
        operator_browser_context.close()
    exit_code, output = run_operator_cli(context, "record-operator-alert", "--kind", "backup_failed")
    check(exit_code == 0, f"record-operator-alert exited {exit_code}: {output[-500:]}")
    records = wait_for_webhook_records(context, description="the operator alert's Discord message", condition=lambda records: any(
        "**Needs the operator**" in read_discord_content(record) and record["answered_status"] == 204
        for record in read_discord_records(records, discord_webhook_id=DISCORD_OPERATOR_WEBHOOK_ID)))  # fmt: skip
    alert_record = next(record for record in read_discord_records(records, discord_webhook_id=DISCORD_OPERATOR_WEBHOOK_ID)
                        if "**Needs the operator**" in read_discord_content(record))  # fmt: skip
    alert_body = json.loads(str(alert_record["body"]))
    alert_content = str(alert_body["content"])
    check(alert_body["allowed_mentions"] == {"parse": []}, f"alert allowed_mentions {alert_body['allowed_mentions']}")
    check(alert_content.startswith("**Needs the operator** (host, backup failed): The hourly Postgres backup"),
          f"alert content: {alert_content}")  # fmt: skip
    check(alert_body["username"] == "pay-7331", f"alert username {alert_body.get('username')}")
    check(alert_content.endswith(f"Reminder every 1 h while it lasts (Settings, Discord) · {PUBLIC_BASE_URL}/activity"),
          f"alert content: {alert_content}")  # fmt: skip
    check("`journalctl -u pay-7331-backup`" in alert_content, f"alert command not left as code: {alert_content}")
    check(len(alert_content) <= 2000 and "0x" not in alert_content, f"alert content: {alert_content}")
    return alert_content


def scenario_discord_messages_for_invoice_events(context: FlowContext) -> dict[str, object]:
    """A fresh account sets a Discord webhook on /settings and sends a test message; its paid invoice's "Paid" line
    arrives (429 once, then sent after ``retry_after``) with no mentions parsed, into the thread its URL named; a
    webhook Discord answers a bare 404 for is marked deleted and /settings says so. Then an operator alert reaches the operator account's own Discord webhook."""
    payout_address = Account.create().address
    start_webhook_receiver(context)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_context().new_page()
                add_virtual_authenticator(page)
                session_token = sign_up_account_on_page(page, public_base_url=PUBLIC_BASE_URL,
                                                        username=f"e2e_{secrets.token_hex(4)}",
                                                        payout_address=payout_address)  # fmt: skip
                account_headers = account_request_headers(session_token, public_base_url=PUBLIC_BASE_URL)
                page.goto(f"{PUBLIC_BASE_URL}/settings")
                set_discord_webhook_in_settings(page, discord_webhook_id=DISCORD_ACTIVE_WEBHOOK_ID,
                                                discord_thread_id=DISCORD_ACTIVE_THREAD_ID)  # fmt: skip
                page.click("#settings_send_test_discord_message_button")
                wait_for_webhook_records(context, description="the Discord test message", condition=lambda records: any(
                    "Test message" in read_discord_content(record) and record["answered_status"] == 204
                    for record in read_discord_records(records, discord_webhook_id=DISCORD_ACTIVE_WEBHOOK_ID)))  # fmt: skip
                note = "e2e @everyone *discord* <@123>"
                reply = context.http_client.post(f"{API_URL}/invoices", headers=account_headers, json={
                    "chain": FORK_CHAIN_KEY, "amount": "5", "currency": "USD", "assets": ["USDC"], "note": note})  # fmt: skip
                check(reply.status_code == 201, f"create invoice: {reply.status_code} {reply.text}")
                invoice = reply.json()
                invoice_id = str(invoice["id"])
                payer = DISCORD_SCENARIO_PAYER.address
                fund_with_usdc(context.http_client, payer, base_units=10 * USDC_BASE_UNITS)
                wait_for_receipt(context.http_client, send_transaction(
                    context.http_client, sender=payer, to=USDC_ADDRESS,
                    data=build_erc20_transfer_data(str(invoice["deposit_address"]),
                                                   to_base_units(str(invoice["amount_due"]), decimals=6))))  # fmt: skip
                records = wait_for_webhook_records(context, description="the Paid message after a 429", condition=lambda records: [
                    record["answered_status"] for record in read_discord_records(records, discord_webhook_id=DISCORD_ACTIVE_WEBHOOK_ID)
                    if "**Paid**" in read_discord_content(record)] == [429, 204])  # fmt: skip
                active_records = read_discord_records(records, discord_webhook_id=DISCORD_ACTIVE_WEBHOOK_ID)
                paid_record = next(record for record in active_records
                                   if "**Paid**" in read_discord_content(record) and record["answered_status"] == 204)  # fmt: skip
                paid_body = json.loads(str(paid_record["body"]))
                paid_content = str(paid_body["content"])
                check(
                    paid_body["allowed_mentions"] == {"parse": []}, f"allowed_mentions {paid_body['allowed_mentions']}"
                )
                check(paid_content.startswith("**Paid**: 5.00 USD (5.00 USDC)") and f"/i/{invoice_id}" in paid_content,
                      f"Paid content: {paid_content}")  # fmt: skip
                check("\\@everyone \\*discord\\* \\<\\@123\\>" in paid_content, f"note not escaped: {paid_content}")
                check(
                    all("0x" not in read_discord_content(record) for record in active_records),
                    "an address in a message",
                )
                check(all(str(record["user_agent"]).startswith("DiscordBot (") for record in active_records),
                      "a message without a DiscordBot User-Agent")  # fmt: skip
                check(all(f"thread_id={DISCORD_ACTIVE_THREAD_ID}" in str(record["path"]) for record in active_records),
                      "a message to the active webhook without its thread id")  # fmt: skip
                set_discord_webhook_in_settings(page, discord_webhook_id=DISCORD_DELETED_WEBHOOK_ID)
                page.click("#settings_send_test_discord_message_button")
                wait_for_webhook_records(context, description="the 404 from the deleted Discord webhook", condition=lambda records: any(
                    record["answered_status"] == 404
                    for record in read_discord_records(records, discord_webhook_id=DISCORD_DELETED_WEBHOOK_ID)))  # fmt: skip
                page.reload()
                expect(page.get_by_test_id("settings_discord_row_value")).to_have_text(
                    "Gone in Discord", timeout=SCENARIO_TIMEOUT_SECONDS * 1000
                )
                page.click("#settings_discord_row")
                expect(page.locator("#settings_discord_webhook_deleted")).to_be_visible(
                    timeout=SCENARIO_TIMEOUT_SECONDS * 1000
                )
                expect(page.locator("#settings_send_test_discord_message_button")).to_have_count(0)
                test_reply = context.http_client.post(
                    f"{API_URL}/account-discord-webhook/test-messages", headers=account_headers
                )
                check(test_reply.status_code == 409, f"test message to a deleted webhook: {test_reply.status_code}")
                deleted_records = read_discord_records(read_webhook_receiver_records(),
                                                       discord_webhook_id=DISCORD_DELETED_WEBHOOK_ID)  # fmt: skip
                check(len(deleted_records) == 1, f"{len(deleted_records)} requests to the deleted webhook, expected 1")
                operator_alert_content = check_operator_alert_reaches_discord(context, browser=browser)
                return {"invoice_id": invoice_id, "paid_content": paid_content,
                        "operator_alert_content": operator_alert_content,
                        "received": [(read_discord_content(record)[:40], record["answered_status"])
                                     for record in read_webhook_receiver_records() if record.get("discord")]}  # fmt: skip
            finally:
                browser.close()
    finally:
        stop_webhook_receiver()


def create_account_invoice_reply(context: FlowContext, **fields: object) -> httpx.Response:
    """``POST /invoices`` by the stack's account session, answered as it comes (``create_invoice`` raises on a refusal)."""
    return context.http_client.post(
        f"{API_URL}/invoices", json={"chain": FORK_CHAIN_KEY, **fields}, headers=context.account_headers
    )


def scenario_refreshed_order_paid_and_late_money_on_the_old_deposit(context: FlowContext) -> dict[str, object]:
    """``refresh_expired`` (2026-10-21): a live invoice comes back unchanged; once it expired, a new invoice with a new
    deposit replaces it for the order (``?order_id=`` finds the new one, the two point at each other) and is paid;
    POL sent afterwards to the old deposit is late money on the old invoice, swept to the destination, and the new
    invoice's amounts do not move. An order whose expired invoice got late money is refused 409
    ``invoice_not_refreshable``."""
    destination = Account.create().address
    payer = REFRESH_SCENARIO_PAYER.address
    order_id = f"e2e-refresh-{secrets.token_hex(4)}"
    invoice_fields: dict[str, object] = {
        "amount": "1", "currency": NATIVE_SYMBOL, "assets": [NATIVE_SYMBOL], "destination_address": destination,
        "note": "e2e refresh", "lifetime_seconds": 300, "order_id": order_id,
    }  # fmt: skip
    old_invoice = create_invoice(context, **invoice_fields)
    old_invoice_id = str(old_invoice["id"])
    live_refresh = create_invoice(context, **invoice_fields, refresh_expired=True)
    check(live_refresh["id"] == old_invoice_id, f"refresh of a live invoice made {live_refresh['id']}")
    expire_invoice_now(context, invoice_id=old_invoice_id)
    new_invoice = create_invoice(context, **invoice_fields, refresh_expired=True)
    new_invoice_id = str(new_invoice["id"])
    check(new_invoice_id != old_invoice_id and new_invoice["status"] == "awaiting_payment",
          f"refresh answered {new_invoice_id} {new_invoice['status']}")  # fmt: skip
    check(new_invoice["refreshed_from_invoice_id"] == old_invoice_id and new_invoice["order_id"] == order_id,
          f"new invoice links {new_invoice['refreshed_from_invoice_id']} {new_invoice['order_id']}")  # fmt: skip
    check(new_invoice["deposit_address"] != old_invoice["deposit_address"], "the refresh kept the old deposit")
    old_invoice = read_invoice(context, old_invoice_id)
    check(old_invoice["refreshed_into_invoice_id"] == new_invoice_id and old_invoice["status"] == "expired",
          f"old invoice: {old_invoice['refreshed_into_invoice_id']} {old_invoice['status']}")  # fmt: skip
    found_reply = context.http_client.get(
        f"{API_URL}/invoices", params={"order_id": order_id}, headers=context.account_headers
    )
    found_invoice_ids = [item["id"] for item in found_reply.json()["invoices"]]
    check(found_invoice_ids == [new_invoice_id], f"?order_id= found {found_invoice_ids}")
    repeated_refresh = create_invoice(context, **invoice_fields, refresh_expired=True)
    check(repeated_refresh["id"] == new_invoice_id, f"a second refresh made {repeated_refresh['id']}")
    wait_for_receipt(context.http_client, send_transaction(
        context.http_client, sender=payer, to=str(new_invoice["deposit_address"]), value_wei=NATIVE_UNIT_WEI))  # fmt: skip
    new_invoice = wait_for_invoice(context, new_invoice_id, description="the refreshed invoice paid and swept",
                                   condition=lambda current: current["status"] == "paid" and last_sweep_final(current))  # fmt: skip
    wait_for_receipt(context.http_client, send_transaction(
        context.http_client, sender=payer, to=str(old_invoice["deposit_address"]), value_wei=NATIVE_UNIT_WEI))  # fmt: skip
    old_invoice = wait_for_invoice(context, old_invoice_id, description="late money on the old deposit swept",
                                   condition=lambda current: bool(current["late_payment"]) and last_sweep_final(current))  # fmt: skip
    check(old_invoice["status"] == "expired" and to_base_units(str(old_invoice["amount_received"]), decimals=18) == 0,
          f"old invoice after late money: {old_invoice['status']} {old_invoice['amount_received']}")  # fmt: skip
    new_invoice_after = read_invoice(context, new_invoice_id)
    check(new_invoice_after["amount_received"] == new_invoice["amount_received"] == new_invoice["amount_due"]
          and new_invoice_after["status"] == "paid" and not new_invoice_after["late_payment"],
          f"new invoice after the old one's late money: {new_invoice_after['amount_received']} "
          f"{new_invoice_after['status']} late={new_invoice_after['late_payment']}")  # fmt: skip
    destination_balance = read_native_balance(context.http_client, destination)
    check(destination_balance == 2 * NATIVE_UNIT_WEI, f"destination holds {destination_balance} wei, expected 2 POL")
    old_event_types = wait_for_account_events(
        context, old_invoice_id, expected_types={"invoice.expired", "invoice.late_payment", "invoice.swept"}
    )
    paid_order_refresh = create_invoice(context, **invoice_fields, refresh_expired=True)
    check(paid_order_refresh["id"] == new_invoice_id, f"refresh of a paid order made {paid_order_refresh['id']}")
    late_order_fields = {**invoice_fields, "order_id": f"{order_id}-late"}
    late_invoice = create_invoice(context, **late_order_fields)
    late_invoice_id = str(late_invoice["id"])
    expire_invoice_now(context, invoice_id=late_invoice_id)
    wait_for_receipt(context.http_client, send_transaction(
        context.http_client, sender=payer, to=str(late_invoice["deposit_address"]), value_wei=NATIVE_UNIT_WEI))  # fmt: skip
    wait_for_invoice(context, late_invoice_id, description="late money flagged",
                     condition=lambda current: bool(current["late_payment"]))  # fmt: skip
    refused_reply = create_account_invoice_reply(context, **late_order_fields, refresh_expired=True)
    check(refused_reply.status_code == 409 and refused_reply.json()["code"] == "invoice_not_refreshable",
          f"refresh after late money: {refused_reply.status_code} {refused_reply.text[:200]}")  # fmt: skip
    return {"old_invoice_id": old_invoice_id, "new_invoice_id": new_invoice_id, "destination_wei": destination_balance,
            "old_invoice_events": old_event_types, "refused_refresh": refused_reply.json()["code"]}  # fmt: skip


def check_token_invoice_actions(context: FlowContext, *, bearer_headers: dict[str, str]) -> dict[str, object]:
    """By the token: cancel an unpaid invoice, and accept a short USDC payment as paid."""
    base_fields: dict[str, object] = {"chain": FORK_CHAIN_KEY, "currency": "USD", "assets": ["USDC"]}
    cancelled_reply = context.http_client.post(
        f"{API_URL}/invoices", json={**base_fields, "amount": "5", "note": "e2e token cancel"}, headers=bearer_headers
    )
    check(cancelled_reply.status_code == 201, f"token create: {cancelled_reply.status_code} {cancelled_reply.text}")
    cancelled_invoice_id = str(cancelled_reply.json()["id"])
    cancel_reply = context.http_client.post(f"{API_URL}/invoices/{cancelled_invoice_id}/cancel", headers=bearer_headers)
    check(cancel_reply.status_code == 200 and cancel_reply.json()["status"] == "cancelled",
          f"token cancel: {cancel_reply.status_code} {cancel_reply.text[:200]}")  # fmt: skip
    partial_reply = context.http_client.post(
        f"{API_URL}/invoices", json={**base_fields, "amount": "5", "note": "e2e token partial"}, headers=bearer_headers
    )
    partial_invoice = partial_reply.json()
    partial_invoice_id = str(partial_invoice["id"])
    payer = MERCHANT_ROUTES_SCENARIO_PAYER.address
    fund_with_usdc(context.http_client, payer, base_units=20 * USDC_BASE_UNITS)
    wait_for_receipt(context.http_client, send_transaction(
        context.http_client, sender=payer, to=USDC_ADDRESS,
        data=build_erc20_transfer_data(str(partial_invoice["deposit_address"]), 2 * USDC_BASE_UNITS)))  # fmt: skip
    wait_for_token_invoice(context, invoice_id=partial_invoice_id, bearer_headers=bearer_headers,
                           condition=lambda current: current["status"] == "underpaid")  # fmt: skip
    accept_reply = context.http_client.post(
        f"{API_URL}/invoices/{partial_invoice_id}/accept-partial", headers=bearer_headers
    )
    accepted_invoice = accept_reply.json()
    check(accept_reply.status_code == 200 and accepted_invoice["status"] == "paid" and accepted_invoice["partial"],
          f"token accept-partial: {accept_reply.status_code} {accept_reply.text[:200]}")  # fmt: skip
    refund_reply = context.http_client.post(
        f"{API_URL}/invoices/{partial_invoice_id}/refunds",
        json={"sender": payer, "amount": "1"},
        headers=bearer_headers,
    )
    check(refund_reply.status_code == 401, f"token on the float refund route: {refund_reply.status_code}")
    webhook_change_reply = context.http_client.post(f"{API_URL}/account-webhook/change-options", json={},
                                                    headers=bearer_headers)  # fmt: skip
    check(webhook_change_reply.status_code == 401, f"token on the webhook change: {webhook_change_reply.status_code}")
    return {"cancelled_invoice_id": cancelled_invoice_id, "accepted_partial_invoice_id": partial_invoice_id}


def wait_for_token_invoice(
    context: FlowContext,
    *,
    invoice_id: str,
    bearer_headers: dict[str, str],
    condition: Callable[[dict[str, object]], bool],
) -> dict[str, object]:
    deadline = time.monotonic() + SCENARIO_TIMEOUT_SECONDS
    while True:
        invoice_reply = context.http_client.get(f"{API_URL}/invoices/{invoice_id}", headers=bearer_headers)
        check(invoice_reply.status_code == 200, f"bearer read: {invoice_reply.status_code}")
        invoice = invoice_reply.json()
        if condition(invoice):
            return invoice
        check(
            time.monotonic() < deadline, f"{invoice_id}: still {invoice['status']} after {SCENARIO_TIMEOUT_SECONDS} s"
        )
        time.sleep(1)


def check_token_payment_link_routes(context: FlowContext, *, bearer_headers: dict[str, str]) -> str:
    """By the token: create a quantity link, list it, read it and close it."""
    create_reply = context.http_client.post(f"{API_URL}/payment-links", headers=bearer_headers, json={
        "chain": FORK_CHAIN_KEY, "amount": "5", "currency": "USD", "assets": ["USDC"], "quantity": 2,
        "note": "e2e token link"})  # fmt: skip
    check(create_reply.status_code == 201, f"token link create: {create_reply.status_code} {create_reply.text}")
    payment_link_id = str(create_reply.json()["id"])
    list_reply = context.http_client.get(f"{API_URL}/payment-links", headers=bearer_headers)
    listed_ids = [payment_link["id"] for payment_link in list_reply.json()["payment_links"]]
    check(payment_link_id in listed_ids, f"token link list: {list_reply.status_code} {listed_ids}")
    read_reply = context.http_client.get(f"{API_URL}/payment-links/{payment_link_id}", headers=bearer_headers)
    check(read_reply.status_code == 200 and read_reply.json()["slots_left"] == 2,
          f"token link read: {read_reply.status_code} {read_reply.text[:200]}")  # fmt: skip
    close_reply = context.http_client.post(f"{API_URL}/payment-links/{payment_link_id}/close", headers=bearer_headers)
    check(close_reply.status_code == 200 and close_reply.json()["status"] == "closed",
          f"token link close: {close_reply.status_code} {close_reply.text[:200]}")  # fmt: skip
    return payment_link_id


# The webhook's event rule, as backend/app/payments/account_events/webhook_event_scope.py writes it (this script talks
# to the API from outside and cannot import it): keep the prefixes the same as there.
WEBHOOK_EVENT_TYPE_PREFIXES_WITHOUT_INVOICE: tuple[str, ...] = ("static_address.",)


def is_event_the_webhook_sends(event: dict[str, object]) -> bool:
    return event["invoice_id"] is not None or str(event["type"]).startswith(WEBHOOK_EVENT_TYPE_PREFIXES_WITHOUT_INVOICE)


def check_invoice_callback_delivered(
    context: FlowContext, *, webhook_secret: str, invoice_id: str, callback_path: str
) -> dict[str, object]:
    """``invoice.paid`` reaches the invoice's callback URL (after the receiver's one 500 for its webhook-id, whichever
    copy took it) signed with the account webhook's secret, under the same webhook-id as the account webhook's copy."""

    def select_this_invoice_paid_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
        """The accept-partial invoice's own ``invoice.paid`` goes to the webhook too: only this invoice's count."""
        return [record for record in select_webhook_records(records, event_type="invoice.paid")
                if json.loads(str(record["body"]))["data"]["id"] == invoice_id]  # fmt: skip

    def paid_delivered_to_both(records: list[dict[str, object]]) -> bool:
        paid_records = select_this_invoice_paid_records(records)
        return any(
            record["path"] == callback_path and record["answered_status"] == 204 for record in paid_records
        ) and any(
            record["path"] == WEBHOOK_RECEIVER_PATH and record["answered_status"] == 204 for record in paid_records
        )

    records = wait_for_webhook_records(context, description="invoice.paid at the callback and the webhook",
                                       condition=paid_delivered_to_both)  # fmt: skip
    webhook_verifier = Webhook(webhook_secret)
    for record in records:
        webhook_verifier.verify(str(record["body"]), dict(record["headers"]))  # type: ignore[call-overload]
    callback_records = [record for record in records if record["path"] == callback_path]
    check(all(json.loads(str(record["body"]))["data"]["id"] == invoice_id for record in callback_records),
          "a callback for another invoice")  # fmt: skip
    paid_webhook_ids = {str(record["headers"]["webhook-id"])  # type: ignore[index]
                        for record in select_this_invoice_paid_records(records)}  # fmt: skip
    check(len(paid_webhook_ids) == 1, f"invoice.paid went out under webhook-ids {paid_webhook_ids}")
    return {"callback_received": [(read_webhook_record_type(record), record["answered_status"])
                                  for record in callback_records],
            "invoice_paid_webhook_id": paid_webhook_ids.pop()}  # fmt: skip


def scenario_api_token_merchant_routes_and_invoice_callback(context: FlowContext) -> dict[str, object]:
    """A fresh account's API token (2026-10-21): a ``callback_url`` is refused before the account has a webhook and
    taken after; the invoice's answer carries ``return_url``, ``success_url`` and ``callback_url`` (the payer's view no
    callback); paid, ``invoice.paid`` reaches the callback signed with the webhook secret beside the webhook's own copy.
    The token cancels an invoice, accepts a short payment, makes, lists, reads and closes a quantity link and reads
    only invoice events; the float refund and webhook routes still refuse it."""
    payout_address = Account.create().address
    start_webhook_receiver(context)
    try:
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
                bearer_headers = {"Authorization": f"Bearer {api_token}"}
                callback_path = f"/hooks/callback?order=e2e-{secrets.token_hex(4)}"
                invoice_fields: dict[str, object] = {
                    "chain": FORK_CHAIN_KEY, "amount": "5", "currency": "USD", "assets": ["USDC"],
                    "note": "e2e callback", "return_url": "https://shop.example/cart",
                    "success_url": "https://shop.example/thanks",
                    "callback_url": f"http://{WEBHOOK_RECEIVER_CONTAINER}:8080{callback_path}",
                }  # fmt: skip
                no_webhook_reply = context.http_client.post(f"{API_URL}/invoices", json=invoice_fields,
                                                            headers=bearer_headers)  # fmt: skip
                check(no_webhook_reply.status_code == 409 and no_webhook_reply.json()["code"] == "webhook_not_set",
                      f"callback_url with no webhook: {no_webhook_reply.status_code} {no_webhook_reply.text[:200]}")  # fmt: skip
                webhook_secret = set_webhook_in_settings(page)
                close_settings_sheet(page)
                bad_callback_reply = context.http_client.post(
                    f"{API_URL}/invoices", json={**invoice_fields, "callback_url": "ftp://shop.example/hook"},
                    headers=bearer_headers)  # fmt: skip
                check(bad_callback_reply.status_code == 400,
                      f"ftp callback_url: {bad_callback_reply.status_code} {bad_callback_reply.text[:200]}")  # fmt: skip
                create_reply = context.http_client.post(
                    f"{API_URL}/invoices", json=invoice_fields, headers=bearer_headers
                )
                check(create_reply.status_code == 201, f"token create: {create_reply.status_code} {create_reply.text}")
                invoice = create_reply.json()
                invoice_id = str(invoice["id"])
                check((invoice["return_url"], invoice["success_url"], invoice["callback_url"])
                      == (invoice_fields["return_url"], invoice_fields["success_url"], invoice_fields["callback_url"]),
                      f"account view urls: {invoice['return_url']} {invoice['success_url']} {invoice['callback_url']}")  # fmt: skip
                public_invoice = read_public_invoice(context, invoice_id)
                check(public_invoice["success_url"] == invoice_fields["success_url"]
                      and public_invoice["return_url"] == invoice_fields["return_url"]
                      and "callback_url" not in public_invoice,
                      f"public view urls: {sorted(key for key in public_invoice if key.endswith('_url'))}")  # fmt: skip
                action_summary = check_token_invoice_actions(context, bearer_headers=bearer_headers)
                payment_link_id = check_token_payment_link_routes(context, bearer_headers=bearer_headers)
                payer = MERCHANT_ROUTES_SCENARIO_PAYER.address
                wait_for_receipt(context.http_client, send_transaction(
                    context.http_client, sender=payer, to=USDC_ADDRESS,
                    data=build_erc20_transfer_data(str(invoice["deposit_address"]),
                                                   to_base_units(str(invoice["amount_due"]), decimals=6))))  # fmt: skip
                wait_for_token_invoice(context, invoice_id=invoice_id, bearer_headers=bearer_headers,
                                       condition=lambda current: current["status"] == "paid")  # fmt: skip
                callback_summary = check_invoice_callback_delivered(
                    context, webhook_secret=webhook_secret, invoice_id=invoice_id, callback_path=callback_path
                )
                events_reply = context.http_client.get(f"{API_URL}/events", params={"limit": 200},
                                                       headers=bearer_headers)  # fmt: skip
                check(events_reply.status_code == 200, f"token events: {events_reply.status_code}")
                events = events_reply.json()["events"]
                event_pairs = {(event["invoice_id"], event["type"]) for event in events}
                check(all(is_event_the_webhook_sends(event) for event in events),
                      f"a token read an event the webhook does not send: {sorted(event_pairs)}")  # fmt: skip
                check((action_summary["cancelled_invoice_id"], "invoice.cancelled") in event_pairs
                      and (invoice_id, "invoice.paid") in event_pairs,
                      f"token events: {sorted(event_pairs)}")  # fmt: skip
                return {"invoice_id": invoice_id, "payment_link_id": payment_link_id, **action_summary,
                        **callback_summary, "token_event_count": len(events)}  # fmt: skip
            finally:
                browser.close()
    finally:
        stop_webhook_receiver()


SCENARIOS: list[Callable[[FlowContext], dict[str, object]]] = [
    scenario_reconcile_alerts_and_watcher_health,
    scenario_usdc_paid_exactly,
    scenario_eur_underpaid_then_topped_up,
    scenario_overpaid_then_refunded,
    scenario_expired_then_paid_late,
    scenario_third_party_sweep,
    scenario_payment_reorged_out_before_finality,
    scenario_refund_attempt_abandoned,
    scenario_refund_beyond_the_float_waits_while_sweeps_go_on,
    scenario_refund_to_an_eip7702_account,
    scenario_operator_out_of_gas_sweep_waits,
    scenario_underpaid_refund_only_after_the_payment_window,
    scenario_three_inputs_then_the_rest_refunded,
    scenario_accept_partial_after_a_third_party_sweep,
    scenario_paid_inside_the_quote_final_after_it,
    scenario_token_payment_reorged_to_another_log_index,
    scenario_refunds_wait_for_a_price_then_go_once,
    scenario_stray_payment_past_the_block_scan_then_attributed,
    scenario_refund_final_applied_once_after_a_failed_resolve,
    scenario_refund_needs_owner_applied_once_after_a_failed_resolve,
    scenario_sweep_failure_applied_once_after_a_failed_resolve,
    scenario_deposit_recovered_after_its_invoice_row_is_lost,
    scenario_api_token_invoice_paid_with_signed_webhook,
    scenario_discord_messages_for_invoice_events,
    scenario_frozen_token_skipped_by_hand,
    scenario_skipped_token_on_a_lapsed_underpaid_invoice_is_not_late,
    scenario_refreshed_order_paid_and_late_money_on_the_old_deposit,
    scenario_api_token_merchant_routes_and_invoice_callback,
    *OPERATOR_KEY_SCENARIOS,
    *MERCHANT_OPERATIONS_SCENARIOS,
    *STATIC_DEPOSIT_ADDRESS_SCENARIOS,
    *INVOICE_SCHEDULE_SCENARIOS,
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--scratch-folder", type=Path, default=Path(tempfile.gettempdir()) / "pay-7331-e2e" / "polygon_fork_flow"
    )
    parser.add_argument("--keep-stack", action="store_true", help="leave the stack running for a look")
    parser.add_argument(
        "--scenario", action="append", default=[], help="run only this scenario (its function name); repeatable"
    )
    options = parser.parse_args()
    unknown_names = set(options.scenario) - {scenario.__name__ for scenario in SCENARIOS}
    if unknown_names:
        parser.error(f"no such scenario: {', '.join(sorted(unknown_names))}")
    return run_scenarios_on_fork_stack(
        [
            (scenario.__name__, scenario)
            for scenario in SCENARIOS
            if not options.scenario or scenario.__name__ in options.scenario
        ],
        scratch_folder=options.scratch_folder.resolve(),
        keep_stack=options.keep_stack,
    )


if __name__ == "__main__":
    sys.exit(main())
