"""Mixed-network end-to-end run on two anvil forks, Polygon and Arbitrum One (PLAN.md sections 12 and 15). Run by hand
when the payer-picks-the-network flow changes:

    PAY_E2E_FORK_CHAIN=mixed uv run --project backend python e2e/mixed_network_flow.py \\
        [--scratch-folder DIR] [--keep-stack] [--scenario NAME]

`PAY_E2E_STACK_NAME`, `PAY_E2E_API_PORT`, `PAY_E2E_ANVIL_PORT` and `PAY_E2E_ANVIL_ARBITRUM_PORT` give parallel runs their
own stack (e2e/fork_stack.py). The stack, the signed-up account (a virtual passkey in headless Chromium), the watcher and
the operator's sweeps are the real system; payers are fresh local keys and the money is fork money. Pages run at 390 px.
Both networks read live prices over public endpoints, so the quotes are real and move.

1. Estimates (API): an account invoice offers coins on both networks. Its legacy fields (`chain`, `assets`, the stored
   `chain_key`) are the first offered chain's, whichever that is; every option is priced by its own network's Chainlink
   feed (read here independently over the same public endpoints), the same stablecoin asks
   the same on both, and the quote after a pick uses the picked network's price.
2. Quantity-1 link, two payers on different networks at once: exactly one 201 and one 409 `sold_out`; the slot stays held
   (the page says "All taken for now", a later request is refused too); once the holder's invoice expires unpaid the slot
   is free again (the account's view says so, the page offers both networks again) and a payer takes it by tapping.
3. A visitor makes a $5 invoice on `/` (both networks ticked), the payer sees "USDC · Polygon" and "USDC · Arbitrum" rows
   side by side and no address; the forward button names the coin and the network before the address shows; the payer
   picks Arbitrum USDC, sends it with the Arbitrum fork slowed down: "Confirming", then "Paid", then the operator's sweep
   is final on Arbitrum and the destination holds the USDC there and none on Polygon.
4. The same on Polygon: Polygon USDC picked, paid, swept.
5. The pair is one-way: two picks of different networks at the same moment get one 200 and one 409
   `invoice_state_conflict`, the invoice keeps the winner's network, and the same pick again is refused; a second
   browser tab left on the picker is told a coin was already picked and then shows only the winner's network.
6. A signed-in maker ticks coins per network on `/` (Polygon USDC and USDC.e, Arbitrum ETH and USDT); the account's saved
   coins are exactly those, the invoice offers exactly those four pairs, and a reload of `/` shows them again.
7. A coin-priced maker (currency USDC) starts with two networks, the currency leaves one network, and the maker switches
   it to Arbitrum: the invoice is made on Arbitrum.
8. Per-network defaults (API): with 60 minutes and 0.5% saved on Polygon and 20 minutes and 2% on Arbitrum, an invoice
   offering both made without terms waits 60 minutes for the pick, keeps both networks' terms, and the Arbitrum pick
   gives it 20 minutes from the pick and 2%, even after the settings changed; a Polygon-only one gets 60 minutes and
   0.5%.

Screenshots, the log and the results go to the scratch folder. Nothing is sent to a real network.
"""

import argparse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
import functools
from pathlib import Path
import re
import sys
import tempfile
import threading

from browser_flow import (
    PAYER_PHONE_VIEWPORT,
    BrowserScenarioRun,
    close_share_overlay_and_check_payer_preview,
    expect_pay_with_coin_button_text,
    open_phone_page,
    page_scrolls_sideways,
    read_invoice_id_from_url,
    read_link_from_share_overlay,
    wait_for_page_status,
)
from eth_abi import decode
from eth_account import Account
from eth_account.signers.local import LocalAccount
from fork_stack import (
    API_URL,
    ARBITRUM_CHAIN,
    FORK_CHAIN_SELECTION,
    POLYGON_CHAIN,
    PUBLIC_BASE_URL,
    USDC_BASE_UNITS,
    FlowContext,
    ForkChain,
    build_erc20_transfer_data,
    call_anvil,
    check,
    create_local_account,
    forget_rate_limit_budgets,
    fund_with_usdc,
    last_sweep_final,
    read_invoice,
    read_public_invoice,
    read_usdc_balance,
    resume_mining,
    run_scenarios_on_fork_stack,
    run_sql,
    send_transaction,
    to_base_units,
    wait_for_invoice,
    wait_for_receipt,
)
import httpx
from playwright.sync_api import Browser, Page, Route, expect, sync_playwright

# Made at import, so the stack's warm-up gives each native coin on both networks (fork_stack.warm_fork_state).
MIXED_PAYER_ACCOUNTS = [create_local_account() for _ in range(6)]
ARBITRUM_NETWORK_NAME = ARBITRUM_CHAIN.display_name
POLYGON_NETWORK_NAME = POLYGON_CHAIN.display_name
# Without a live price the quote refuses, so a chain's feed is read independently over the endpoints the API uses.
LATEST_ROUND_DATA_SELECTOR = "0xfeaf968c"
DECIMALS_SELECTOR = "0x313ce567"
# Public feeds move between the API's read and this one; a few percent still tells one network's feed from another's.
LIVE_PRICE_TOLERANCE = Decimal("0.03")
SLOWED_BLOCK_SECONDS = 10


# ---- Small readers and the API ----


def list_offered_pairs(invoice_or_link: dict[str, object]) -> list[tuple[str, str]]:
    """(chain key, coin symbol) of every payment option, in the order the API gave them."""
    options = invoice_or_link["payment_options"]
    check(isinstance(options, list), f"payment_options is {options!r}")
    return [(dict(option["chain"])["key"], dict(option["asset"])["symbol"]) for option in options]  # ty: ignore[no-matching-overload, not-subscriptable]


def read_option_amount(invoice_or_link: dict[str, object], *, chain: ForkChain, asset_symbol: str) -> Decimal:
    for option in invoice_or_link["payment_options"]:  # ty: ignore[not-iterable]
        if dict(option["chain"])["key"] == chain.chain_key and dict(option["asset"])["symbol"] == asset_symbol:  # ty: ignore[no-matching-overload, not-subscriptable]
            check(option["amount"] is not None, f"{asset_symbol} on {chain.chain_key} has no amount")  # ty: ignore[not-subscriptable]
            return Decimal(str(option["amount"]))  # ty: ignore[not-subscriptable]
    raise AssertionError(f"no option {asset_symbol} on {chain.chain_key}")


def create_public_invoice_on_both_networks(
    context: FlowContext,
    *,
    destination: str,
    assets_by_chain: dict[str, list[str]],
    amount_usd: str = "5",
    note: str = "",
) -> dict[str, object]:
    """What `/` posts signed out: a USD amount on both networks with the maker's exact coins on each."""
    reply = context.http_client.post(
        f"{API_URL}/public/invoices",
        json={"amount_usd": amount_usd, "destination_address": destination, "note": note,
              "chains": [POLYGON_CHAIN.chain_key, ARBITRUM_CHAIN.chain_key], "assets_by_chain": assets_by_chain},
    )  # fmt: skip
    check(reply.status_code == 201, f"public invoice: {reply.status_code} {reply.text}")
    return reply.json()


def check_nothing_to_pay_to_before_the_pick(context: FlowContext, invoice: dict[str, object]) -> None:
    """No address, no payment URI and no QR until the payer's one tap names the coin and the network."""
    invoice_id = str(invoice["id"])
    check(invoice["status"] == "awaiting_asset", f"new invoice status {invoice['status']}")
    check(invoice["deposit_address"] is None and invoice["payment_uri"] is None, "an address shows before the pick")
    check(invoice["asset"] is None, f"an asset shows before the pick: {invoice['asset']}")
    row = read_invoice_row(context, invoice_id)
    check(row["deposit_address"] == "", f"the database holds a deposit {row['deposit_address']} before the pick")
    qr_reply = context.http_client.get(f"{API_URL}/public/invoices/{invoice_id}/qr.svg")
    check(qr_reply.status_code == 409, f"a QR before the pick answered {qr_reply.status_code}")


def read_invoice_row(context: FlowContext, invoice_id: str) -> dict[str, str]:
    if not re.fullmatch(r"inv_[0-9A-Za-z]+", invoice_id):
        raise AssertionError(f"not an invoice id: {invoice_id!r}")
    row = run_sql(
        context,
        f"SELECT chain_key, coalesce(asset_symbol, ''), coalesce(deposit_address, ''), status FROM invoices WHERE id = '{invoice_id}'",  # noqa: S608
    )
    chain_key, asset_symbol, deposit_address, status = row.split("|")
    return {"chain_key": chain_key, "asset_symbol": asset_symbol, "deposit_address": deposit_address, "status": status}


def pick_pair(context: FlowContext, invoice_id: str, *, chain: ForkChain, asset_symbol: str = "USDC") -> httpx.Response:
    return context.http_client.post(
        f"{API_URL}/public/invoices/{invoice_id}/asset", json={"asset": asset_symbol, "chain": chain.chain_key}
    )


def post_at_once(requests: list[tuple[str, dict[str, object]]]) -> list[httpx.Response]:
    """Each request on its own connection, released together by a barrier; the replies in the order given."""
    start_barrier = threading.Barrier(len(requests))

    def send(url_and_body: tuple[str, dict[str, object]]) -> httpx.Response:
        with httpx.Client(timeout=60) as payer_client:
            start_barrier.wait()
            return payer_client.post(url_and_body[0], json=url_and_body[1])

    with ThreadPoolExecutor(max_workers=len(requests)) as executor:
        return list(executor.map(send, requests))


def read_live_feed_price_usd(chain: ForkChain, feed_address: str) -> Decimal:
    """A Chainlink feed's answer read straight from the public endpoints the API reads, never through the fork."""
    failures: list[str] = []
    for rpc_url in chain.live_price_rpc_urls:
        try:
            replies = [
                httpx.post(
                    rpc_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                          "params": [{"to": feed_address, "data": selector}, "latest"]},
                    timeout=20,
                ).json()["result"]
                for selector in (LATEST_ROUND_DATA_SELECTOR, DECIMALS_SELECTOR)
            ]  # fmt: skip
        except (httpx.HTTPError, KeyError, ValueError) as error:
            failures.append(f"{rpc_url}: {type(error).__name__} {error}")
            continue
        answer = decode(["uint80", "int256", "uint256", "uint256", "uint80"], bytes.fromhex(replies[0][2:]))[1]
        return Decimal(answer).scaleb(-int(replies[1], 16))
    raise AssertionError(f"no live price for {feed_address} on {chain.name}: {failures}")


def check_close(
    actual: Decimal, expected: Decimal, *, description: str, tolerance: Decimal = LIVE_PRICE_TOLERANCE
) -> None:
    check(abs(actual - expected) <= expected * tolerance, f"{description}: {actual}, independent reading {expected}")


# ---- Chain proof ----


def read_transaction_status(context: FlowContext, transaction_hash: str, *, chain: ForkChain) -> str | None:
    receipt = call_anvil(context.http_client, "eth_getTransactionReceipt", transaction_hash, chain=chain)
    return str(dict(receipt)["status"]) if isinstance(receipt, dict) else None  # ty: ignore[no-matching-overload]


def check_transaction_succeeded_only_on(
    context: FlowContext, transaction_hash: str, *, chain: ForkChain, other_chain: ForkChain
) -> None:
    status = read_transaction_status(context, transaction_hash, chain=chain)
    check(status == "0x1", f"{transaction_hash} on {chain.name}: status {status}")
    other_status = read_transaction_status(context, transaction_hash, chain=other_chain)
    check(other_status is None, f"{transaction_hash} also exists on {other_chain.name}: status {other_status}")


def send_usdc_from_payer(
    context: FlowContext, payer: LocalAccount, *, chain: ForkChain, deposit_address: str, base_units: int
) -> str:
    """Fund the payer's key with USDC (mined at the normal pace), then send it to the deposit; the hash, unmined."""
    fund_with_usdc(context.http_client, payer.address, base_units=base_units, chain=chain)
    return send_transaction(
        context.http_client, sender=payer.address, to=chain.usdc_address,
        data=build_erc20_transfer_data(deposit_address, base_units), chain=chain,
    )  # fmt: skip


def check_paid_and_swept_on(
    context: FlowContext, invoice_id: str, *, chain: ForkChain, other_chain: ForkChain, destination: str,
    payment_hash: str,
) -> dict[str, object]:  # fmt: skip
    """The invoice is paid with one final sweep on ``chain``: the destination holds the amount there and nothing on the
    other network, and both the payment's and the sweep's transactions succeeded on ``chain`` only."""
    invoice = wait_for_invoice(
        context, invoice_id, description="final sweep", condition=last_sweep_final, signed_out=True
    )
    check(invoice["status"] == "paid", f"{invoice_id} status {invoice['status']}")
    check(dict(invoice["chain"])["key"] == chain.chain_key, f"{invoice_id} chain {invoice['chain']}")  # ty: ignore[no-matching-overload]
    amount_base_units = to_base_units(str(invoice["amount_due"]), decimals=6)
    destination_balance = read_usdc_balance(context.http_client, destination, chain=chain)
    check(destination_balance == amount_base_units, f"destination holds {destination_balance} on {chain.name}")
    other_balance = read_usdc_balance(context.http_client, destination, chain=other_chain)
    check(other_balance == 0, f"destination holds {other_balance} on {other_chain.name}")
    sweep_hash = str(invoice["sweeps"][-1]["tx_hash"])  # ty: ignore[not-subscriptable]
    check_transaction_succeeded_only_on(context, payment_hash, chain=chain, other_chain=other_chain)
    check_transaction_succeeded_only_on(context, sweep_hash, chain=chain, other_chain=other_chain)
    context.write_log(
        "paid_and_swept", invoice_id=invoice_id, chain=chain.chain_key, payment_hash=payment_hash, sweep_hash=sweep_hash,
        destination_base_units=destination_balance,
    )  # fmt: skip
    return {"invoice_id": invoice_id, "chain": chain.chain_key, "payment_tx": payment_hash, "sweep_tx": sweep_hash,
            "sweep_status": "0x1", "destination_usdc_base_units": destination_balance}  # fmt: skip


# ---- What the pages show ----


def check_phone_page_fits(page: Page, *, description: str) -> None:
    check(page.viewport_size == PAYER_PHONE_VIEWPORT, f"{description}: viewport {page.viewport_size}")
    check(not page_scrolls_sideways(page), f"{description} scrolls sideways at 390 px")


def check_network_rows_side_by_side(payer_page: Page, *, symbol: str) -> None:
    """The same coin on both networks: two rows, one beside the other, each labelled with its network."""
    expect(payer_page.get_by_text(f"{symbol} · {POLYGON_NETWORK_NAME}", exact=True)).to_be_visible(timeout=30_000)
    expect(payer_page.get_by_text(f"{symbol} · {ARBITRUM_NETWORK_NAME}", exact=True)).to_be_visible()
    row_keys = payer_page.locator('[data-testid^="coin_option_"]').evaluate_all(
        "rows => rows.map(row => row.dataset.testid)"
    )
    positions = [row_keys.index(f"coin_option_{symbol}_{chain.chain_key}") for chain in (POLYGON_CHAIN, ARBITRUM_CHAIN)]
    check(abs(positions[0] - positions[1]) == 1, f"the {symbol} rows are not adjacent: {row_keys}")


def pick_row_and_check_forward_button(payer_page: Page, *, chain: ForkChain, asset_symbol: str) -> None:
    """Tap a coin row: the forward button names the coin and the network, and no address or QR is on the page yet."""
    payer_page.get_by_test_id(f"coin_option_{asset_symbol}_{chain.chain_key}").click()
    expect_pay_with_coin_button_text(payer_page, asset_symbol=asset_symbol, chain_name=chain.display_name)
    check(
        payer_page.get_by_test_id("invoice_deposit_address").count() == 0
        and payer_page.get_by_test_id("invoice_qr").count() == 0,
        "the address or QR shows before the forward tap",
    )


def commit_pick_and_read_deposit(payer_page: Page) -> str:
    """The forward tap (the payer's confirmation of coin and network), then the send screen's deposit address."""
    payer_page.click("#pay_with_coin_button")
    expect(payer_page.get_by_test_id("invoice_deposit_address")).to_be_visible(timeout=30_000)
    wait_for_page_status(payer_page, "Awaiting payment", timeout_milliseconds=30_000)
    return str(payer_page.get_by_test_id("invoice_deposit_address").text_content()).strip()


def create_invoice_as_visitor_on_front_page(run: BrowserScenarioRun, *, destination: str) -> str:
    """A signed-out visitor on `/` at phone width: the receiver, the $5 chip and "Create link" with both networks ticked
    as they come; returns the invoice's link from the share overlay."""
    visitor_page = open_phone_page(run)
    visitor_page.goto(f"{PUBLIC_BASE_URL}/")
    expect(visitor_page.locator("#account_sheet_trigger")).to_have_text("Sign in")
    expect(visitor_page.locator("#create_button")).to_be_enabled()
    expect(visitor_page.locator("#accepted_networks_summary")).to_contain_text(POLYGON_NETWORK_NAME)
    expect(visitor_page.locator("#accepted_networks_summary")).to_contain_text(ARBITRUM_NETWORK_NAME)
    visitor_page.get_by_test_id("quick_amount_5").click()
    visitor_page.fill("#receiver_address_input", destination)
    check_phone_page_fits(visitor_page, description="the front page")
    run.take_screenshot(visitor_page, "visitor_front_page_two_networks")
    visitor_page.click("#create_button")
    link_url = read_link_from_share_overlay(run, visitor_page, who="visitor")
    visitor_page.context.close()
    return link_url


def check_no_page_errors_except_conflicts(run: BrowserScenarioRun) -> None:
    """A refused pick is a 409 the browser logs as a console error; any other page error fails the run."""
    unexpected = [error for error in run.page_errors if "status of 409" not in error]
    check(not unexpected, f"the pages reported errors: {unexpected}")


# ---- Scenarios ----


def scenario_estimates_follow_the_stored_first_chain_and_each_networks_own_feed(
    context: FlowContext,
) -> dict[str, object]:  # fmt: skip
    """1. Legacy fields and per-network prices (API)."""
    destination = Account.create().address
    assets_by_chain = {POLYGON_CHAIN.chain_key: ["POL", "USDC"], ARBITRUM_CHAIN.chain_key: ["ETH", "USDC"]}
    pol_price = read_live_feed_price_usd(POLYGON_CHAIN, POLYGON_CHAIN.native_usd_feed_address)
    eth_price = read_live_feed_price_usd(ARBITRUM_CHAIN, ARBITRUM_CHAIN.native_usd_feed_address)
    check(abs(pol_price - eth_price) > 1, f"POL {pol_price} and ETH {eth_price} cannot tell their feeds apart")
    results: dict[str, object] = {"independent_pol_usd": str(pol_price), "independent_eth_usd": str(eth_price)}
    for first_chain, second_chain, native_symbol, native_price in (
        (ARBITRUM_CHAIN, POLYGON_CHAIN, "ETH", eth_price),
        (POLYGON_CHAIN, ARBITRUM_CHAIN, "POL", pol_price),
    ):
        reply = context.http_client.post(
            f"{API_URL}/invoices", headers=context.account_headers,
            json={"amount": "7", "currency": "USD", "chains": [first_chain.chain_key, second_chain.chain_key],
                  "assets_by_chain": assets_by_chain, "destination_address": destination},
        )  # fmt: skip
        check(reply.status_code == 201, f"account invoice: {reply.status_code} {reply.text}")
        invoice = reply.json()
        invoice_id = str(invoice["id"])
        # The stored first chain is what an old client sees: the chain and its coins.
        check(dict(invoice["chain"])["key"] == first_chain.chain_key, f"legacy chain {invoice['chain']}")
        check(invoice["assets"] == assets_by_chain[first_chain.chain_key], f"legacy assets {invoice['assets']}")
        check(read_invoice_row(context, invoice_id)["chain_key"] == first_chain.chain_key, "stored chain key")
        # Each network's own feed: the native coin by the independent reading, the stablecoin exactly at its peg.
        for chain, symbol, price in ((POLYGON_CHAIN, "POL", pol_price), (ARBITRUM_CHAIN, "ETH", eth_price)):
            check_close(
                read_option_amount(invoice, chain=chain, asset_symbol=symbol),
                Decimal(7) / price,
                description=f"{symbol} on {chain.name} for $7",
            )
            check(read_option_amount(invoice, chain=chain, asset_symbol="USDC") == Decimal("7.00"),
                  f"USDC on {chain.name} is {read_option_amount(invoice, chain=chain, asset_symbol='USDC')}")  # fmt: skip
        # The pick prices on the picked network and stores it there.
        picked = pick_pair(context, invoice_id, chain=first_chain, asset_symbol=native_symbol)
        check(picked.status_code == 200, f"pick {native_symbol}: {picked.status_code} {picked.text}")
        picked_view = picked.json()
        check(dict(picked_view["chain"])["key"] == first_chain.chain_key, f"picked chain {picked_view['chain']}")
        check_close(Decimal(str(dict(picked_view["quote"])["asset_price_usd"])), native_price,
                    description=f"{native_symbol} quote price")  # fmt: skip
        check_close(Decimal(str(picked_view["amount_due"])), Decimal(7) / native_price,
                    description=f"{native_symbol} amount due")  # fmt: skip
        row = read_invoice_row(context, invoice_id)
        check(row["chain_key"] == first_chain.chain_key and row["asset_symbol"] == native_symbol, f"stored {row}")
        results[first_chain.name] = {"invoice_id": invoice_id, "amount_due": picked_view["amount_due"],
                                     "quote_price_usd": dict(picked_view["quote"])["asset_price_usd"]}  # fmt: skip
    return results


def scenario_quantity_one_link_races_two_networks_and_frees_the_slot_after_expiry(
    context: FlowContext, *, browser: Browser
) -> dict[str, object]:  # fmt: skip
    """2. A quantity-1 link, two payers on different networks, the slot held, then freed by a zero-pay expiry."""
    run = BrowserScenarioRun(context=context, browser=browser, scenario_name="link_race")
    destination = Account.create().address
    link_reply = context.http_client.post(
        f"{API_URL}/payment-links", headers=context.account_headers,
        json={"amount": "5", "currency": "USD", "chains": [POLYGON_CHAIN.chain_key, ARBITRUM_CHAIN.chain_key],
              "assets": ["USDC"], "quantity": 1, "destination_address": destination, "lifetime_seconds": 300,
              "note": "Mixed e2e: the last one"},
    )  # fmt: skip
    check(link_reply.status_code == 201, f"payment link: {link_reply.status_code} {link_reply.text}")
    link_id = str(link_reply.json()["id"])
    link_url = f"{PUBLIC_BASE_URL}/l/{link_id}"
    check(sorted(list_offered_pairs(link_reply.json())) == sorted(
        [(POLYGON_CHAIN.chain_key, "USDC"), (ARBITRUM_CHAIN.chain_key, "USDC")]), "the link's payment options")  # fmt: skip

    slot_url = f"{API_URL}/public/payment-links/{link_id}/invoices"
    replies = post_at_once([
        (slot_url, {"asset": "USDC", "chain": POLYGON_CHAIN.chain_key}),
        (slot_url, {"asset": "USDC", "chain": ARBITRUM_CHAIN.chain_key}),
    ])  # fmt: skip
    statuses = [reply.status_code for reply in replies]
    context.write_log("link_race", statuses=statuses, codes=[reply.json().get("code") for reply in replies])
    check(sorted(statuses) == [201, 409], f"two networks racing for the last slot answered {statuses}")
    winning_reply = next(reply for reply in replies if reply.status_code == 201)
    losing_reply = next(reply for reply in replies if reply.status_code == 409)
    check(losing_reply.json()["code"] == "sold_out", f"the losing request: {losing_reply.text}")
    holder_invoice_id = str(winning_reply.json()["id"])
    winning_chain = POLYGON_CHAIN if statuses[0] == 201 else ARBITRUM_CHAIN
    check(
        dict(winning_reply.json()["chain"])["key"] == winning_chain.chain_key,
        f"holder chain {winning_reply.json()['chain']}",
    )
    check(read_invoice_row(context, holder_invoice_id)["chain_key"] == winning_chain.chain_key, "holder stored chain")
    invoice_count = run_sql(context, f"SELECT count(*) FROM invoices WHERE payment_link_id = '{link_id}'")  # noqa: S608
    check(invoice_count == "1", f"the link has {invoice_count} invoices, wanted 1")

    # The slot stays held while the holder's invoice is open and unpaid.
    def read_link() -> dict[str, object]:
        reply = context.http_client.get(f"{API_URL}/payment-links/{link_id}", headers=context.account_headers)
        reply.raise_for_status()
        return reply.json()

    held_link = read_link()
    check(held_link["status"] == "all_taken" and held_link["slots_held"] == 1 and held_link["slots_left"] == 0,
          f"held link {held_link['status']} {held_link['slots_held']} {held_link['slots_left']}")  # fmt: skip
    check(held_link["paid_count"] == 0, f"paid_count {held_link['paid_count']}")
    for chain in (POLYGON_CHAIN, ARBITRUM_CHAIN):
        refused = context.http_client.post(slot_url, json={"asset": "USDC", "chain": chain.chain_key})
        check(
            refused.status_code == 409 and refused.json()["code"] == "sold_out", f"{chain.name}: {refused.status_code}"
        )
    payer_page = open_phone_page(run)
    payer_page.goto(link_url)
    expect(payer_page.get_by_test_id("payment_link_status")).to_have_text("All taken for now", timeout=30_000)
    check(payer_page.locator('[data-testid^="coin_option_"]').count() == 0, "an all-taken link shows coins")
    check_phone_page_fits(payer_page, description="the all-taken link page")
    run.take_screenshot(payer_page, "link_all_taken")

    # Zero-pay expiry frees it (the deadline moved like polygon_fork_flow's late scenario; the watcher expires it).
    run_sql(context, f"UPDATE invoices SET expires_at = now() - interval '1 second' WHERE id = '{holder_invoice_id}'")  # noqa: S608
    expired_invoice = wait_for_invoice(
        context, holder_invoice_id, description="expired", condition=lambda current: current["status"] == "expired"
    )
    check(not expired_invoice["late_payment"] and not expired_invoice["payments"], "the holder's invoice saw money")
    freed_link = read_link()
    check(freed_link["status"] == "open" and freed_link["slots_held"] == 0 and freed_link["slots_left"] == 1,
          f"freed link {freed_link['status']} {freed_link['slots_held']} {freed_link['slots_left']}")  # fmt: skip

    # A payer takes the freed slot in the browser, on Arbitrum: the tap names coin and network, then the address.
    payer_page.reload()
    expect(payer_page.get_by_test_id("payment_link_status")).to_have_text("Choose how to pay", timeout=30_000)
    check_network_rows_side_by_side(payer_page, symbol="USDC")
    check(payer_page.get_by_test_id("invoice_deposit_address").count() == 0, "a link page shows an address")
    check_phone_page_fits(payer_page, description="the freed link page")
    pick_row_and_check_forward_button(payer_page, chain=ARBITRUM_CHAIN, asset_symbol="USDC")
    run.take_screenshot(payer_page, "link_freed_arbitrum_picked")
    payer_page.click("#pay_with_coin_button")
    payer_page.wait_for_url(re.compile(r"/i/inv_"))
    expect(payer_page.get_by_test_id("invoice_deposit_address")).to_be_visible(timeout=30_000)
    check_phone_page_fits(payer_page, description="the taken slot's send screen")
    taken_invoice_id = read_invoice_id_from_url(payer_page.url)
    taken_invoice = read_invoice(context, taken_invoice_id)
    check(dict(taken_invoice["chain"])["key"] == ARBITRUM_CHAIN.chain_key, f"taken chain {taken_invoice['chain']}")
    check(taken_invoice["payment_link_id"] == link_id, f"taken invoice link {taken_invoice['payment_link_id']}")
    check(read_link()["slots_held"] == 1, "the freed slot was not taken")
    run.take_screenshot(payer_page, "link_freed_slot_taken_send_screen")
    payer_page.context.close()
    run.check_no_page_errors()
    return {"link_id": link_id, "race_statuses": statuses, "holder_invoice": holder_invoice_id,
            "holder_chain": winning_chain.chain_key, "holder_final_status": expired_invoice["status"],
            "taken_invoice": taken_invoice_id, "screenshots": run.screenshot_paths}  # fmt: skip


def pay_invoice_through_the_payer_page(
    run: BrowserScenarioRun, *, chain: ForkChain, other_chain: ForkChain, payer: LocalAccount, link_url: str,
    destination: str, slows_the_chain: bool,
) -> dict[str, object]:  # fmt: skip
    """A payer on `/i/...` at 390 px: both networks' USDC side by side with no address, the pick, the forward button, the
    send screen, a USDC transfer on ``chain`` ("Confirming" then "Paid"), the final sweep on ``chain`` only."""
    context = run.context
    invoice_id = read_invoice_id_from_url(link_url)
    check_nothing_to_pay_to_before_the_pick(context, read_public_invoice(context, invoice_id))
    payer_page = open_phone_page(run)
    payer_page.goto(link_url)
    wait_for_page_status(payer_page, "Choose how to pay", timeout_milliseconds=30_000)
    check_network_rows_side_by_side(payer_page, symbol="USDC")
    check(payer_page.get_by_test_id("invoice_deposit_address").count() == 0, "an address shows before the pick")
    check_phone_page_fits(payer_page, description="the coin choice")
    run.take_screenshot(payer_page, f"{chain.name}_choice_two_networks")
    pick_row_and_check_forward_button(payer_page, chain=chain, asset_symbol="USDC")
    check_phone_page_fits(payer_page, description="the picked coin")
    run.take_screenshot(payer_page, f"{chain.name}_picked_forward_button")
    deposit_address = commit_pick_and_read_deposit(payer_page)
    invoice = read_public_invoice(context, invoice_id)
    check(deposit_address == invoice["deposit_address"], f"page {deposit_address}, API {invoice['deposit_address']}")
    check(dict(invoice["chain"])["key"] == chain.chain_key, f"picked chain {invoice['chain']}")
    check(read_invoice_row(context, invoice_id)["chain_key"] == chain.chain_key, "stored chain after the pick")
    check_phone_page_fits(payer_page, description="the send screen")
    run.take_screenshot(payer_page, f"{chain.name}_send_screen")
    base_units = to_base_units(str(invoice["amount_due"]), decimals=6)
    check(base_units == 5 * USDC_BASE_UNITS, f"a $5 USDC invoice asks {base_units} base units")
    fund_with_usdc(context.http_client, payer.address, base_units=base_units, chain=chain)
    try:
        if slows_the_chain:
            # Blocks every few seconds: the payment is mined, seen at `latest`, and final two blocks later.
            call_anvil(context.http_client, "evm_setIntervalMining", SLOWED_BLOCK_SECONDS, chain=chain)
        payment_hash = send_transaction(
            context.http_client, sender=payer.address, to=chain.usdc_address,
            data=build_erc20_transfer_data(deposit_address, base_units), chain=chain,
        )  # fmt: skip
        if slows_the_chain:
            wait_for_page_status(payer_page, "Confirming", timeout_milliseconds=60_000)
            expect(payer_page.get_by_test_id("invoice_confirming_amount")).to_contain_text("USDC received")
            check_phone_page_fits(payer_page, description="the confirming screen")
            run.take_screenshot(payer_page, f"{chain.name}_confirming")
    finally:
        resume_mining(context.http_client, chain=chain)
    wait_for_receipt(context.http_client, payment_hash, chain=chain)
    wait_for_page_status(payer_page, "Paid", timeout_milliseconds=90_000)
    check_phone_page_fits(payer_page, description="the paid screen")
    run.take_screenshot(payer_page, f"{chain.name}_paid")
    payer_page.context.close()
    proof = check_paid_and_swept_on(
        context, invoice_id, chain=chain, other_chain=other_chain, destination=destination, payment_hash=payment_hash
    )
    run.check_no_page_errors()
    return {**proof, "deposit_address": deposit_address, "screenshots": run.screenshot_paths}


def scenario_visitor_invoice_paid_in_arbitrum_usdc_and_swept(
    context: FlowContext, *, browser: Browser
) -> dict[str, object]:  # fmt: skip
    """3. A visitor's `/` invoice, the payer picks Arbitrum USDC: confirming, paid, swept."""
    run = BrowserScenarioRun(context=context, browser=browser, scenario_name="arbitrum_pay")
    destination = Account.create().address
    link_url = create_invoice_as_visitor_on_front_page(run, destination=destination)
    invoice = read_public_invoice(context, read_invoice_id_from_url(link_url))
    pairs = list_offered_pairs(invoice)
    check(
        (POLYGON_CHAIN.chain_key, "USDC") in pairs and (ARBITRUM_CHAIN.chain_key, "USDC") in pairs, f"offered {pairs}"
    )
    return pay_invoice_through_the_payer_page(
        run, chain=ARBITRUM_CHAIN, other_chain=POLYGON_CHAIN, payer=MIXED_PAYER_ACCOUNTS[0], link_url=link_url,
        destination=destination, slows_the_chain=True,
    )  # fmt: skip


def scenario_visitor_invoice_paid_in_polygon_usdc_and_swept(
    context: FlowContext, *, browser: Browser
) -> dict[str, object]:  # fmt: skip
    """4. The same invoice kind, the payer picks Polygon USDC: paid, swept."""
    run = BrowserScenarioRun(context=context, browser=browser, scenario_name="polygon_pay")
    destination = Account.create().address
    invoice = create_public_invoice_on_both_networks(
        context, destination=destination, note="Mixed e2e: Polygon pick",
        assets_by_chain={POLYGON_CHAIN.chain_key: ["USDC", "POL"], ARBITRUM_CHAIN.chain_key: ["USDC", "ETH"]},
    )  # fmt: skip
    return pay_invoice_through_the_payer_page(
        run, chain=POLYGON_CHAIN, other_chain=ARBITRUM_CHAIN, payer=MIXED_PAYER_ACCOUNTS[1], link_url=str(invoice["url"]),
        destination=destination, slows_the_chain=False,
    )  # fmt: skip


def scenario_pair_is_one_way_and_a_racing_second_pick_is_refused(
    context: FlowContext, *, browser: Browser
) -> dict[str, object]:  # fmt: skip
    """5. Two picks at once (API), then a stale second tab (browser)."""
    run = BrowserScenarioRun(context=context, browser=browser, scenario_name="pair_one_way")
    destination = Account.create().address
    assets_by_chain = {POLYGON_CHAIN.chain_key: ["USDC"], ARBITRUM_CHAIN.chain_key: ["USDC"]}
    invoice = create_public_invoice_on_both_networks(context, destination=destination, assets_by_chain=assets_by_chain)
    check_nothing_to_pay_to_before_the_pick(context, invoice)
    invoice_id = str(invoice["id"])
    asset_url = f"{API_URL}/public/invoices/{invoice_id}/asset"
    replies = post_at_once([
        (asset_url, {"asset": "USDC", "chain": POLYGON_CHAIN.chain_key}),
        (asset_url, {"asset": "USDC", "chain": ARBITRUM_CHAIN.chain_key}),
    ])  # fmt: skip
    statuses = [reply.status_code for reply in replies]
    context.write_log("pick_race", statuses=statuses, codes=[reply.json().get("code") for reply in replies])
    check(sorted(statuses) == [200, 409], f"two picks at once answered {statuses}")
    losing_reply = next(reply for reply in replies if reply.status_code == 409)
    check(losing_reply.json()["code"] == "invoice_state_conflict", f"the losing pick: {losing_reply.text}")
    winning_chain = POLYGON_CHAIN if statuses[0] == 200 else ARBITRUM_CHAIN
    losing_chain = ARBITRUM_CHAIN if winning_chain is POLYGON_CHAIN else POLYGON_CHAIN
    won = read_public_invoice(context, invoice_id)
    check(dict(won["chain"])["key"] == winning_chain.chain_key, f"the invoice moved to {won['chain']}")
    deposit_address = won["deposit_address"]
    check(deposit_address is not None and won["status"] == "awaiting_payment", f"after the race {won['status']}")
    for chain in (losing_chain, winning_chain):
        again = pick_pair(context, invoice_id, chain=chain)
        check(again.status_code == 409 and again.json()["code"] == "invoice_state_conflict",
              f"a repeat pick on {chain.name}: {again.status_code} {again.text}")  # fmt: skip
    still = read_invoice_row(context, invoice_id)
    check(still["chain_key"] == winning_chain.chain_key and still["deposit_address"] == deposit_address, f"row {still}")

    # A tab that loaded the picker before the other one picked: its tap is refused and a reload shows one network only.
    forget_rate_limit_budgets(context)
    tab_invoice = create_public_invoice_on_both_networks(
        context, destination=destination, assets_by_chain=assets_by_chain
    )
    tab_invoice_id = str(tab_invoice["id"])
    first_page, stale_page = open_phone_page(run), open_phone_page(run)
    for page in (first_page, stale_page):
        page.goto(str(tab_invoice["url"]))
        wait_for_page_status(page, "Choose how to pay", timeout_milliseconds=30_000)
        check_network_rows_side_by_side(page, symbol="USDC")
    # The stale tab's 3-second polls wait unanswered (its own requests, held in the browser), so it cannot learn of the pick.
    held_polls: list[Route] = []
    stale_page.route(f"**/api/v1/public/invoices/{tab_invoice_id}", lambda route: held_polls.append(route))
    stale_page.wait_for_timeout(1_000)
    pick_row_and_check_forward_button(first_page, chain=ARBITRUM_CHAIN, asset_symbol="USDC")
    commit_pick_and_read_deposit(first_page)
    pick_row_and_check_forward_button(stale_page, chain=POLYGON_CHAIN, asset_symbol="USDC")
    stale_page.click("#pay_with_coin_button")
    expect(stale_page.get_by_role("alert")).to_contain_text(
        "A coin was already picked for this invoice", timeout=30_000
    )
    check(stale_page.get_by_test_id("invoice_deposit_address").count() == 0, "the refused tab shows an address")
    run.take_screenshot(stale_page, "stale_tab_refused")
    check(
        dict(read_public_invoice(context, tab_invoice_id)["chain"])["key"] == ARBITRUM_CHAIN.chain_key,
        "tab race winner",
    )
    stale_page.unroute_all(behavior="ignoreErrors")
    for held_poll in held_polls:
        held_poll.continue_()
    stale_page.reload()
    expect_pay_with_coin_button_text(stale_page, asset_symbol="USDC", chain_name=ARBITRUM_NETWORK_NAME)
    expect(stale_page.get_by_test_id("coin_choice_footnote")).to_have_text(f"Send on {ARBITRUM_NETWORK_NAME} only.")
    check(
        stale_page.locator('[data-testid^="coin_option_"]').count() == 1, "the reloaded tab still offers both networks"
    )
    check(stale_page.get_by_test_id("invoice_deposit_address").count() == 0, "an address shows before the confirm tap")
    check_phone_page_fits(stale_page, description="the reloaded stale tab")
    run.take_screenshot(stale_page, "stale_tab_reloaded_one_network")
    first_page.context.close()
    stale_page.context.close()
    check_no_page_errors_except_conflicts(run)
    return {"race_invoice": invoice_id, "race_statuses": statuses, "race_winner": winning_chain.chain_key,
            "tab_invoice": tab_invoice_id, "tab_winner": ARBITRUM_CHAIN.chain_key,
            "screenshots": run.screenshot_paths}  # fmt: skip


def tick_coins_per_network_in_the_accept_sheet(account_page: Page, *, wanted_by_chain: dict[str, list[str]]) -> None:
    """Each network's coins ticked exactly as given: wanted ones first, then the rest off (one coin always stays)."""
    account_page.click("#accepted_coins_button")
    expect(account_page.locator("#accepted_coins_sheet")).to_be_visible()
    for wanted in (True, False):
        for chain_key, wanted_symbols in wanted_by_chain.items():
            prefix = "accepted_coin_toggle_"
            for coin_toggle in account_page.locator(f'[data-testid^="{prefix}"][data-testid$="_{chain_key}"]').all():
                symbol = (
                    str(coin_toggle.get_attribute("data-testid")).removeprefix(prefix).removesuffix(f"_{chain_key}")
                )
                is_pressed = coin_toggle.get_attribute("aria-pressed") == "true"
                if (symbol in wanted_symbols) == wanted and is_pressed != wanted:
                    coin_toggle.click()
    for chain_key, wanted_symbols in wanted_by_chain.items():
        ticked = [
            str(coin_toggle.get_attribute("data-testid"))
            .removeprefix("accepted_coin_toggle_")
            .removesuffix(f"_{chain_key}")
            for coin_toggle in account_page.locator(
                f'[data-testid^="accepted_coin_toggle_"][data-testid$="_{chain_key}"]'
            ).all()
            if coin_toggle.get_attribute("aria-pressed") == "true"
        ]
        check(sorted(ticked) == sorted(wanted_symbols), f"{chain_key} ticked {ticked}, wanted {wanted_symbols}")
    account_page.click("#accepted_coins_done_button")
    expect(account_page.locator("#accepted_coins_sheet")).to_have_count(0)


def scenario_maker_ticks_coins_per_network_and_they_are_saved_exactly(
    context: FlowContext, *, browser: Browser
) -> dict[str, object]:  # fmt: skip
    """6. The signed-in maker's per-network ticks."""
    run = BrowserScenarioRun(context=context, browser=browser, scenario_name="maker_ticks")
    wanted = {POLYGON_CHAIN.chain_key: ["USDC", "USDC.e"], ARBITRUM_CHAIN.chain_key: ["ETH", "USDT"]}
    destination = Account.create().address
    account_page = run.open_account_page()
    account_page.goto(f"{PUBLIC_BASE_URL}/")
    expect(account_page.locator("#account_sheet_trigger")).to_have_text(re.compile(r"^@"))
    expect(account_page.locator("#create_button")).to_be_enabled()
    expect(account_page.locator("#accepted_networks_summary")).to_contain_text(POLYGON_NETWORK_NAME)
    expect(account_page.locator("#accepted_networks_summary")).to_contain_text(ARBITRUM_NETWORK_NAME)
    tick_coins_per_network_in_the_accept_sheet(account_page, wanted_by_chain=wanted)
    expected_networks_summary = f"{POLYGON_NETWORK_NAME}: USDC, USDC.e · {ARBITRUM_NETWORK_NAME}: ETH, USDT"
    expect(account_page.locator("#accepted_coins_summary")).to_have_text("USDC, USDC.e, ETH, USDT")
    expect(account_page.locator("#accepted_networks_summary")).to_have_text(expected_networks_summary)
    run.take_screenshot(account_page, "maker_ticks_set")
    account_page.click("#change_receiver_address_button")
    account_page.fill("#receiver_address_input", destination)
    account_page.click("#create_button")
    link_url = read_link_from_share_overlay(run, account_page, who="maker")
    close_share_overlay_and_check_payer_preview(account_page)
    invoice_id = read_invoice_id_from_url(link_url)
    invoice = read_invoice(context, invoice_id)
    expected_pairs = [(POLYGON_CHAIN.chain_key, "USDC"), (POLYGON_CHAIN.chain_key, "USDC.e"),
                      (ARBITRUM_CHAIN.chain_key, "ETH"), (ARBITRUM_CHAIN.chain_key, "USDT")]  # fmt: skip
    check(sorted(list_offered_pairs(invoice)) == sorted(expected_pairs), f"offered {list_offered_pairs(invoice)}")
    session = context.http_client.get(f"{API_URL}/sessions/current", headers=context.account_headers).json()
    check(session["accepted_assets_by_chain"] == wanted, f"saved coins {session['accepted_assets_by_chain']}")
    # A new visit to `/` starts from what was saved.
    account_page.goto(f"{PUBLIC_BASE_URL}/")
    expect(account_page.locator("#accepted_coins_summary")).to_have_text("USDC, USDC.e, ETH, USDT", timeout=30_000)
    expect(account_page.locator("#accepted_networks_summary")).to_have_text(expected_networks_summary)
    run.take_screenshot(account_page, "maker_ticks_after_reload")
    account_page.context.close()
    payer_page = open_phone_page(run)
    payer_page.goto(link_url)
    wait_for_page_status(payer_page, "Choose how to pay", timeout_milliseconds=30_000)
    row_keys = payer_page.locator('[data-testid^="coin_option_"]').evaluate_all(
        "rows => rows.map(row => row.dataset.testid)"
    )
    check(sorted(row_keys) == sorted(f"coin_option_{symbol}_{chain_key}" for chain_key, symbol in expected_pairs),
          f"the payer's rows {row_keys}")  # fmt: skip
    check_phone_page_fits(payer_page, description="the maker's four-row choice")
    run.take_screenshot(payer_page, "maker_ticks_payer_rows")
    payer_page.context.close()
    run.check_no_page_errors()
    return {"invoice_id": invoice_id, "saved_assets_by_chain": session["accepted_assets_by_chain"],
            "offered_pairs": list_offered_pairs(invoice), "screenshots": run.screenshot_paths}  # fmt: skip


def scenario_coin_priced_usdc_maker_leaves_two_networks_then_switches_to_arbitrum(
    context: FlowContext, *, browser: Browser
) -> dict[str, object]:  # fmt: skip
    """7. Currency USDC takes the maker to one network; the maker moves it to Arbitrum and creates."""
    run = BrowserScenarioRun(context=context, browser=browser, scenario_name="coin_priced_switch")
    account_page = run.open_account_page()
    account_page.goto(f"{PUBLIC_BASE_URL}/")
    expect(account_page.locator("#account_sheet_trigger")).to_have_text(re.compile(r"^@"))
    expect(account_page.locator("#create_button")).to_be_enabled()
    expect(account_page.locator("#accepted_networks_summary")).to_contain_text(POLYGON_NETWORK_NAME)
    expect(account_page.locator("#accepted_networks_summary")).to_contain_text(ARBITRUM_NETWORK_NAME)
    run.take_screenshot(account_page, "coin_priced_two_networks")
    account_page.click("#invoice_options_button")
    account_page.click("#currency_select")
    account_page.get_by_role("option", name="USDC", exact=True).click()
    account_page.click("#invoice_options_done_button")
    expect(account_page.locator("#invoice_options_sheet")).to_have_count(0)
    # The coin-priced link names one network: the first that lists USDC.
    expect(account_page.locator("#accepted_networks_summary")).to_have_text(f"on {POLYGON_NETWORK_NAME}")
    expect(account_page.locator("#accepted_coins_summary")).to_have_text("USDC")
    run.take_screenshot(account_page, "coin_priced_one_network")
    account_page.click("#accepted_coins_button")
    polygon_toggle = account_page.get_by_test_id(f"accepted_network_toggle_{POLYGON_CHAIN.chain_key}")
    arbitrum_toggle = account_page.get_by_test_id(f"accepted_network_toggle_{ARBITRUM_CHAIN.chain_key}")
    expect(polygon_toggle).to_have_attribute("aria-pressed", "true")
    expect(arbitrum_toggle).to_have_attribute("aria-pressed", "false")
    arbitrum_toggle.click()
    expect(arbitrum_toggle).to_have_attribute("aria-pressed", "true")
    expect(polygon_toggle).to_have_attribute("aria-pressed", "false")
    account_page.click("#accepted_coins_done_button")
    expect(account_page.locator("#accepted_networks_summary")).to_have_text(f"on {ARBITRUM_NETWORK_NAME}")
    expect(account_page.locator("#accepted_coins_summary")).to_have_text("USDC")
    account_page.fill("#amount_input", "5")
    run.take_screenshot(account_page, "coin_priced_arbitrum")
    account_page.click("#create_button")
    link_url = read_link_from_share_overlay(run, account_page, who="maker")
    invoice_id = read_invoice_id_from_url(link_url)
    invoice = read_invoice(context, invoice_id)
    check(dict(invoice["chain"])["key"] == ARBITRUM_CHAIN.chain_key, f"coin-priced invoice chain {invoice['chain']}")
    check(invoice["assets"] == ["USDC"], f"coin-priced invoice assets {invoice['assets']}")
    check(dict(invoice["fiat"])["currency"] is None, f"coin-priced invoice fiat {invoice['fiat']}")
    # A coin-priced invoice may ask its amount only once the payer's tap starts the quote.
    check(
        invoice["amount_due"] is None or Decimal(str(invoice["amount_due"])) == 5,
        f"coin-priced amount {invoice['amount_due']}",
    )
    check(read_invoice_row(context, invoice_id)["chain_key"] == ARBITRUM_CHAIN.chain_key, "coin-priced stored chain")
    account_page.context.close()
    run.check_no_page_errors()
    return {"invoice_id": invoice_id, "chain": ARBITRUM_CHAIN.chain_key, "status": invoice["status"],
            "screenshots": run.screenshot_paths}  # fmt: skip


def read_window_seconds(invoice: dict[str, object], *, since: datetime) -> float:
    return (datetime.fromisoformat(str(invoice["expires_at"])) - since).total_seconds()


def scenario_each_network_takes_its_own_window_and_shortfall(context: FlowContext) -> dict[str, object]:
    """8. Per-network defaults (API): the account saves 60 minutes and 0.5% on Polygon and 20 minutes and 2% on
    Arbitrum. An invoice offering both, made without terms, waits for the pick with the longer window, snapshots both,
    and on the pick takes the picked network's window (restarted from the pick) and shortfall; a settings change after
    it was made moves nothing; a one-network invoice takes its network's at once. Cleared at the end."""
    polygon_key, arbitrum_key = POLYGON_CHAIN.chain_key, ARBITRUM_CHAIN.chain_key

    def save_defaults(lifetime_by_chain: dict[str, int | None], accuracy_by_chain: dict[str, str | None]) -> None:
        reply = context.http_client.patch(
            f"{API_URL}/accounts/current", headers=context.account_headers,
            json={"invoice_lifetime_seconds_by_chain": lifetime_by_chain, "accuracy_percent_by_chain": accuracy_by_chain},
        )  # fmt: skip
        check(reply.status_code == 200, f"PATCH /accounts/current: {reply.status_code} {reply.text}")

    def create_account_invoice(**fields: object) -> dict[str, object]:
        reply = context.http_client.post(
            f"{API_URL}/invoices", headers=context.account_headers,
            json={"amount": "3", "currency": "USD", "destination_address": Account.create().address, **fields},
        )  # fmt: skip
        check(reply.status_code == 201, f"account invoice: {reply.status_code} {reply.text}")
        return reply.json()

    save_defaults({polygon_key: 3600, arbitrum_key: 1200}, {polygon_key: "0.5", arbitrum_key: "2"})
    try:
        both = create_account_invoice(
            chains=[arbitrum_key, polygon_key], assets_by_chain={arbitrum_key: ["USDC"], polygon_key: ["USDC"]}
        )
        created_at = datetime.fromisoformat(str(both["created_at"]))
        check(abs(read_window_seconds(both, since=created_at) - 3600) < 5,
              f"the two-network invoice waits {read_window_seconds(both, since=created_at)} s for the pick")  # fmt: skip
        check(re.fullmatch(r"inv_[0-9A-Za-z]+", str(both["id"])) is not None, f"not an invoice id: {both['id']}")
        stored_terms_sql = f"payment_terms_by_chain -> '{arbitrum_key}'"
        snapshot = run_sql(
            context,
            f"SELECT {stored_terms_sql} ->> 'lifetime_seconds', {stored_terms_sql} ->> 'accuracy_percent' FROM invoices WHERE id = '{both['id']}'",  # noqa: S608
        )
        stored_lifetime_text, stored_accuracy_text = snapshot.split("|")
        check(stored_lifetime_text == "1200" and Decimal(stored_accuracy_text) == Decimal(2),
              f"the stored Arbitrum terms: {snapshot}")  # fmt: skip
        # Changed after the invoice was made: the invoice keeps what it was made with.
        save_defaults({arbitrum_key: 900}, {arbitrum_key: "1"})
        picked_at = datetime.now(UTC)
        picked = pick_pair(context, str(both["id"]), chain=ARBITRUM_CHAIN)
        check(picked.status_code == 200, f"pick: {picked.status_code} {picked.text}")
        picked_account_view = read_invoice(context, str(both["id"]))
        check(Decimal(str(picked_account_view["accuracy_percent"])) == Decimal(2),
              f"picked shortfall {picked_account_view['accuracy_percent']}")  # fmt: skip
        check(abs(read_window_seconds(picked_account_view, since=picked_at) - 1200) < 10,
              f"the picked window is {read_window_seconds(picked_account_view, since=picked_at)} s, wanted 1200")  # fmt: skip
        one = create_account_invoice(chains=[polygon_key], assets_by_chain={polygon_key: ["USDC"]})
        check(Decimal(str(one["accuracy_percent"])) == Decimal("0.5")
              and abs(read_window_seconds(one, since=datetime.fromisoformat(str(one["created_at"]))) - 3600) < 5,
              f"the one-network invoice: {one['accuracy_percent']}%, {one['expires_at']}")  # fmt: skip
        return {"two_network_invoice_id": both["id"], "one_network_invoice_id": one["id"], "snapshot": snapshot}
    finally:
        save_defaults({polygon_key: None, arbitrum_key: None}, {polygon_key: None, arbitrum_key: None})


API_SCENARIOS: list[Callable[[FlowContext], dict[str, object]]] = [
    scenario_estimates_follow_the_stored_first_chain_and_each_networks_own_feed,
    scenario_each_network_takes_its_own_window_and_shortfall,
]
BROWSER_SCENARIOS: list[Callable[..., dict[str, object]]] = [
    scenario_quantity_one_link_races_two_networks_and_frees_the_slot_after_expiry,
    scenario_visitor_invoice_paid_in_arbitrum_usdc_and_swept,
    scenario_visitor_invoice_paid_in_polygon_usdc_and_swept,
    scenario_pair_is_one_way_and_a_racing_second_pick_is_refused,
    scenario_maker_ticks_coins_per_network_and_they_are_saved_exactly,
    scenario_coin_priced_usdc_maker_leaves_two_networks_then_switches_to_arbitrum,
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--scratch-folder", type=Path, default=Path(tempfile.gettempdir()) / "pay-7331-e2e" / "mixed_network_flow"
    )
    parser.add_argument("--keep-stack", action="store_true", help="leave the stack running for a look")
    parser.add_argument(
        "--scenario", action="append", default=[], help="run only this scenario (its function name); repeatable"
    )
    options = parser.parse_args()
    if FORK_CHAIN_SELECTION != "mixed":
        parser.error("run with PAY_E2E_FORK_CHAIN=mixed: this run needs both forks")
    unknown_names = set(options.scenario) - {scenario.__name__ for scenario in [*API_SCENARIOS, *BROWSER_SCENARIOS]}
    if unknown_names:
        parser.error(f"no such scenario: {', '.join(sorted(unknown_names))}")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            return run_scenarios_on_fork_stack(
                [
                    (
                        scenario.__name__,
                        scenario if scenario in API_SCENARIOS else functools.partial(scenario, browser=browser),
                    )
                    for scenario in [*API_SCENARIOS, *BROWSER_SCENARIOS]
                    if not options.scenario or scenario.__name__ in options.scenario
                ],
                scratch_folder=options.scratch_folder.resolve(),
                keep_stack=options.keep_stack,
                browser=browser,
            )
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
