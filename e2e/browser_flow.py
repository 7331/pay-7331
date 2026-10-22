"""Browser end-to-end run on an anvil fork of Polygon mainnet (PLAN.md sections 0, 10 and 12). Run by hand when a page
or the flow behind it changes:

    uv run --project backend python e2e/browser_flow.py [--scratch-folder DIR] [--keep-stack]

The stack comes from e2e/fork_stack.py (the same one e2e/polygon_fork_flow.py drives through the API). Here headless
Chromium (Playwright) uses the pages the way people do: the account on / (with its Options sheet) at desktop width and
a visitor on / at phone width, the payer on /i/... at phone width (390 px) with a minimal EIP-1193 wallet injected as `window.ethereum`. The wallet
holds a fresh local key: it signs `eth_sendTransaction` and `personal_sign` in Python (bridged with `expose_function`) and
forwards everything else to the fork. Scenarios, each checking the page, the API, balances and the recorded events:

The account is the one the stack signed up with a virtual passkey (its session cookie in the account's browser
context); its payout address is the operator's own, the only receiver the float refunds for.

"Create link" lands the maker on the invoice's own page (the address bar is the link) under a share overlay: a Dialog on
the account's desktop, a Drawer on the visitor's phone. Each run reads the link and its QR there and closes it; the maker
then sees the payer view with "This is what your payer sees" and no account tab bar, and a payer opening the link sees
neither.

1. Signed in on /, the New tab is current; the account submits an empty amount (the error shows under the amount,
   not by the button, and goes once it is typed), then makes a $5 USDC invoice with 15 minutes to pay in Options; the payer sees note, amounts, QR (decoded, the API's EIP-681 URI) and
   address, pays with the wallet button; the page goes confirming, then paid; USDC swept; paid and swept events.
2. The account makes a 3 EUR POL invoice on / (EUR in Options); the payer pays half by a direct send, sees underpaid with the rest,
   pays the rest with the wallet button; paid and swept.
3. Signed out on / at phone width, a visitor is refused without a receiver and below $1, then makes a $1 invoice to the
   operator's address in the default coins (POL, USDC, USDT, not every listed coin), with no account tab bar and a
   note behind "Add a note"; the share Drawer names the
   receiver and those coins; a tap on a coin through the open share Drawer selects nothing, after "Done" the coin
   commit stays disabled for the maker, a reload keeps the share icon and not the overlay, Back gives a fresh form,
   "Make another" a form that keeps the receiver; the payer selects USDC, goes back, selects
   POL and commits with the forward button ("Pay <amount> POL on Polygon", which also confirms the coin and network, so
   the send screen follows at once), then overpays 40 POL by a direct send; the excess goes back by itself once final,
   with nothing to sign, and the paid screen says in one sentence where it went, linked to the refund.
4. A POL invoice to the operator's address underpaid; while the payment window is open the page says what arrived
   and what is left and offers no refund (no button, no line); after it the account's /activity lists it under "Needs
   you", whose "Resolve" opens the overlay that offers that one sender and asks before refunding; the payer asks for
   the money back from the page, the wallet signs, the page says where it goes with nothing left to pay with, it lands
   back and the page shows it refunded, in one sentence naming the amount and the address.
5. Quantity link: the account sets "How many payers" to 2 in /'s Options and lands on /l/... under the share overlay; payer A picks
   USDC and pays from the wallet, payer B picks POL and pays; a third payer sees "Sold out" (light and dark) and the
   API refuses 409 `sold_out`; /activity's Links tab says "Sold 2 of 2" with both payers. Race: two slot requests at
   once for a quantity-1 link get exactly one invoice; the page says "All taken for now"; closed from /activity, the
   page says "Link closed".
6. Wrong coin (INC-001): a USDC-only invoice to the operator's address gets POL instead; within seconds the payer page
   says so in an Alert and the invoice stays awaiting payment with nothing received; once final the POL goes back to
   the payer by itself, in full, and /activity shows "Wrong coin received" with no button; once expired the rescan
   sweeps it, not late, and the sweep shows its coin. INC-001 replayed: POL into a closed deposit is swept as late,
   then `record-stray-payment-from-transaction` records it and it goes back by itself, once. On an invoice to someone
   else's address nothing goes back and both pages say the seller settles it.
7. "I've paid", with the watcher paused so the page cannot move on by itself: after a direct POL
   send, the check rereads the deposit, says the payment is on its way (or counted) and closes; then on a USDC-only
   invoice nothing shows yet, so it asks for the hash: a Tron-style id doesn't look like a Polygon hash, a random 0x
   hash isn't on the chain, a send to another address says so, and the hash of 30 POL sent to this deposit records it
   as wrong-coin money, which goes back by itself once the watcher runs again. The one-coin confirm (the coin row
   picked, "Pay 25.00 USDC on Polygon") and the sheet are shot at 360, 390 and 430 px and on a desktop, the confirm
   dark too.
8. The send lock survives a reload (2026-10-12): a copy, the page hidden and shown again, and a muted line points at
   "I've paid"; then, with the watcher paused, the wallet pays and the page is reloaded: still "Waiting for
   confirmation…", no live "Pay with wallet", the QR dimmed and "Your payment is on its way"; the watcher resumes and
   it counts once. A reload after a wallet send that reverted on chain, or one the wallet sent to another address,
   clears the lock and says so.

9. Watcher silence: with the watcher stopped, the API records one "Needs the operator" alert (`watcher_not_ticking`)
   on the operator's account about two minutes later, then the watcher starts again.

10. Settings on a 375 px phone (in Chromium: WebKit sends no Secure cookie to http://localhost): the groups read Getting
   paid, Payer page, Security, Connect, Records, and every row's sheet lies within the screen (no element past an edge,
   no sideways scroll, the export sheet with both dates filled); the Coins sheet refuses a 3-minute window under its
   field, says the network counts a payment at its finalized block (no count to set), and saves 45 minutes and 2.5%;
   an invoice made on / with Options left empty, an API invoice and a quantity link's payer invoice without terms take
   them, an API invoice naming 10 minutes and 0.5% keeps its own; an unknown network is refused; the defaults are
   cleared at the end. Every sheet the other scenarios open (options and coins on /, the share overlay, "I've paid",
   the link and invoice Drawers on /activity) is checked the same way at its page's width.

Screenshots of each key moment, the log and the results go to the scratch folder. Nothing is sent to a real network.
"""

import argparse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, ROUND_UP, Decimal
import functools
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
import time

from account_passkey_session import ACCOUNT_SESSION_COOKIE_NAME
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount
from fork_stack import (
    API_URL,
    FORK_CHAIN_ID,
    FORK_CHAIN_KEY,
    FORK_CHAIN_NAME,
    NATIVE_SYMBOL,
    NATIVE_UNIT_WEI,
    PUBLIC_BASE_URL,
    USDC_BASE_UNITS,
    FlowContext,
    FlowFailedError,
    call_anvil,
    call_anvil_raw,
    check,
    compose_arguments,
    compose_environment,
    create_invoice,
    create_local_account,
    fund_with_usdc,
    last_refund_final,
    last_sweep_final,
    read_invoice,
    read_native_balance,
    read_public_invoice,
    read_usdc_balance,
    run_command,
    run_operator_cli,
    run_scenarios_on_fork_stack,
    run_sql,
    send_transaction,
    to_base_units,
    wait_for_account_events,
    wait_for_invoice,
    wait_for_receipt,
)
import httpx
from playwright.sync_api import Browser, BrowserContext, Locator, Page, expect, sync_playwright

# Made at import, so the stack's warm-up gives each POL (fork_stack.warm_fork_state).
PAYER_ACCOUNTS = [create_local_account() for _ in range(12)]
PUBLIC_BASE_HOST = "localhost"
PAYER_PHONE_VIEWPORT = {"width": 390, "height": 844}
ACCOUNT_DESKTOP_VIEWPORT = {"width": 1280, "height": 900}
# The payer pages speak the browser's language (PLAN.md section 10); the e2e reads their English, so every context
# pins it rather than inheriting the machine's locale.
BROWSER_LOCALE = "en-US"
PAGE_STATUS_TIMEOUT_MILLISECONDS = 180_000
# The page asks about a stored send again after Retry-After when the public-write budget (10 a minute per IP) is spent,
# as it is right after the "I paid" scenario.
STORED_SEND_CHECK_TIMEOUT_MILLISECONDS = 90_000
WALLET_BRIDGE_FUNCTION_NAME = "pay_e2e_wallet_request"
# The page's `window.ethereum`: every request goes to Python, which answers {"result"} or {"error": {code, message}}.
INJECTED_WALLET_SCRIPT = f"""
(() => {{
  window.ethereum = {{
    isPayE2eWallet: true,
    async request({{ method, params = [] }}) {{
      const reply = JSON.parse(await window.{WALLET_BRIDGE_FUNCTION_NAME}(JSON.stringify({{ method, params }})));
      if (reply.error) {{
        const wallet_error = new Error(reply.error.message);
        wallet_error.code = reply.error.code;
        throw wallet_error;
      }}
      return reply.result;
    }},
    on() {{}},
    removeListener() {{}},
  }};
}})();
"""


class WalletRequestError(Exception):
    def __init__(self, message: str, *, code: int) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class InjectedBrowserWallet:
    """Answers the page's EIP-1193 requests for one local key."""

    http_client: httpx.Client
    account: LocalAccount
    # Misbehaviours a real wallet can show, for the send lock: a fixed gas limit (a token transfer the payer can't
    # cover then reverts on chain instead of failing the estimate) and a native send that lands elsewhere.
    fixed_gas: int | None = None
    redirect_native_sends_to: str | None = None
    sent_transaction_hashes: list[str] = field(default_factory=list)
    signed_messages: list[str] = field(default_factory=list)
    requested_methods: list[str] = field(default_factory=list)

    def handle_page_request(self, request_json: str) -> str:
        request = json.loads(request_json)
        method = str(request["method"])
        self.requested_methods.append(method)
        try:
            return json.dumps({"result": self.answer_request(method, params=list(request.get("params") or []))})
        except WalletRequestError as error:
            return json.dumps({"error": {"code": error.code, "message": str(error)}})
        # Broad on purpose: anything else would hang the page's promise; it gets a wallet error instead.
        except Exception as error:  # noqa: BLE001
            return json.dumps({"error": {"code": -32603, "message": f"{type(error).__name__}: {error}"}})

    def answer_request(self, method: str, *, params: list[object]) -> object:
        if method in {"eth_requestAccounts", "eth_accounts"}:
            return [self.account.address]
        if method == "eth_chainId":
            return hex(FORK_CHAIN_ID)
        if method == "wallet_switchEthereumChain":
            requested_chain_id = int(str(dict(params[0])["chainId"]), 16)  # ty: ignore[no-matching-overload]
            if requested_chain_id != FORK_CHAIN_ID:
                raise WalletRequestError(f"unknown chain {requested_chain_id}", code=4902)
            return None
        if method == "eth_sendTransaction":
            return self.send_page_transaction(dict(params[0]))  # ty: ignore[no-matching-overload]
        if method == "personal_sign":
            message_hex, signing_address = str(params[0]), str(params[1])
            self.require_own_address(signing_address)
            message_text = bytes.fromhex(message_hex.removeprefix("0x")).decode()
            self.signed_messages.append(message_text)
            signature = self.account.sign_message(encode_defunct(hexstr=message_hex)).signature
            return "0x" + signature.hex().removeprefix("0x")
        reply = call_anvil_raw(self.http_client, method, params)
        if "error" in reply:
            error = dict(reply["error"])  # ty: ignore[no-matching-overload]
            raise WalletRequestError(str(error.get("message")), code=int(error.get("code", -32603)))
        return reply["result"]

    def send_page_transaction(self, transaction: dict[str, object]) -> str:
        self.require_own_address(str(transaction["from"]))
        data = str(transaction.get("data", "0x"))
        recipient = str(transaction["to"])
        if self.redirect_native_sends_to and data == "0x":
            recipient = self.redirect_native_sends_to
        transaction_hash = send_transaction(
            self.http_client,
            sender=self.account.address,
            to=recipient,
            value_wei=int(str(transaction.get("value", "0x0")), 16),
            data=data,
            gas=self.fixed_gas,
        )
        self.sent_transaction_hashes.append(transaction_hash)
        return transaction_hash

    def require_own_address(self, address: str) -> None:
        if address.lower() != self.account.address.lower():
            raise WalletRequestError(f"{address} is not this wallet's account", code=4100)


@dataclass
class BrowserScenarioRun:
    """One scenario's browser state: numbered screenshots and the page errors seen."""

    context: FlowContext
    browser: Browser
    scenario_name: str
    screenshot_paths: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)

    def open_account_page(self) -> Page:
        """A desktop page signed in as the account: the session the stack's passkey sign-up opened."""
        account_context = self.browser.new_context(viewport=ACCOUNT_DESKTOP_VIEWPORT, locale=BROWSER_LOCALE)  # ty: ignore[invalid-argument-type]
        account_context.add_cookies([{
            # Chromium refuses a `__Host-` cookie set by url on http://localhost; host-only by domain and path works.
            "name": ACCOUNT_SESSION_COOKIE_NAME, "value": self.context.account_session_token,
            "domain": PUBLIC_BASE_HOST, "path": "/", "secure": True, "httpOnly": True, "sameSite": "Strict",
        }])  # fmt: skip
        return self.watch_page(account_context.new_page())

    def open_payer_context(self, wallet: InjectedBrowserWallet) -> BrowserContext:
        payer_context = self.browser.new_context(
            viewport=PAYER_PHONE_VIEWPORT,  # ty: ignore[invalid-argument-type]
            locale=BROWSER_LOCALE,
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
            # No entrance motion or QR scan line in the way of the screenshots and the QR decode.
            reduced_motion="reduce",
        )
        payer_context.expose_function(WALLET_BRIDGE_FUNCTION_NAME, wallet.handle_page_request)
        payer_context.add_init_script(INJECTED_WALLET_SCRIPT)
        return payer_context

    def watch_page(self, page: Page) -> Page:
        page.on("pageerror", lambda error: self.page_errors.append(f"{page.url}: {error}"))
        page.on(
            "console",
            lambda message: (
                self.page_errors.append(f"{page.url}: console {message.text}") if message.type == "error" else None
            ),
        )
        return page

    def take_screenshot(self, page: Page, moment: str) -> Path:
        screenshot_folder = self.context.scratch_folder / "screenshots"
        screenshot_folder.mkdir(exist_ok=True)
        screenshot_path = screenshot_folder / f"{self.scenario_name}_{len(self.screenshot_paths) + 1:02d}_{moment}.png"
        # Finite CSS animations (a dialog zooming in) jump to their end, so the shot shows what settled.
        page.screenshot(path=screenshot_path, full_page=True, animations="disabled")
        self.screenshot_paths.append(screenshot_path.name)
        return screenshot_path

    def check_no_page_errors(self) -> None:
        check(not self.page_errors, f"the pages reported errors: {self.page_errors}")


# ---- Reading what the pages show ----


def format_amount_like_the_page(decimal_text: str) -> str:
    """amount_formatting.ts formatDecimalForDisplay: the whole part grouped, at most six places (half up), a shorter
    fraction kept at its own width, at least two places."""
    whole_part, _, fraction_part = decimal_text.partition(".")
    if len(fraction_part) > 6:
        rounded = Decimal(decimal_text).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        whole_part, _, fraction_part = str(rounded).partition(".")
    return f"{int(whole_part):,}.{fraction_part.ljust(2, '0')}"


def format_amount_to_send_like_the_page(decimal_text: str) -> str:
    """amount_formatting.ts formatAmountToSendForDisplay: like format_amount_like_the_page, but rounded up at six
    places, so a payer typing the shown figure never comes up short."""
    whole_part, _, fraction_part = decimal_text.partition(".")
    if len(fraction_part) > 6:
        rounded = Decimal(decimal_text).quantize(Decimal("0.000001"), rounding=ROUND_UP)
        whole_part, _, fraction_part = str(rounded).partition(".")
    return f"{int(whole_part):,}.{fraction_part.ljust(2, '0')}"


def pad_decimal_places(decimal_text: str, places: int) -> str:
    """ "23.9831" to 5 places is "23.98310", as the underpaid screen lines up the rest and what arrived."""
    whole_part, _, fraction_part = decimal_text.partition(".")
    return f"{whole_part}.{fraction_part.ljust(places, '0')}"


def format_fiat_like_the_page(amount_text: str, *, currency: str) -> str:
    return {"USD": "$", "EUR": "€"}[currency] + f"{Decimal(amount_text):,.2f}"


def page_scrolls_sideways(page: Page) -> bool:
    """The document, or PageColumn's content scroller (frontend/src/components/page_column.tsx), is wider than its view.
    The scroller clips sideways overflow, but its scrollWidth still reports it."""
    return bool(
        page.evaluate(
            """() => [document.documentElement, document.getElementById("page_column_content")]
                .some((element) => element !== null && element.scrollWidth > element.clientWidth)"""
        )
    )


# Every element of every open dialog (a Drawer and a Dialog are both role="dialog") must lie within the viewport, and
# no scroll area in one may be wider than its view: a sheet's own scroller clips sideways overflow (as on iOS, where a
# date field once pushed the export sheet off the screen), so the document's width alone would not show it. Text cut
# with an ellipsis and form fields scrolling their own value are their own business. Finite animations are awaited
# first, so a Dialog zooming in is measured at its end.
OPEN_SHEET_OVERFLOW_SCRIPT = """async () => {
    await Promise.all(document.getAnimations()
        .filter((animation) => animation.effect?.getComputedTiming().iterations !== Infinity)
        .map((animation) => animation.finished.catch(() => null)));
    const viewportWidth = document.documentElement.clientWidth;
    const problems = [];
    if (document.documentElement.scrollWidth > viewportWidth) {
        problems.push(`document ${document.documentElement.scrollWidth} px wide in ${viewportWidth}`);
    }
    const describe = (element) => element.id ? `#${element.id}`
        : element.dataset.testid ? `[data-testid=${element.dataset.testid}]`
        : `${element.tagName.toLowerCase()}.${String(element.className).split(" ").slice(0, 3).join(".")}`;
    for (const sheet of document.querySelectorAll('[role="dialog"], [role="alertdialog"]')) {
        for (const element of [sheet, ...sheet.querySelectorAll("*")]) {
            const style = getComputedStyle(element);
            if (style.display === "none" || style.visibility === "hidden" || element.closest(".sr-only")) continue;
            const box = element.getBoundingClientRect();
            if (box.width === 0) continue;
            if (box.right > viewportWidth + 1 || box.left < -1) {
                problems.push(`${describe(element)} spans ${Math.round(box.left)} to ${Math.round(box.right)} px`);
            }
            const scrollsItsOwnText = style.textOverflow === "ellipsis" || ["INPUT", "TEXTAREA"].includes(element.tagName);
            if (!scrollsItsOwnText && style.overflowX !== "visible" && element.scrollWidth > element.clientWidth + 1) {
                problems.push(`${describe(element)} holds ${element.scrollWidth} px in ${element.clientWidth}`);
            }
        }
    }
    return problems.slice(0, 12);
}"""


def check_open_sheets_fit_the_screen(page: Page, *, what: str) -> None:
    """With a sheet open: nothing in it reaches past the screen's edges, and nothing scrolls sideways."""
    problems = page.evaluate(OPEN_SHEET_OVERFLOW_SCRIPT)
    check(not problems, f"{what} is wider than the {page.viewport_size} screen: {problems}")


def decode_qr_code(qr_locator: Locator, *, screenshot_path: Path) -> str | None:
    """Screenshot the QR element and decode it with OpenCV; None when OpenCV finds no code."""
    import cv2  # noqa: PLC0415 (only this check needs it)

    qr_locator.screenshot(path=screenshot_path)
    image = cv2.imread(str(screenshot_path), cv2.IMREAD_GRAYSCALE)
    # OpenCV's classic detector misses some codes at one size and reads them at another (2026-10-09; a 576 px share QR
    # at 3x on 2026-10-14), so the ArUco-based detector reads first and the classic one tries a few sizes after it.
    decoded_text, _, _ = cv2.QRCodeDetectorAruco().detectAndDecode(image)
    if decoded_text:
        return decoded_text
    for border, scale in ((40, 1.0), (80, 1.0), (40, 1.5), (80, 0.75), (80, 0.5)):
        resized = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
        bordered = cv2.copyMakeBorder(resized, border, border, border, border, cv2.BORDER_CONSTANT, value=255)
        decoded_text, _, _ = cv2.QRCodeDetector().detectAndDecode(bordered)
        if decoded_text:
            return decoded_text
    return None


def main_status_title(page: Page) -> Locator:
    """Every payer screen has one `invoice_status` heading (one of the test ids the frontend keeps for the e2e)."""
    return page.get_by_test_id("invoice_status").first


def wait_for_page_status(
    page: Page, title: str, *, timeout_milliseconds: int = PAGE_STATUS_TIMEOUT_MILLISECONDS
) -> None:
    expect(main_status_title(page)).to_have_text(title, timeout=timeout_milliseconds)


def expect_pay_with_coin_button_text(payer_page: Page, *, asset_symbol: str, chain_name: str | None = None) -> None:
    """The coin choice's forward button names the amount, the coin and the network: "Pay 5.00 USDC on Polygon"."""
    chain_pattern = re.escape(chain_name) + "$" if chain_name else ""
    expect(payer_page.locator("#pay_with_coin_button")).to_have_text(
        re.compile(rf"^Pay [\d,]+\.\d+ {re.escape(asset_symbol)} on {chain_pattern}"), timeout=30_000
    )


def confirm_coin_and_network(payer_page: Page, *, asset_symbol: str) -> None:
    """The one tap before the address shows on an invoice whose coin is already fixed (one coin
    only): the coin row shows picked, no address or QR until the payer taps "Pay 5.00 USDC on Polygon"; then the send
    screen. A coin picked on the page commits and confirms with the same tap, so it needs no second one."""
    confirm_button = payer_page.locator("#pay_with_coin_button")
    expect_pay_with_coin_button_text(payer_page, asset_symbol=asset_symbol)
    check(payer_page.locator("#coin_choice_back_button").count() == 0, "a fixed coin offers Back")
    check(
        payer_page.get_by_test_id("invoice_deposit_address").count() == 0
        and payer_page.get_by_test_id("invoice_qr").count() == 0,
        "the address or QR shows before the coin and network are confirmed",
    )
    confirm_button.click()
    expect(payer_page.get_by_test_id("invoice_deposit_address")).to_be_visible(timeout=30_000)


# ---- The account form and the front page ----


@dataclass
class AccountFormEntry:
    currency: str
    assets: list[str]
    destination_address: str
    note: str
    amount: str
    lifetime_minutes: int | None = None
    # Above 1, / makes a quantity link (/l/...) instead of an invoice.
    quantity: int | None = None


def read_link_from_share_overlay(
    run: BrowserScenarioRun, maker_page: Page, *, who: str, link_path_pattern: str = r"/i/inv_"
) -> str:
    """After "Create link": the maker is on /i/inv_... (a quantity link: /l/lnk_...) under the share overlay, whose link
    is the address bar and whose QR reads the link. Returns the link."""
    maker_page.wait_for_url(re.compile(link_path_pattern))
    expect(maker_page.get_by_test_id("creator_share_overlay")).to_be_visible()
    check_open_sheets_fit_the_screen(maker_page, what=f"the {who}'s share overlay")
    link_url = str(maker_page.locator("#invoice_link_text").text_content()).strip()
    check(link_url == maker_page.url, f"the overlay's link {link_url!r} is not the address bar {maker_page.url!r}")
    run.take_screenshot(maker_page, f"{who}_share_overlay")
    decoded_link = decode_qr_code(
        maker_page.locator("#invoice_link_qr"),
        screenshot_path=run.context.scratch_folder / "screenshots" / f"{run.scenario_name}_{who}_qr.png",
    )
    run.context.write_log(f"{who}_link_qr", link_url=link_url, decoded=decoded_link)
    check(decoded_link == link_url, f"the {who}'s share QR reads {decoded_link!r}, the link is {link_url!r}")
    return link_url


def close_share_overlay_and_check_payer_preview(maker_page: Page) -> None:
    """The "Done" button shows the payer view under it, marked for the maker, with the share icon back to the overlay."""
    maker_page.click("#close_share_overlay_button")
    expect(maker_page.get_by_test_id("creator_share_overlay")).to_have_count(0)
    expect(maker_page.locator("#payer_preview_bar")).to_have_text("This is what your payer sees")
    expect(maker_page.locator("#reopen_share_overlay_button")).to_be_visible()


def check_payer_sees_no_maker_marks(payer_page: Page) -> None:
    check(payer_page.get_by_test_id("creator_share_overlay").count() == 0, "a payer sees the share overlay")
    check(payer_page.locator("#payer_preview_bar").count() == 0, "a payer sees the maker's preview bar")
    check(payer_page.locator("#reopen_share_overlay_button").count() == 0, "a payer sees the maker's share icon")


def create_link_on_account_page(run: BrowserScenarioRun, entry: AccountFormEntry) -> str:
    """Fill / the way an account does, submit, land on the invoice under the share overlay, check its link and QR,
    close it; the account's own invoice opened afresh still says it is the payer's view. Returns the link."""
    account_page = run.open_account_page()
    fill_account_form(run, account_page=account_page, entry=entry)
    account_page.click("#create_button")
    link_url = read_link_from_share_overlay(run, account_page, who="account")
    close_share_overlay_and_check_payer_preview(account_page)
    # The account's tab bar belongs to its own pages, never to a payer page, even signed in.
    check(account_page.locator("#account_tab_new").count() == 0, "the account tab bar shows on the payer page")
    # Without this tab's history entry, the signed-in owner is still told it is the payer's view, with no share icon.
    owner_page = run.watch_page(account_page.context.new_page())
    owner_page.goto(link_url)
    expect(owner_page.locator("#payer_preview_bar")).to_be_visible(timeout=30_000)
    check(
        owner_page.locator("#reopen_share_overlay_button").count() == 0, "a fresh tab got the creating tab's share icon"
    )
    account_page.context.close()
    return link_url


def fill_account_form(run: BrowserScenarioRun, *, account_page: Page, entry: AccountFormEntry) -> None:
    """/ filled as the entry says, signed in, up to the create button: the options in their sheet, the coins in theirs,
    the receiver behind "Change" and the note behind "Add a note"."""
    account_page.goto(f"{PUBLIC_BASE_URL}/")
    expect(account_page.locator("#account_sheet_trigger")).to_have_text(re.compile(r"^@"))
    expect(account_page.locator("#create_button")).to_be_enabled()
    expect(account_page.locator("#account_tab_new")).to_have_attribute("aria-current", "page")
    # A refused submit marks the field under itself, and typing clears it.
    account_page.fill("#amount_input", "")
    account_page.click("#create_button")
    expect(account_page.locator("#amount_error")).to_contain_text("amount")
    check(account_page.locator("#form_message_area").count() == 0, "/ put a field's error by the button")
    run.take_screenshot(account_page, "account_form_field_error")
    account_page.click("#invoice_options_button")
    expect(account_page.locator("#quantity_input")).to_be_visible()
    check_open_sheets_fit_the_screen(account_page, what="the options sheet")
    # The stack enables only local_fork, so the options show no chain picker.
    check(account_page.locator("#chain_select").count() == 0, "the options show a chain picker with one chain enabled")
    account_page.click("#currency_select")
    account_page.get_by_role("option", name=entry.currency, exact=True).click()
    if entry.quantity is not None:
        account_page.fill("#quantity_input", str(entry.quantity))
    if entry.lifetime_minutes is not None:
        account_page.fill("#lifetime_minutes_input", str(entry.lifetime_minutes))
    run.take_screenshot(account_page, "account_form_options")
    account_page.click("#invoice_options_done_button")
    expect(account_page.locator("#invoice_options_sheet")).to_have_count(0)
    account_page.fill("#amount_input", entry.amount)
    expect(account_page.locator("#amount_error")).to_have_count(0)
    account_page.click("#accepted_coins_button")
    coin_toggles = account_page.locator('[data-testid^="accepted_coin_toggle_"]')
    expect(coin_toggles.first).to_be_visible()
    check_open_sheets_fit_the_screen(account_page, what="the coins sheet on /")
    # Tick the wanted coins first, then untick the rest: the picker keeps at least one ticked.
    for wanted in (True, False):
        for coin_toggle in coin_toggles.all():
            symbol = str(coin_toggle.get_attribute("data-testid")).removeprefix("accepted_coin_toggle_")
            is_pressed = coin_toggle.get_attribute("aria-pressed") == "true"
            if coin_toggle.is_enabled() and (symbol in entry.assets) == wanted and is_pressed != wanted:
                coin_toggle.click()
    ticked_assets = [
        str(coin_toggle.get_attribute("data-testid")).removeprefix("accepted_coin_toggle_")
        for coin_toggle in coin_toggles.all()
        if coin_toggle.get_attribute("aria-pressed") == "true"
    ]
    check(sorted(ticked_assets) == sorted(entry.assets), f"account form coins {ticked_assets}, wanted {entry.assets}")
    account_page.click("#accepted_coins_done_button")
    expect(account_page.locator("#accepted_coins_summary")).to_have_text(", ".join(ticked_assets))
    # Signed in the receiver starts as the payout address, shown short; "Change" gives the whole field.
    account_page.click("#change_receiver_address_button")
    account_page.fill("#receiver_address_input", entry.destination_address)
    account_page.click("#add_note_button")
    account_page.fill("#note_input", entry.note)
    account_page.wait_for_timeout(
        500
    )  # the note opens with a Motion spring, which `animations="disabled"` doesn't skip
    run.take_screenshot(account_page, "account_form_filled")


def read_invoice_id_from_url(page_url: str) -> str:
    id_match = re.search(r"/i/(inv_[1-9A-HJ-NP-Za-km-z]+)$", page_url)
    if id_match is None:
        raise FlowFailedError(f"{page_url} is not an invoice page")
    return id_match.group(1)


def check_paid_and_swept(
    context: FlowContext, invoice_id: str, *, expected_event_types: set[str]
) -> tuple[dict[str, object], list[str]]:
    invoice = wait_for_invoice(context, invoice_id, description="final sweep", condition=last_sweep_final)
    account_event_types = wait_for_account_events(context, invoice_id, expected_types=expected_event_types)
    return invoice, account_event_types


# ---- Scenarios ----


def scenario_account_form_usdc_paid_with_browser_wallet(context: FlowContext, *, browser: Browser) -> dict[str, object]:
    run = BrowserScenarioRun(context=context, browser=browser, scenario_name="usdc_wallet")
    payer_account = PAYER_ACCOUNTS[0]
    fund_with_usdc(context.http_client, payer_account.address, base_units=20 * USDC_BASE_UNITS)
    destination = create_local_account().address
    note = "Browser e2e: sticker pack, invoice 1"
    link_url = create_link_on_account_page(
        run,
        AccountFormEntry(currency="USD", amount="5", assets=["USDC"], destination_address=destination, note=note,
                       lifetime_minutes=15),
    )  # fmt: skip
    invoice_id = read_invoice_id_from_url(link_url)
    check(link_url == f"{PUBLIC_BASE_URL}/i/{invoice_id}", f"account page link {link_url}")
    invoice = read_public_invoice(context, invoice_id)
    fiat = dict(invoice["fiat"])  # ty: ignore[no-matching-overload]
    check(fiat["currency"] == "USD" and fiat["amount"] == "5.00", f"fiat {fiat}")
    account_invoice = read_invoice(context, invoice_id)
    lifetime_seconds = (datetime.fromisoformat(str(account_invoice["expires_at"]))
                        - datetime.fromisoformat(str(account_invoice["created_at"]))).total_seconds()  # fmt: skip
    check(abs(lifetime_seconds - 15 * 60) < 5, f"the invoice lives {lifetime_seconds} s, the form asked 15 minutes")
    check(
        account_invoice["destination_address"] == destination, f"destination {account_invoice['destination_address']}"
    )
    wallet = InjectedBrowserWallet(http_client=context.http_client, account=payer_account)
    payer_page = run.watch_page(run.open_payer_context(wallet).new_page())
    payer_page.goto(link_url)
    wait_for_page_status(payer_page, "Awaiting payment", timeout_milliseconds=30_000)
    run.take_screenshot(payer_page, "payer_confirm_coin_network")
    confirm_coin_and_network(payer_page, asset_symbol="USDC")
    # Remembered for this tab: a reload goes straight to the address.
    payer_page.reload()
    expect(payer_page.get_by_test_id("invoice_deposit_address")).to_be_visible(timeout=30_000)
    check(payer_page.locator("#pay_with_coin_button").count() == 0, "a reload asked for the coin again")
    expect(payer_page.get_by_test_id("invoice_note")).to_have_text(note)
    expect(payer_page.get_by_test_id("invoice_crypto_amount")).to_have_text(
        format_amount_like_the_page(str(invoice["amount_due"]))
    )
    expect(payer_page.get_by_test_id("invoice_crypto_symbol")).to_have_text("USDC")
    fiat_line = payer_page.get_by_test_id("invoice_fiat_amount")
    expect(fiat_line).to_contain_text(format_fiat_like_the_page(str(fiat["usd"]), currency="USD"))
    expect(fiat_line).to_contain_text(format_fiat_like_the_page(str(fiat["eur"]), currency="EUR"))
    expect(payer_page.get_by_test_id("invoice_deposit_address")).to_have_text(str(invoice["deposit_address"]))
    expect(payer_page.get_by_test_id("invoice_countdown")).to_have_text(re.compile(r"^1[45]:\d\d$"))
    check(not page_scrolls_sideways(payer_page), "the payer page scrolls sideways at 390 px")
    qr_image = payer_page.get_by_test_id("invoice_qr_image")
    expect(qr_image).to_be_visible()
    payer_page.wait_for_function("image => image.complete && image.naturalWidth > 0", arg=qr_image.element_handle())
    run.take_screenshot(payer_page, "payer_invoice_open")
    decoded_payment_uri = decode_qr_code(
        payer_page.get_by_test_id("invoice_qr"),
        screenshot_path=context.scratch_folder / "screenshots" / f"{run.scenario_name}_payer_qr.png",
    )
    check(decoded_payment_uri == invoice["payment_uri"],
          f"the invoice QR reads {decoded_payment_uri!r}, the API's payment_uri is {invoice['payment_uri']!r}")  # fmt: skip

    # Hold the chain still so the payment sits unfinalized long enough for the page to show "confirming".
    call_anvil(context.http_client, "evm_setIntervalMining", 0)
    try:
        payer_page.click("#pay_with_wallet_button")
        expect(payer_page.get_by_test_id("invoice_wallet_message")).to_contain_text(
            "Sent. This page updates by itself", timeout=30_000
        )
        check(len(wallet.sent_transaction_hashes) == 1, f"wallet sent {wallet.sent_transaction_hashes}")
        # Sent and not yet seen: the button stays locked, so a second tap cannot pay twice.
        pay_button = payer_page.locator("#pay_with_wallet_button")
        expect(pay_button).to_be_disabled()
        expect(pay_button).to_have_text("Waiting for confirmation…")
        call_anvil(context.http_client, "evm_mine")
        wait_for_receipt(context.http_client, wallet.sent_transaction_hashes[0])
        wait_for_page_status(payer_page, "Confirming", timeout_milliseconds=30_000)
        # While confirming the API's amount_received is still 0; the page shows the amount seen in payments.
        expect(payer_page.get_by_test_id("invoice_confirming_amount")).to_contain_text(
            f"{format_amount_like_the_page(str(invoice['amount_due']))} USDC received"
        )
        run.take_screenshot(payer_page, "payer_confirming")
    finally:
        call_anvil(context.http_client, "evm_setIntervalMining", 1)
    wait_for_page_status(payer_page, "Paid")
    expect(payer_page.get_by_test_id("invoice_received_amount")).to_have_text(
        f"{format_amount_like_the_page(str(invoice['amount_due']))} USDC"
    )
    run.take_screenshot(payer_page, "payer_paid")
    invoice, account_event_types = check_paid_and_swept(
        context, invoice_id, expected_event_types={"invoice.payment_detected", "invoice.paid", "invoice.swept"}
    )
    amount_base_units = to_base_units(str(invoice["amount_due"]), decimals=6)
    destination_balance = read_usdc_balance(context.http_client, destination)
    check(invoice["status"] == "paid", f"status {invoice['status']}, expected paid")
    check(
        destination_balance == amount_base_units, f"destination holds {destination_balance}, wanted {amount_base_units}"
    )
    run.check_no_page_errors()
    payer_page.context.close()
    return {"invoice_id": invoice_id, "amount_due": invoice["amount_due"], "fiat": invoice["fiat"],
            "lifetime_seconds": lifetime_seconds, "payment_uri_from_qr": decoded_payment_uri,
            "wallet_methods": sorted(set(wallet.requested_methods)), "payment_tx": wallet.sent_transaction_hashes,
            "destination_usdc_base_units": destination_balance, "account_events": account_event_types,
            "screenshots": run.screenshot_paths}  # fmt: skip


def scenario_eur_invoice_underpaid_then_wallet_top_up(context: FlowContext, *, browser: Browser) -> dict[str, object]:
    run = BrowserScenarioRun(context=context, browser=browser, scenario_name="eur_underpaid")
    payer_account = PAYER_ACCOUNTS[1]
    destination = create_local_account().address
    note = "Browser e2e: tip jar"
    link_url = create_link_on_account_page(
        run,
        AccountFormEntry(currency="EUR", amount="3", assets=[NATIVE_SYMBOL], destination_address=destination, note=note),
    )  # fmt: skip
    invoice_id = read_invoice_id_from_url(link_url)
    wallet = InjectedBrowserWallet(http_client=context.http_client, account=payer_account)
    payer_page = run.watch_page(run.open_payer_context(wallet).new_page())
    payer_page.goto(link_url)
    wait_for_page_status(payer_page, "Awaiting payment", timeout_milliseconds=30_000)
    confirm_coin_and_network(payer_page, asset_symbol=NATIVE_SYMBOL)
    expect(payer_page.get_by_test_id("invoice_note")).to_have_text(note)
    invoice = read_public_invoice(context, invoice_id)
    fiat = dict(invoice["fiat"])  # ty: ignore[no-matching-overload]
    check(fiat["currency"] == "EUR" and fiat["amount"] == "3.00", f"fiat {fiat}")
    check(dict(invoice["asset"])["symbol"] == NATIVE_SYMBOL, f"asset {invoice['asset']}")  # ty: ignore[no-matching-overload]
    expect(payer_page.get_by_test_id("invoice_crypto_amount")).to_have_text(
        format_amount_like_the_page(str(invoice["amount_due"]))
    )
    run.take_screenshot(payer_page, "payer_invoice_open")

    amount_due_wei = to_base_units(str(invoice["amount_due"]), decimals=18)
    first_payment_wei = amount_due_wei // 2
    wait_for_receipt(
        context.http_client,
        send_transaction(context.http_client, sender=payer_account.address, to=str(invoice["deposit_address"]),
                         value_wei=first_payment_wei),
    )  # fmt: skip
    wait_for_page_status(payer_page, "Underpaid")
    invoice = read_public_invoice(context, invoice_id)
    remaining_text = str(invoice["amount_remaining"])
    # The hero is the rest to send, padded to as many places as the amount received beside it; the line says so.
    received_text = str(invoice["amount_received"])
    shared_places = max(2, len(remaining_text.partition(".")[2]), len(received_text.partition(".")[2]))
    expect(payer_page.get_by_test_id("invoice_crypto_amount")).to_have_text(
        format_amount_to_send_like_the_page(pad_decimal_places(remaining_text, shared_places))
    )
    underpaid_line = payer_page.get_by_test_id("invoice_underpaid_line")
    expect(underpaid_line).to_have_text(
        f"{format_amount_like_the_page(pad_decimal_places(received_text, shared_places))} {NATIVE_SYMBOL} arrived. "
        f"Send {format_amount_to_send_like_the_page(pad_decimal_places(remaining_text, shared_places))} {NATIVE_SYMBOL} more."
    )
    run.take_screenshot(payer_page, "payer_underpaid")
    payer_page.click("#pay_with_wallet_button")
    wait_for_page_status(payer_page, "Paid")
    check(len(wallet.sent_transaction_hashes) == 1, f"wallet sent {wallet.sent_transaction_hashes}")
    run.take_screenshot(payer_page, "payer_paid")
    invoice, account_event_types = check_paid_and_swept(
        context, invoice_id, expected_event_types={"invoice.underpaid", "invoice.paid", "invoice.swept"}
    )
    remaining_wei = to_base_units(remaining_text, decimals=18)
    destination_balance = read_native_balance(context.http_client, destination)
    check(invoice["status"] == "paid", f"status {invoice['status']}, expected paid")
    check(destination_balance == first_payment_wei + remaining_wei,
          f"destination holds {destination_balance} wei, wanted {first_payment_wei + remaining_wei}")  # fmt: skip
    run.check_no_page_errors()
    payer_page.context.close()
    return {"invoice_id": invoice_id, "amount_due": invoice["amount_due"], "fiat": invoice["fiat"], "payments_wei": [first_payment_wei, remaining_wei],
            "wallet_payment_tx": wallet.sent_transaction_hashes, "destination_wei": destination_balance,
            "account_events": account_event_types, "screenshots": run.screenshot_paths}  # fmt: skip


def create_link_signed_out_on_front_page(run: BrowserScenarioRun, *, receiver_address: str, note: str) -> str:
    """A visitor with no account on `/` (no account tab bar): an amount below $1 and a missing receiver are refused
    under their own fields, then the receiver, a note behind "Add a note", the $1 chip and "Create link" make a one-off
    invoice and land on its page under the
    share Drawer. A tap on a coin through the Drawer selects nothing; after "Done" the commit stays disabled for the
    maker; a reload keeps the share icon, not the Drawer; Back gives a fresh form. Returns the invoice page's URL."""
    visitor_context = run.browser.new_context(
        viewport=PAYER_PHONE_VIEWPORT,  # ty: ignore[invalid-argument-type]
        locale=BROWSER_LOCALE,
        device_scale_factor=3,
        is_mobile=True,
        has_touch=True,
    )
    visitor_page = run.watch_page(visitor_context.new_page())
    visitor_page.goto(f"{PUBLIC_BASE_URL}/")
    expect(visitor_page.locator("#account_sheet_trigger")).to_have_text("Sign in")
    expect(visitor_page.locator("#create_button")).to_be_enabled()
    check(visitor_page.locator("#account_tab_new").count() == 0, "a signed-out visitor sees the account tab bar")
    # The default coins are ticked: POL, USDC and USDT, whatever else the chain lists.
    expect(visitor_page.locator("#accepted_coins_summary")).to_have_text(f"{NATIVE_SYMBOL}, USDC, USDT")
    visitor_page.get_by_test_id("quick_amount_1").click()
    visitor_page.click("#create_button")
    expect(visitor_page.locator("#receiver_address_error")).to_contain_text("wallet address")
    check(visitor_page.locator("#add_note_button").count() == 0, "the note is offered before the address is valid")
    visitor_page.fill("#receiver_address_input", receiver_address)
    visitor_page.click("#add_note_button")
    visitor_page.fill("#note_input", note)
    visitor_page.wait_for_timeout(
        500
    )  # the note opens with a Motion spring, which `animations="disabled"` doesn't skip
    visitor_page.fill("#amount_input", "0.50")
    visitor_page.click("#create_button")
    expect(visitor_page.locator("#amount_error")).to_contain_text("$1")
    run.take_screenshot(visitor_page, "visitor_form_refusals")
    visitor_page.get_by_test_id("quick_amount_1").click()
    visitor_page.click("#create_button")
    link_url = read_link_from_share_overlay(run, visitor_page, who="visitor")
    # The overlay says where the money goes and in what, so a wrong paste is caught before sharing.
    expect(visitor_page.locator("#share_receiver_line")).to_contain_text(receiver_address[-4:])
    expect(visitor_page.locator("#share_receiver_line")).to_contain_text(f"{NATIVE_SYMBOL}, USDC, USDT")
    # A tap where a coin sits, through the open Drawer, must not reach it: the coin choice is one-way on the server.
    usdc_option = visitor_page.get_by_test_id("coin_option_USDC")
    expect(usdc_option).to_have_count(1, timeout=30_000)
    usdc_option_box = usdc_option.bounding_box()
    if usdc_option_box is None:
        raise FlowFailedError("no box for the USDC coin row")
    visitor_page.mouse.click(
        usdc_option_box["x"] + usdc_option_box["width"] / 2, usdc_option_box["y"] + usdc_option_box["height"] / 2
    )
    visitor_page.wait_for_timeout(600)
    check(usdc_option.get_attribute("aria-checked") == "false", "a tap through the share Drawer selected a coin")
    if visitor_page.get_by_test_id("creator_share_overlay").count() == 0:
        visitor_page.click("#reopen_share_overlay_button")
        expect(visitor_page.get_by_test_id("creator_share_overlay")).to_be_visible()
    close_share_overlay_and_check_payer_preview(visitor_page)
    run.take_screenshot(visitor_page, "visitor_payer_preview")
    usdc_option.click()
    expect(visitor_page.locator("#pay_with_coin_button")).to_be_disabled()
    visitor_page.click("#coin_choice_back_button")
    visitor_page.reload()
    expect(visitor_page.locator("#reopen_share_overlay_button")).to_be_visible(timeout=30_000)
    check(visitor_page.get_by_test_id("creator_share_overlay").count() == 0, "a reload reopened the share Drawer")
    visitor_page.go_back()
    visitor_page.wait_for_url(f"{PUBLIC_BASE_URL}/")
    expect(visitor_page.locator("#amount_input")).to_have_value("5")
    expect(visitor_page.locator("#accepted_coins_summary")).to_have_text(f"{NATIVE_SYMBOL}, USDC, USDT")
    check(visitor_page.locator("#receiver_address_input").input_value() == "", "Back kept the receiver")
    # "Make another" goes back to a form that keeps the receiver.
    visitor_page.go_forward()
    visitor_page.click("#reopen_share_overlay_button")
    visitor_page.click("#make_another_invoice_button")
    visitor_page.wait_for_url(f"{PUBLIC_BASE_URL}/")
    expect(visitor_page.locator("#receiver_address_input")).to_have_value(receiver_address)
    run.take_screenshot(visitor_page, "visitor_make_another_keeps_receiver")
    visitor_context.close()
    check(
        read_public_invoice(run.context, read_invoice_id_from_url(link_url))["status"] == "awaiting_asset",
        "the maker's taps committed a coin",
    )
    return link_url


def check_signed_out_operator_destination_invoice_gets_no_float_refund(
    run: BrowserScenarioRun, *, payer_account: LocalAccount
) -> str:
    """Signed out on `/`, a $1 invoice to the operator's own address, overpaid: the float refunds only the operator
    account's own invoices, so the receiver settles and nothing goes back. Returns the invoice id."""
    context = run.context
    link_url = create_link_signed_out_on_front_page(
        run, receiver_address=context.operator_destination_address, note="Browser e2e: signed out, one dollar"
    )
    invoice_id = read_invoice_id_from_url(link_url)
    check(
        run_sql(context, f"SELECT account_id IS NULL FROM invoices WHERE id = '{invoice_id}'") == "t",
        "a signed-out invoice has an account",
    )
    pick_reply = context.http_client.post(
        f"{API_URL}/public/invoices/{invoice_id}/asset", json={"asset": NATIVE_SYMBOL}
    )
    check(pick_reply.status_code == 200, f"pick {NATIVE_SYMBOL}: {pick_reply.status_code} {pick_reply.text}")
    wait_for_receipt(
        context.http_client,
        send_transaction(context.http_client, sender=payer_account.address,
                         to=str(pick_reply.json()["deposit_address"]), value_wei=40 * NATIVE_UNIT_WEI),
    )  # fmt: skip
    invoice = wait_for_invoice(
        context, invoice_id, description="overpaid and swept", signed_out=True,
        condition=lambda current: current["status"] == "overpaid" and last_sweep_final(current),
    )  # fmt: skip
    check(not invoice["refunds"], f"a signed-out invoice got a float refund: {invoice['refunds']}")
    refund_offer = dict(invoice["refund_offer"])  # ty: ignore[no-matching-overload]
    check(refund_offer["reason"] == "receiver_settles", f"signed-out refund offer: {refund_offer}")
    return invoice_id


def scenario_overpaid_refunded_by_itself(context: FlowContext, *, browser: Browser) -> dict[str, object]:
    """The account's $1 invoice on `/` to the operator's own address (only the operator account's own invoices paying
    it get float refunds); the payer picks POL, overpays and the excess comes back by itself. The same invoice made
    signed out is overpaid too and gets nothing back: the receiver settles."""
    run = BrowserScenarioRun(context=context, browser=browser, scenario_name="overpaid_refund")
    payer_account = PAYER_ACCOUNTS[2]
    destination = context.operator_destination_address
    signed_out_invoice_id = check_signed_out_operator_destination_invoice_gets_no_float_refund(
        run, payer_account=payer_account
    )
    destination_balance_before = read_native_balance(context.http_client, destination)
    link_url = create_link_on_account_page(
        run,
        AccountFormEntry(currency="USD", amount="1", assets=[NATIVE_SYMBOL, "USDC", "USDT"],
                         destination_address=destination, note="Browser e2e: one dollar"),
    )  # fmt: skip
    invoice_id = read_invoice_id_from_url(link_url)
    wallet = InjectedBrowserWallet(http_client=context.http_client, account=payer_account)
    payer_page = run.watch_page(run.open_payer_context(wallet).new_page())
    payer_page.goto(link_url)
    expect(main_status_title(payer_page)).to_have_text("Choose how to pay", timeout=30_000)
    check_payer_sees_no_maker_marks(payer_page)
    offered_assets = list(read_public_invoice(context, invoice_id)["assets"])  # ty: ignore[invalid-argument-type]
    check(
        offered_assets == [NATIVE_SYMBOL, "USDC", "USDT"],
        f"the invoice offers {offered_assets}, the form ticked {NATIVE_SYMBOL}, USDC, USDT",
    )
    check(payer_page.get_by_test_id(re.compile(r"^coin_option_")).count() == len(offered_assets), "coin rows differ")
    run.take_screenshot(payer_page, "payer_choose_coin")
    # Picking only selects; the forward button commits, back deselects.
    payer_page.get_by_test_id("coin_option_USDC").click()
    payer_page.click("#coin_choice_back_button")
    expect(payer_page.locator("#pay_with_coin_button")).to_have_count(0)
    payer_page.get_by_test_id("coin_option_POL").click()
    expect_pay_with_coin_button_text(payer_page, asset_symbol=NATIVE_SYMBOL)
    run.take_screenshot(payer_page, "payer_coin_selected")
    payer_page.click("#pay_with_coin_button")
    wait_for_page_status(payer_page, "Awaiting payment", timeout_milliseconds=30_000)
    # That tap confirmed the coin and network: the send screen, no second question.
    expect(payer_page.get_by_test_id("invoice_deposit_address")).to_be_visible(timeout=30_000)
    invoice = read_public_invoice(context, invoice_id)
    amount_due_wei = to_base_units(str(invoice["amount_due"]), decimals=18)
    paid_wei = 40 * NATIVE_UNIT_WEI
    wait_for_receipt(
        context.http_client,
        send_transaction(context.http_client, sender=payer_account.address, to=str(invoice["deposit_address"]),
                         value_wei=paid_wei),
    )  # fmt: skip
    payer_balance_before_refund = read_native_balance(context.http_client, payer_account.address)
    wait_for_page_status(payer_page, "Paid")
    expected_refund_wei = paid_wei - amount_due_wei
    # One sentence on the extra, ending on where it went and linked to the refund once it is final.
    expect(payer_page.get_by_test_id("invoice_refund_note")).to_have_text(
        re.compile(rf"^[\d,]+\.\d+ {NATIVE_SYMBOL} extra went back to {re.escape(payer_account.address[:6])}"),
        timeout=PAGE_STATUS_TIMEOUT_MILLISECONDS,
    )
    expect(payer_page.get_by_test_id("invoice_refund_note_refund_link")).to_be_visible()
    check(payer_page.locator("#refund_request_button").count() == 0, "an automatic refund offered a signing button")
    check(not wallet.signed_messages, f"the wallet was asked to sign {len(wallet.signed_messages)} messages")
    run.take_screenshot(payer_page, "payer_refund_sent")
    invoice = wait_for_invoice(
        context, invoice_id, description="final refund", condition=last_refund_final, signed_out=True
    )
    invoice = wait_for_invoice(
        context, invoice_id, description="final sweep", condition=last_sweep_final, signed_out=True
    )
    refunded_wei = read_native_balance(context.http_client, payer_account.address) - payer_balance_before_refund
    destination_balance = read_native_balance(context.http_client, destination) - destination_balance_before
    # A USD invoice keeps the dollar amount's exact worth, a hair under the rounded-up amount_due.
    recorded_refund_wei = to_base_units(str(invoice["refunds"][0]["amount"]), decimals=18)
    check(
        refunded_wei == recorded_refund_wei, f"payer got {refunded_wei} wei back, the refund says {recorded_refund_wei}"
    )
    check(abs(refunded_wei - expected_refund_wei) * 1000 <= amount_due_wei,
          f"refund {refunded_wei} wei is off the excess {expected_refund_wei} by over 0.1% of the amount due")  # fmt: skip
    check(destination_balance == paid_wei, f"destination got {destination_balance} wei, wanted {paid_wei}")
    account_event_types = wait_for_account_events(
        context, invoice_id, expected_types={"invoice.overpaid", "invoice.swept", "invoice.refunded"}
    )
    run.check_no_page_errors()
    payer_page.context.close()
    return {"invoice_id": invoice_id, "status": invoice["status"], "paid_wei": paid_wei, "refunded_wei": refunded_wei,
            "refunds": invoice["refunds"], "refund_offer_after": invoice["refund_offer"],
            "destination_wei": destination_balance, "account_events": account_event_types,
            "signed_out_invoice_id": signed_out_invoice_id, "screenshots": run.screenshot_paths}  # fmt: skip


def check_account_refund_offer_asks_first(run: BrowserScenarioRun, *, invoice_id: str, payer_address: str) -> None:
    """On /activity (desktop, so a Dialog) the underpaid invoice past its window is under "Needs you"; its "Resolve"
    opens the invoice's overlay, which offers exactly the API's refund senders, and a tap on one asks in an AlertDialog
    first; "Keep it" refunds nothing (the payer asks for it from the page next)."""
    account_page = run.open_account_page()
    account_page.goto(f"{PUBLIC_BASE_URL}/activity")
    needs_you_row = account_page.locator(f'[data-testid="needs_you_row"][data-invoice-id="{invoice_id}"]')
    expect(needs_you_row).to_contain_text("Underpaid. Accept what arrived or refund it.", timeout=30_000)
    run.take_screenshot(account_page, "account_needs_you")
    needs_you_row.get_by_test_id("resolve_needs_you_button").click()
    invoice_overlay = account_page.get_by_role("dialog")
    expect(invoice_overlay).to_be_visible(timeout=30_000)
    refund_sender_buttons = invoice_overlay.get_by_test_id("refund_sender_button")
    expect(refund_sender_buttons).to_have_count(1)
    expect(refund_sender_buttons.first).to_contain_text(f"Refund 20.00 {NATIVE_SYMBOL} to {payer_address[:6]}")
    refund_sender_buttons.first.click()
    refund_confirm = account_page.get_by_role("alertdialog")
    expect(refund_confirm).to_contain_text(f"Refund 20.00 {NATIVE_SYMBOL} to")
    run.take_screenshot(account_page, "account_refund_asks_first")
    refund_confirm.get_by_role("button", name="Keep it").click()
    expect(refund_confirm).to_have_count(0)
    account_page.context.close()


def scenario_underpaid_refund_requested_on_page_after_the_window(
    context: FlowContext, *, browser: Browser
) -> dict[str, object]:
    run = BrowserScenarioRun(context=context, browser=browser, scenario_name="underpaid_refund")
    payer_account = PAYER_ACCOUNTS[3]
    destination = context.operator_destination_address
    destination_balance_before = read_native_balance(context.http_client, destination)
    link_url = create_link_on_account_page(
        run,
        AccountFormEntry(currency=NATIVE_SYMBOL, amount="40", assets=[NATIVE_SYMBOL], destination_address=destination,
                       note=f"Browser e2e: forty {NATIVE_SYMBOL}"),
    )  # fmt: skip
    invoice_id = read_invoice_id_from_url(link_url)
    invoice = read_public_invoice(context, invoice_id)
    wallet = InjectedBrowserWallet(http_client=context.http_client, account=payer_account)
    payer_page = run.watch_page(run.open_payer_context(wallet).new_page())
    payer_page.goto(link_url)
    wait_for_page_status(payer_page, "Awaiting payment", timeout_milliseconds=30_000)
    confirm_coin_and_network(payer_page, asset_symbol=NATIVE_SYMBOL)
    paid_wei = 20 * NATIVE_UNIT_WEI
    wait_for_receipt(
        context.http_client,
        send_transaction(context.http_client, sender=payer_account.address, to=str(invoice["deposit_address"]),
                         value_wei=paid_wei),
    )  # fmt: skip
    wait_for_page_status(payer_page, "Underpaid")
    # Inside the payment window the underpaid screen asks only for the rest: no refund button and no refund line.
    expect(payer_page.get_by_test_id("invoice_underpaid_line")).to_contain_text(
        f"{NATIVE_SYMBOL} arrived.", timeout=30_000
    )
    check(
        payer_page.get_by_test_id("invoice_refund_note").count() == 0, "a refund line shows inside the payment window"
    )
    check(payer_page.locator("#refund_request_button").count() == 0, "a refund is offered inside the payment window")
    run.take_screenshot(payer_page, "payer_underpaid_window_open")
    # The fork cannot wait out a real window; the window ends in the database instead.
    run_sql(context, f"UPDATE invoices SET expires_at = now() - interval '1 second' WHERE id = '{invoice_id}'")
    refund_request_button = payer_page.locator("#refund_request_button")
    expect(refund_request_button).to_have_text("Get my money back", timeout=30_000)
    run.take_screenshot(payer_page, "payer_refund_offer")
    check_account_refund_offer_asks_first(run, invoice_id=invoice_id, payer_address=payer_account.address)
    payer_balance_before_refund = read_native_balance(context.http_client, payer_account.address)
    refund_request_button.click()
    # Once asked, the page says where the money goes and offers nothing to pay with: no "Refresh price", no wallet
    # payment, until the watcher closes it as Refunded (which may already have happened on a fast fork).
    expect(main_status_title(payer_page)).to_have_text(re.compile(r"^(Refund on its way|Refunded)$"), timeout=30_000)
    if main_status_title(payer_page).inner_text() == "Refund on its way":
        expect(payer_page.get_by_test_id("invoice_refund_underway_line")).to_contain_text(
            f"Your 20.00 {NATIVE_SYMBOL} is on its way back to {payer_account.address[:6]}"
        )
    for payment_control in ("#refresh_price_button", "#pay_with_wallet_button", "#refund_request_button"):
        check(
            payer_page.locator(payment_control).count() == 0, f"{payment_control} still shows after the refund request"
        )
    check(len(wallet.signed_messages) == 1, f"wallet signed {len(wallet.signed_messages)} messages")
    check(invoice_id in wallet.signed_messages[0], "the signed refund message does not name the invoice")
    payer_page.wait_for_timeout(600)  # the screen's fade-in (Motion, not CSS) settles before the shot
    run.take_screenshot(payer_page, "payer_refund_requested")
    wait_for_page_status(payer_page, "Refunded")
    expect(payer_page.get_by_test_id("invoice_closed_line")).to_have_text(
        f"Your 20.00 {NATIVE_SYMBOL} went back to {payer_account.address[:6]}…{payer_account.address[-4:]}",
        timeout=PAGE_STATUS_TIMEOUT_MILLISECONDS,
    )
    expect(payer_page.get_by_test_id("invoice_closed_line_refund_link")).to_be_visible()
    run.take_screenshot(payer_page, "payer_refund_sent")
    invoice = wait_for_invoice(context, invoice_id, description="final refund", condition=last_refund_final)
    invoice = wait_for_invoice(context, invoice_id, description="final sweep", condition=last_sweep_final)
    refunded_wei = read_native_balance(context.http_client, payer_account.address) - payer_balance_before_refund
    destination_balance = read_native_balance(context.http_client, destination) - destination_balance_before
    check(refunded_wei == paid_wei, f"payer got {refunded_wei} wei back, wanted {paid_wei}")
    check(destination_balance == paid_wei, f"destination got {destination_balance} wei, wanted {paid_wei}")
    run.check_no_page_errors()
    payer_page.context.close()
    return {"invoice_id": invoice_id, "status": invoice["status"], "refunded_wei": refunded_wei,
            "refunds": invoice["refunds"], "destination_wei": destination_balance,
            "screenshots": run.screenshot_paths}  # fmt: skip


def open_phone_page(run: BrowserScenarioRun, *, color_scheme: str = "light", signed_in: bool = False) -> Page:
    """A 390 px phone page, light or dark, signed out or as the account; no motion in the screenshots' way."""
    phone_context = run.browser.new_context(
        viewport=PAYER_PHONE_VIEWPORT,  # ty: ignore[invalid-argument-type]
        locale=BROWSER_LOCALE,
        device_scale_factor=3,
        is_mobile=True,
        has_touch=True,
        reduced_motion="reduce",
        color_scheme=color_scheme,  # ty: ignore[invalid-argument-type]
    )
    if signed_in:
        phone_context.add_cookies([{
            "name": ACCOUNT_SESSION_COOKIE_NAME, "value": run.context.account_session_token,
            "domain": PUBLIC_BASE_HOST, "path": "/", "secure": True, "httpOnly": True, "sameSite": "Strict",
        }])  # fmt: skip
    return run.watch_page(phone_context.new_page())


def take_slot_on_link_page(payer_page: Page, *, link_url: str, asset_symbol: str) -> str:
    """The payer's way through /l/: pick the coin, "Pay 2.00 X on Polygon" (the one tap that takes the slot and
    confirms the coin and network), land on the invoice's send screen. Returns its id."""
    payer_page.goto(link_url)
    expect(payer_page.get_by_test_id("payment_link_status")).to_have_text("Choose how to pay", timeout=30_000)
    payer_page.get_by_test_id(f"coin_option_{asset_symbol}").click()
    expect_pay_with_coin_button_text(payer_page, asset_symbol=asset_symbol)
    payer_page.click("#pay_with_coin_button")
    payer_page.wait_for_url(re.compile(r"/i/inv_"))
    wait_for_page_status(payer_page, "Awaiting payment", timeout_milliseconds=30_000)
    expect(payer_page.get_by_test_id("invoice_deposit_address")).to_be_visible(timeout=30_000)
    return read_invoice_id_from_url(payer_page.url)


def read_payment_link(context: FlowContext, *, payment_link_id: str) -> dict[str, object]:
    reply = context.http_client.get(f"{API_URL}/payment-links/{payment_link_id}", headers=context.account_headers)
    reply.raise_for_status()
    return reply.json()


def request_last_slot_twice_at_once(payment_link_id: str) -> list[httpx.Response]:
    """Two payers' slot requests released together by a barrier, each on its own connection."""
    start_barrier = threading.Barrier(2)

    def request_slot() -> httpx.Response:
        with httpx.Client(timeout=60) as payer_client:
            start_barrier.wait()
            return payer_client.post(
                f"{API_URL}/public/payment-links/{payment_link_id}/invoices", json={"asset": "USDC"}
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        return list(executor.map(lambda _: request_slot(), range(2)))


def scenario_quantity_link_two_payers_then_sold_out(context: FlowContext, *, browser: Browser) -> dict[str, object]:
    """5. The account makes a quantity-2 link on /; payer A picks USDC and pays from the wallet, payer B picks POL
    and pays; a third payer sees "Sold out" and the API refuses 409 `sold_out`; /activity's Links tab says "Sold 2 of
    2" with both payers. Race: on a quantity-1 link two slot requests at once get exactly one invoice and one 409; the
    page says "All taken for now"; the account closes that link from /activity and the page says "Link closed"."""
    run = BrowserScenarioRun(context=context, browser=browser, scenario_name="quantity_link")
    usdc_payer, pol_payer = PAYER_ACCOUNTS[4], PAYER_ACCOUNTS[5]
    fund_with_usdc(context.http_client, usdc_payer.address, base_units=20 * USDC_BASE_UNITS)
    destination = create_local_account().address
    destination_pol_before = read_native_balance(context.http_client, destination)
    note = "Browser e2e: sticker pack, 2 for sale"

    # The maker: / with "How many payers" 2 lands on /l/... under the share overlay; the maker's own tap cannot buy.
    account_page = run.open_account_page()
    fill_account_form(run, account_page=account_page, entry=AccountFormEntry(
        currency="USD", amount="2", assets=[NATIVE_SYMBOL, "USDC"], destination_address=destination, note=note, quantity=2,
    ))  # fmt: skip
    expect(account_page.locator("#create_button")).to_have_text("Create link for 2")
    account_page.click("#create_button")
    link_url = read_link_from_share_overlay(run, account_page, who="account", link_path_pattern=r"/l/lnk_")
    close_share_overlay_and_check_payer_preview(account_page)
    account_page.get_by_test_id("coin_option_USDC").click()
    expect(account_page.locator("#pay_with_coin_button")).to_be_disabled()
    account_page.context.close()
    payment_link_id = link_url.rsplit("/", 1)[-1]
    payment_link = read_payment_link(context, payment_link_id=payment_link_id)
    check(payment_link["quantity"] == 2 and payment_link["slots_left"] == 2, f"new link {payment_link}")
    check(payment_link["destination_address"] == destination, f"link destination {payment_link['destination_address']}")
    check(payment_link["status"] == "open", f"new link status {payment_link['status']}")

    # Payer A: USDC from the wallet button.
    usdc_wallet = InjectedBrowserWallet(http_client=context.http_client, account=usdc_payer)
    usdc_payer_page = run.watch_page(run.open_payer_context(usdc_wallet).new_page())
    usdc_payer_page.goto(link_url)
    expect(usdc_payer_page.get_by_test_id("payment_link_slots_left")).to_have_text("2 of 2 left", timeout=30_000)
    expect(usdc_payer_page.get_by_test_id("payment_link_note")).to_have_text(note)
    check(usdc_payer_page.locator("#payer_preview_bar").count() == 0, "a payer sees the maker's preview bar")
    check(not page_scrolls_sideways(usdc_payer_page), "the link page scrolls sideways at 390 px")
    run.take_screenshot(usdc_payer_page, "payer_link_open")
    usdc_invoice_id = take_slot_on_link_page(usdc_payer_page, link_url=link_url, asset_symbol="USDC")
    usdc_payer_page.click("#pay_with_wallet_button")
    wait_for_page_status(usdc_payer_page, "Paid")
    # Back on the link: the payer's own invoice again, not a second slot.
    usdc_payer_page.go_back()
    expect(usdc_payer_page.get_by_test_id("payment_link_status")).to_have_text("You started paying", timeout=30_000)
    expect(usdc_payer_page.locator("#payment_link_continue_button")).to_be_visible()
    usdc_payer_page.context.close()

    # Payer B: POL, one slot left.
    pol_wallet = InjectedBrowserWallet(http_client=context.http_client, account=pol_payer)
    pol_payer_page = run.watch_page(run.open_payer_context(pol_wallet).new_page())
    pol_payer_page.goto(link_url)
    expect(pol_payer_page.get_by_test_id("payment_link_slots_left")).to_have_text("1 of 2 left", timeout=30_000)
    pol_payer_page.get_by_test_id("coin_option_POL").click()
    run.take_screenshot(pol_payer_page, "payer_link_coin_picked")
    pol_invoice_id = take_slot_on_link_page(pol_payer_page, link_url=link_url, asset_symbol=NATIVE_SYMBOL)
    pol_payer_page.click("#pay_with_wallet_button")
    wait_for_page_status(pol_payer_page, "Paid")
    pol_payer_page.context.close()

    usdc_invoice, usdc_events = check_paid_and_swept(
        context, usdc_invoice_id, expected_event_types={"invoice.paid", "invoice.swept"}
    )
    pol_invoice, pol_events = check_paid_and_swept(
        context, pol_invoice_id, expected_event_types={"invoice.paid", "invoice.swept"}
    )
    for payer_invoice in (usdc_invoice, pol_invoice):
        check(payer_invoice["payment_link_id"] == payment_link_id, f"invoice link {payer_invoice['payment_link_id']}")
        check(payer_invoice["status"] == "paid", f"payer invoice status {payer_invoice['status']}")
        check(dict(payer_invoice["fiat"])["amount"] == "2.00", f"payer invoice fiat {payer_invoice['fiat']}")  # ty: ignore[no-matching-overload]
    destination_usdc = read_usdc_balance(context.http_client, destination)
    destination_pol = read_native_balance(context.http_client, destination) - destination_pol_before
    check(destination_usdc == to_base_units(str(usdc_invoice["amount_due"]), decimals=6),
          f"destination holds {destination_usdc} USDC base units")  # fmt: skip
    check(destination_pol == to_base_units(str(pol_invoice["amount_due"]), decimals=18),
          f"destination got {destination_pol} wei")  # fmt: skip

    # A third payer: sold out, on the page and from the API.
    for color_scheme in ("light", "dark"):
        late_payer_page = open_phone_page(run, color_scheme=color_scheme)
        late_payer_page.goto(link_url)
        expect(late_payer_page.get_by_test_id("payment_link_status")).to_have_text("Sold out", timeout=30_000)
        check(late_payer_page.get_by_test_id("coin_option_USDC").count() == 0, "a sold-out link shows coins")
        run.take_screenshot(late_payer_page, f"payer_link_sold_out_{color_scheme}")
        late_payer_page.context.close()
    sold_out_reply = context.http_client.post(
        f"{API_URL}/public/payment-links/{payment_link_id}/invoices", json={"asset": "USDC"}
    )
    check(sold_out_reply.status_code == 409 and sold_out_reply.json()["code"] == "sold_out",
          f"a fourth slot answered {sold_out_reply.status_code} {sold_out_reply.text}")  # fmt: skip
    payment_link = read_payment_link(context, payment_link_id=payment_link_id)
    check(payment_link["status"] == "sold_out" and payment_link["paid_count"] == 2, f"sold link {payment_link}")
    check(len(list(payment_link["invoices"])) == 2, f"link invoices {payment_link['invoices']}")  # ty: ignore[invalid-argument-type]

    # The account's Links tab, at phone width.
    for color_scheme in ("light", "dark"):
        activity_page = open_phone_page(run, color_scheme=color_scheme, signed_in=True)
        activity_page.goto(f"{PUBLIC_BASE_URL}/activity")
        activity_page.click("#payment_links_tab")
        link_row_sales = activity_page.get_by_test_id("payment_link_row_sales").first
        expect(link_row_sales).to_contain_text("Sold 2 of 2", timeout=30_000)
        run.take_screenshot(activity_page, f"account_links_tab_{color_scheme}")
        activity_page.get_by_test_id("payment_link_row").first.click()
        expect(activity_page.locator("#payment_link_invoice_list > *")).to_have_count(2, timeout=30_000)
        check_open_sheets_fit_the_screen(activity_page, what="the link Drawer on /activity")
        run.take_screenshot(activity_page, f"account_link_drawer_{color_scheme}")
        activity_page.context.close()

    # The race: two payers ask for the only slot of a quantity-1 link at the same moment.
    race_link_reply = context.http_client.post(
        f"{API_URL}/payment-links",
        headers=context.account_headers,
        json={"amount": "1", "currency": "USD", "chain": FORK_CHAIN_KEY, "assets": ["USDC"], "quantity": 1,
              "destination_address": destination, "note": "Browser e2e: the last one"},
    )  # fmt: skip
    race_link_reply.raise_for_status()
    race_link_id = str(race_link_reply.json()["id"])
    race_replies = request_last_slot_twice_at_once(race_link_id)
    race_statuses = sorted(reply.status_code for reply in race_replies)
    context.write_log(
        "quantity_link_race", statuses=race_statuses, codes=[reply.json().get("code") for reply in race_replies]
    )
    check(race_statuses == [201, 409], f"two requests for the last slot answered {race_statuses}")
    check(next(reply for reply in race_replies if reply.status_code == 409).json()["code"] == "sold_out",
          "the losing slot request was not refused as sold_out")  # fmt: skip
    race_invoice_count = run_sql(context, f"SELECT count(*) FROM invoices WHERE payment_link_id = '{race_link_id}'")  # noqa: S608
    check(race_invoice_count == "1", f"the race link has {race_invoice_count} invoices, wanted 1")
    race_link_url = f"{PUBLIC_BASE_URL}/l/{race_link_id}"
    race_payer_page = open_phone_page(run)
    race_payer_page.goto(race_link_url)
    expect(race_payer_page.get_by_test_id("payment_link_status")).to_have_text("All taken for now", timeout=30_000)
    run.take_screenshot(race_payer_page, "payer_link_all_taken")

    # The account closes it from /activity (after the confirm); the payer page then says so.
    closing_page = open_phone_page(run, signed_in=True)
    closing_page.goto(f"{PUBLIC_BASE_URL}/activity")
    closing_page.click("#payment_links_tab")
    closing_page.get_by_test_id("payment_link_row").filter(has_text="the last one").click()
    closing_page.click("#close_payment_link_button")
    run.take_screenshot(closing_page, "account_link_close_confirm")
    closing_page.click("#confirm_close_payment_link_button")
    expect(closing_page.get_by_test_id("payment_link_row").filter(has_text="the last one")).to_contain_text(
        "Closed", timeout=30_000
    )
    closing_page.context.close()
    race_payer_page.reload()
    expect(race_payer_page.get_by_test_id("payment_link_status")).to_have_text("Link closed", timeout=30_000)
    race_payer_page.context.close()
    closed_reply = context.http_client.post(
        f"{API_URL}/public/payment-links/{race_link_id}/invoices", json={"asset": "USDC"}
    )
    check(closed_reply.status_code == 409 and closed_reply.json()["code"] == "payment_link_closed",
          f"a closed link answered {closed_reply.status_code} {closed_reply.text}")  # fmt: skip
    run.check_no_page_errors()
    return {"payment_link_id": payment_link_id, "invoices": [usdc_invoice_id, pol_invoice_id],
            "usdc_events": usdc_events, "pol_events": pol_events, "destination_usdc_base_units": destination_usdc,
            "destination_pol_wei": destination_pol, "race_link_id": race_link_id, "race_statuses": race_statuses,
            "screenshots": run.screenshot_paths}  # fmt: skip


def find_final_stray_refund(invoice: dict[str, object], *, asset_symbol: str) -> dict[str, object] | None:
    refunds = invoice["refunds"]
    if not isinstance(refunds, list):
        return None
    return next(
        (refund for refund in refunds if refund["asset_symbol"] == asset_symbol and refund["status"] == "final"), None
    )


def wait_for_final_stray_payments(context: FlowContext, invoice_id: str, *, description: str) -> dict[str, object]:
    return wait_for_invoice(
        context, invoice_id, description=description,
        condition=lambda current: bool(current["stray_payments"]) and all(
            stray["final"] for stray in current["stray_payments"]  # ty: ignore[not-iterable]
        ),
    )  # fmt: skip


def scenario_wrong_coin_shown_and_sent_back(context: FlowContext, *, browser: Browser) -> dict[str, object]:
    """6. INC-001: a 25 USD invoice in USDC only, to the operator's address; the payer sends 50.25 POL instead.
    Within seconds the payer page says so in a calm Alert, the invoice stays awaiting payment with nothing received;
    once final the POL goes back to the payer by itself, in full (the float pays the gas), and the account sees
    "Wrong coin received" on /activity with no button to press. Once the invoice expires the rescan sweeps the POL to
    the receiver, not flagged late, and the sweep shows with its coin. Then INC-001 as it happened: POL into a closed
    deposit is swept as late money, and the operator's `record-stray-payment-from-transaction` records it so it goes
    back by itself, once only. Last, an invoice to someone else's address: nothing goes back, the API refuses a
    refund, and both pages say the seller settles it."""
    run = BrowserScenarioRun(context=context, browser=browser, scenario_name="wrong_coin")
    payer_account = PAYER_ACCOUNTS[6]
    destination = context.operator_destination_address
    destination_balance_before = read_native_balance(context.http_client, destination)
    note = f"Browser e2e: 25 USDC, paid in {NATIVE_SYMBOL}"
    invoice = create_invoice(context, amount="25", currency="USD", assets=["USDC"], destination_address=destination,
                             note=note)  # fmt: skip
    invoice_id = str(invoice["id"])
    wallet = InjectedBrowserWallet(http_client=context.http_client, account=payer_account)
    payer_page = run.watch_page(run.open_payer_context(wallet).new_page())
    payer_page.goto(f"{PUBLIC_BASE_URL}/i/{invoice_id}")
    wait_for_page_status(payer_page, "Awaiting payment", timeout_milliseconds=30_000)
    confirm_coin_and_network(payer_page, asset_symbol="USDC")
    expect(payer_page.get_by_test_id("invoice_crypto_symbol")).to_have_text("USDC")

    stray_amount_text = "50.25"
    stray_wei = to_base_units(stray_amount_text, decimals=18)
    payer_balance_before_send = read_native_balance(context.http_client, payer_account.address)
    stray_transaction_hash = send_transaction(
        context.http_client, sender=payer_account.address, to=str(invoice["deposit_address"]), value_wei=stray_wei
    )
    sent_at = time.monotonic()
    receipt = wait_for_receipt(context.http_client, stray_transaction_hash)
    gas_paid_wei = int(receipt["gasUsed"], 16) * int(receipt["effectiveGasPrice"], 16)
    stray_line = payer_page.get_by_test_id("invoice_stray_payment_line")
    # The fork finalizes within seconds, so the refund may already be final by the first poll.
    expect(stray_line).to_have_text(
        re.compile(
            rf"^We got {re.escape(stray_amount_text)} {NATIVE_SYMBOL}\. This link takes USDC only, so it(?:'s being| was) sent back "
            r"to the address it came from\.$"
        ),
        timeout=30_000,
    )
    seconds_until_shown = round(time.monotonic() - sent_at, 1)
    check(seconds_until_shown <= 15, f"the wrong-coin Alert took {seconds_until_shown} s")
    check(main_status_title(payer_page).inner_text() == "Awaiting payment", "the wrong coin changed the payer screen")
    check(not page_scrolls_sideways(payer_page), "the payer page scrolls sideways at 390 px")
    run.take_screenshot(payer_page, "payer_wrong_coin_alert")

    invoice = wait_for_final_stray_payments(context, invoice_id, description="final stray payment")
    stray_payments = list(invoice["stray_payments"])  # ty: ignore[invalid-argument-type]
    check(len(stray_payments) == 1, f"stray payments {stray_payments}")
    stray_payment = dict(stray_payments[0])  # ty: ignore[no-matching-overload]
    check(
        stray_payment["asset_symbol"] == NATIVE_SYMBOL
        and stray_payment["amount"] == stray_amount_text
        and stray_payment["sender"] == payer_account.address
        and stray_payment["tx_hash"] == stray_transaction_hash
        and stray_payment["refund_handling"] == "automatic",
        f"stray payment {stray_payment}",
    )
    check(invoice["status"] == "awaiting_payment" and invoice["amount_received"] == "0",
          f"the wrong coin counted: status {invoice['status']}, received {invoice['amount_received']}")  # fmt: skip
    check(not invoice["payments"] and not invoice["late_payment"], f"payments {invoice['payments']}")

    # Back by itself once final: nobody presses anything.
    invoice = wait_for_invoice(
        context, invoice_id, description="final automatic stray refund",
        condition=lambda current: find_final_stray_refund(current, asset_symbol=NATIVE_SYMBOL) is not None,
    )  # fmt: skip
    stray_refund = find_final_stray_refund(invoice, asset_symbol=NATIVE_SYMBOL) or {}
    pol_refunds = [refund for refund in invoice["refunds"] if refund["asset_symbol"] == NATIVE_SYMBOL]  # ty: ignore[not-iterable]
    check(len(pol_refunds) == 1, f"{NATIVE_SYMBOL} refunds {pol_refunds}")
    payer_net_wei = read_native_balance(context.http_client, payer_account.address) - payer_balance_before_send
    check(payer_net_wei == -gas_paid_wei,
          f"the payer is down {-payer_net_wei} wei, only its own gas {gas_paid_wei} expected")  # fmt: skip
    check(stray_refund["recipient"] == payer_account.address and stray_refund["amount"] == stray_amount_text,
          f"stray refund {stray_refund}")  # fmt: skip
    check(invoice["status"] == "awaiting_payment" and invoice["amount_received"] == "0",
          f"the refund changed the invoice: {invoice['status']}, {invoice['amount_received']}")  # fmt: skip
    expect(stray_line).to_have_text(
        f"We got {stray_amount_text} {NATIVE_SYMBOL}. This link takes USDC only, so it was sent back to the address it came from.",
        timeout=30_000,
    )
    expect(payer_page.get_by_test_id("invoice_refund_row")).to_contain_text(stray_amount_text)
    expect(main_status_title(payer_page)).to_have_text("Awaiting payment")
    run.take_screenshot(payer_page, "payer_wrong_coin_sent_back")
    wait_for_account_events(context, invoice_id, expected_types={"invoice.stray_payment", "invoice.refunded"})

    # The account, on a phone: the event and the invoice's Drawer say what happened; nothing to press.
    activity_page = open_phone_page(run, signed_in=True)
    activity_page.goto(f"{PUBLIC_BASE_URL}/activity")
    activity_page.get_by_role("tab", name="Updates").click()
    expect(activity_page.locator("#event_list")).to_contain_text("Wrong coin received", timeout=30_000)
    run.take_screenshot(activity_page, "account_wrong_coin_event")
    activity_page.get_by_role("tab", name="Invoices").click()
    activity_page.locator("#invoice_list button", has_text=note).first.click()
    invoice_overlay = activity_page.get_by_test_id("invoice_actions_overlay")
    expect(invoice_overlay.get_by_test_id("invoice_stray_payment_block")).to_contain_text(
        f"{stray_amount_text} {NATIVE_SYMBOL} from {payer_account.address[:6]}", timeout=30_000
    )
    check_open_sheets_fit_the_screen(activity_page, what="the invoice Drawer on /activity")
    expect(invoice_overlay.get_by_test_id("stray_refund_handling_line")).to_have_text(
        "Sent back to the wallet it came from."
    )
    activity_page.wait_for_timeout(600)  # the Drawer's slide settles before the shot
    run.take_screenshot(activity_page, "account_wrong_coin_drawer")
    activity_page.context.close()

    # Closed by the clock, the stray POL goes to the receiver; recorded while live, so it is not late money.
    run_sql(context, f"UPDATE invoices SET expires_at = now() - interval '1 second' WHERE id = '{invoice_id}'")
    invoice = wait_for_invoice(
        context, invoice_id, description=f"stray {NATIVE_SYMBOL} swept",
        condition=lambda current: any(
            sweep["status"] == "final" and sweep["asset_symbol"] == NATIVE_SYMBOL
            for sweep in current["sweeps"]  # ty: ignore[not-iterable]
        ),
    )  # fmt: skip
    pol_sweeps = [sweep for sweep in invoice["sweeps"] if sweep["asset_symbol"] == NATIVE_SYMBOL]  # ty: ignore[not-iterable]
    check(pol_sweeps[0]["amount"] == stray_amount_text, f"{NATIVE_SYMBOL} sweeps {pol_sweeps}")
    check(dict(invoice["stray_payments"][0])["swept"] is True, f"stray payments {invoice['stray_payments']}")  # ty: ignore[not-subscriptable, no-matching-overload]
    check(invoice["status"] == "expired" and not invoice["late_payment"],
          f"after the sweep: {invoice['status']}, late_payment {invoice['late_payment']}")  # fmt: skip
    destination_gain = read_native_balance(context.http_client, destination) - destination_balance_before
    check(
        destination_gain == stray_wei,
        f"the receiver got {destination_gain} wei, the stray {NATIVE_SYMBOL} was {stray_wei}",
    )
    wait_for_page_status(payer_page, "Expired")
    expect(stray_line).to_contain_text("so it was sent back to the address it came from.")
    payer_page.context.close()
    account_event_types = wait_for_account_events(
        context, invoice_id, expected_types={"invoice.stray_payment", "invoice.refunded", "invoice.expired",
                                             "invoice.swept"},
    )  # fmt: skip
    check("invoice.late_payment" not in account_event_types, f"events {account_event_types}")

    # INC-001 as it happened: POL into a deposit the watcher no longer reads is swept as late money, unseen as stray;
    # the operator records it from its transaction hash and it goes back by itself, once.
    late_payer = PAYER_ACCOUNTS[8]
    closed_invoice = create_invoice(context, amount="25", currency="USD", assets=["USDC"],
                                    destination_address=destination, note="Browser e2e: INC-001 replay")  # fmt: skip
    closed_invoice_id = str(closed_invoice["id"])
    run_sql(context, f"UPDATE invoices SET expires_at = now() - interval '1 second' WHERE id = '{closed_invoice_id}'")
    wait_for_invoice(context, closed_invoice_id, description="expired",
                     condition=lambda current: current["status"] == "expired")  # fmt: skip
    late_amount_wei = to_base_units("30", decimals=18)
    late_payer_balance_before = read_native_balance(context.http_client, late_payer.address)
    late_transaction_hash = send_transaction(context.http_client, sender=late_payer.address,
                                             to=str(closed_invoice["deposit_address"]), value_wei=late_amount_wei)  # fmt: skip
    late_receipt = wait_for_receipt(context.http_client, late_transaction_hash)
    late_gas_wei = int(late_receipt["gasUsed"], 16) * int(late_receipt["effectiveGasPrice"], 16)
    closed_invoice = wait_for_invoice(
        context, closed_invoice_id, description=f"late {NATIVE_SYMBOL} swept",
        condition=lambda current: current["late_payment"] is True and any(
            sweep["status"] == "final" and sweep["asset_symbol"] == NATIVE_SYMBOL
            for sweep in current["sweeps"]  # ty: ignore[not-iterable]
        ),
    )  # fmt: skip
    check(not closed_invoice["stray_payments"] and not closed_invoice["refunds"],
          f"before the CLI: {closed_invoice['stray_payments']} {closed_invoice['refunds']}")  # fmt: skip
    not_into_deposit_exit_code, not_into_deposit_output = run_operator_cli(
        context, "record-stray-payment-from-transaction", str(pol_sweeps[0]["tx_hash"]), "--chain", FORK_CHAIN_KEY
    )
    check(
        not_into_deposit_exit_code == 1 and "refused" in not_into_deposit_output,
        f"the sweep transaction recorded as stray: {not_into_deposit_output}",
    )
    cli_exit_code, cli_output = run_operator_cli(
        context, "record-stray-payment-from-transaction", late_transaction_hash, "--chain", FORK_CHAIN_KEY
    )
    check(
        cli_exit_code == 0 and closed_invoice_id in cli_output, f"record-stray-payment-from-transaction: {cli_output}"
    )
    closed_invoice = wait_for_invoice(
        context, closed_invoice_id, description=f"final automatic refund of the recorded stray {NATIVE_SYMBOL}",
        condition=lambda current: find_final_stray_refund(current, asset_symbol=NATIVE_SYMBOL) is not None,
    )  # fmt: skip
    late_payer_net_wei = read_native_balance(context.http_client, late_payer.address) - late_payer_balance_before
    check(
        late_payer_net_wei == -late_gas_wei, f"the INC-001 payer is down {-late_payer_net_wei} wei, gas {late_gas_wei}"
    )
    repeat_exit_code, repeat_output = run_operator_cli(
        context, "record-stray-payment-from-transaction", late_transaction_hash, "--chain", FORK_CHAIN_KEY
    )
    check(repeat_exit_code == 1 and "already recorded" in repeat_output, f"recorded twice: {repeat_output}")
    closed_invoice = read_invoice(context, closed_invoice_id)
    check(len(closed_invoice["refunds"]) == 1 and closed_invoice["status"] == "expired"  # ty: ignore[invalid-argument-type]
          and closed_invoice["amount_received"] == "0", f"after the CLI: {closed_invoice}")  # fmt: skip

    # Someone else's address: the float refunds nothing; the seller settles with the payer.
    other_payer = PAYER_ACCOUNTS[7]
    other_note = "Browser e2e: USDC only, to someone else"
    other_invoice = create_invoice(context, amount="25", currency="USD", assets=["USDC"],
                                   destination_address=create_local_account().address, note=other_note)  # fmt: skip
    other_invoice_id = str(other_invoice["id"])
    wait_for_receipt(
        context.http_client,
        send_transaction(context.http_client, sender=other_payer.address, to=str(other_invoice["deposit_address"]),
                         value_wei=stray_wei),
    )  # fmt: skip
    other_invoice = wait_for_final_stray_payments(
        context, other_invoice_id, description="final stray payment on someone else's invoice"
    )
    other_handling = dict(other_invoice["stray_payments"][0])["refund_handling"]  # ty: ignore[not-subscriptable, no-matching-overload]
    check(other_handling == "receiver_settles", f"stray handling {other_handling}")
    time.sleep(5)  # a few watcher ticks: no refund may appear
    refused_reply = context.http_client.post(
        f"{API_URL}/invoices/{other_invoice_id}/refunds",
        headers=context.account_headers,
        json={"sender": other_payer.address, "amount": stray_amount_text},
    )
    check(refused_reply.status_code == 409 and refused_reply.json()["code"] == "refund_not_allowed",
          f"a refund on someone else's invoice answered {refused_reply.status_code} {refused_reply.text}")  # fmt: skip
    check(not read_invoice(context, other_invoice_id)["refunds"], "a refund was recorded on someone else's invoice")
    other_payer_page = run.watch_page(
        run.open_payer_context(InjectedBrowserWallet(http_client=context.http_client, account=other_payer)).new_page()
    )
    other_payer_page.goto(f"{PUBLIC_BASE_URL}/i/{other_invoice_id}")
    expect(other_payer_page.get_by_test_id("invoice_stray_payment_line")).to_have_text(
        f"We got {stray_amount_text} {NATIVE_SYMBOL}. This link takes USDC only, so it wasn't counted. It goes to the seller, who "
        "can send it back.",
        timeout=30_000,
    )
    run.take_screenshot(other_payer_page, "payer_wrong_coin_receiver_settles")
    other_payer_page.context.close()
    settle_page = open_phone_page(run, signed_in=True)
    settle_page.goto(f"{PUBLIC_BASE_URL}/activity")
    settle_page.locator("#invoice_list button", has_text=other_note).first.click()
    other_overlay = settle_page.get_by_test_id("invoice_actions_overlay")
    expect(other_overlay.get_by_test_id("stray_refund_handling_line")).to_have_text(
        "It went to the receiver with the rest. Settle it with the payer.", timeout=30_000
    )
    settle_page.wait_for_timeout(600)
    run.take_screenshot(settle_page, "account_wrong_coin_receiver_settles")
    settle_page.context.close()
    run.check_no_page_errors()
    return {"invoice_id": invoice_id, "stray_transaction": stray_transaction_hash,
            "seconds_until_alert": seconds_until_shown, "stray_refund": stray_refund, "pol_sweeps": pol_sweeps,
            "account_events": account_event_types, "inc_001_replay_invoice_id": closed_invoice_id,
            "inc_001_replay_refunds": closed_invoice["refunds"], "other_invoice_id": other_invoice_id,
            "other_refund_reply": refused_reply.json(), "screenshots": run.screenshot_paths}  # fmt: skip


def pause_watcher(context: FlowContext) -> None:
    """Freeze the watcher (`docker compose pause`) so the invoice stays as it is while the chain moves on."""
    run_command(compose_arguments(context.environment_file, "pause", "watcher"),
                environment=compose_environment(context.environment_file),
                log_path=context.scratch_folder / "commands.log")  # fmt: skip


def unpause_watcher(context: FlowContext) -> None:
    run_command(compose_arguments(context.environment_file, "unpause", "watcher"),
                environment=compose_environment(context.environment_file),
                log_path=context.scratch_folder / "commands.log")  # fmt: skip


def check_transaction_hash_in_sheet(payer_page: Page, *, transaction_hash_text: str, expected: re.Pattern[str]) -> str:
    """Type a hash into the open "I've paid" sheet, check it, and match the one sentence it answers."""
    check_open_sheets_fit_the_screen(payer_page, what='the "I\'ve paid" sheet')
    payer_page.locator("#transaction_hash_input").fill(transaction_hash_text)
    payer_page.click("#check_transaction_hash_button")
    check_message = payer_page.get_by_test_id("payment_check_message")
    expect(check_message).to_have_text(expected, timeout=60_000)
    return check_message.inner_text()


def take_layout_shots(run: BrowserScenarioRun, payer_page: Page, *, moment: str) -> None:
    """The screen at 360 and 430 px besides the default 390: no sideways scroll."""
    for width, height in ((360, 740), (430, 932)):
        payer_page.set_viewport_size({"width": width, "height": height})
        payer_page.wait_for_timeout(300)
        check(not page_scrolls_sideways(payer_page), f"{moment} scrolls sideways at {width} px")
        run.take_screenshot(payer_page, f"{moment}_{width}px")
    payer_page.set_viewport_size(PAYER_PHONE_VIEWPORT)  # ty: ignore[invalid-argument-type]


def scenario_i_paid_checks_the_chain(context: FlowContext, *, browser: Browser) -> dict[str, object]:
    """7. "I've paid": the deposit reread after a right-coin send, then every transaction-hash case on a USDC-only
    invoice, the wrong-coin hash recorded and sent back by itself. The watcher is paused while the page is read, so
    nothing but the check can explain what the page says."""
    run = BrowserScenarioRun(context=context, browser=browser, scenario_name="i_paid")
    payer_account = PAYER_ACCOUNTS[9]
    chain_name = FORK_CHAIN_NAME
    messages: dict[str, str] = {}

    # A right-coin send, then "I've paid": the reread finds it before the watcher does.
    pol_invoice = create_invoice(context, amount="2", currency="USD", assets=[NATIVE_SYMBOL],
                                 destination_address=create_local_account().address, note=f"Browser e2e: I paid, {NATIVE_SYMBOL}")  # fmt: skip
    pol_invoice_id = str(pol_invoice["id"])
    wallet = InjectedBrowserWallet(http_client=context.http_client, account=payer_account)
    payer_page = run.watch_page(run.open_payer_context(wallet).new_page())
    payer_page.goto(f"{PUBLIC_BASE_URL}/i/{pol_invoice_id}")
    wait_for_page_status(payer_page, "Awaiting payment", timeout_milliseconds=30_000)
    expect_pay_with_coin_button_text(payer_page, asset_symbol=NATIVE_SYMBOL, chain_name=chain_name)
    run.take_screenshot(payer_page, "payer_confirm_pol")
    confirm_coin_and_network(payer_page, asset_symbol=NATIVE_SYMBOL)
    run.take_screenshot(payer_page, "payer_send_screen_with_i_paid")
    pause_watcher(context)
    try:
        payment_hash = send_transaction(
            context.http_client, sender=payer_account.address, to=str(pol_invoice["deposit_address"]),
            value_wei=to_base_units(str(pol_invoice["amount_due"]), decimals=18),
        )  # fmt: skip
        wait_for_receipt(context.http_client, payment_hash)
        payer_page.click("#i_paid_button")
        check_message = payer_page.get_by_test_id("payment_check_message")
        expect(check_message).to_have_text(
            re.compile(
                rf"^Found it: [\d,.]+ {NATIVE_SYMBOL} (is on its way\. It counts as soon as .+|arrived and is counted\.)$"
            ),
            timeout=30_000,
        )
        messages["right_coin_reread"] = check_message.inner_text()
        run.take_screenshot(payer_page, "payer_i_paid_found")
        # It closes by itself once said.
        expect(payer_page.get_by_test_id("payment_check_sheet")).to_have_count(0, timeout=10_000)
    finally:
        unpause_watcher(context)
    wait_for_page_status(payer_page, "Paid")
    payer_page.context.close()

    # A USDC-only invoice to the operator's address: wrong-coin money there goes back by itself.
    usdc_invoice = create_invoice(context, amount="25", currency="USD", assets=["USDC"],
                                  destination_address=context.operator_destination_address,
                                  note="Browser e2e: I paid, USDC only")  # fmt: skip
    usdc_invoice_id = str(usdc_invoice["id"])
    deposit_address = str(usdc_invoice["deposit_address"])
    wallet = InjectedBrowserWallet(http_client=context.http_client, account=payer_account)
    payer_page = run.watch_page(run.open_payer_context(wallet).new_page())
    payer_page.goto(f"{PUBLIC_BASE_URL}/i/{usdc_invoice_id}")
    wait_for_page_status(payer_page, "Awaiting payment", timeout_milliseconds=30_000)
    take_layout_shots(run, payer_page, moment="payer_confirm_usdc")
    confirm_coin_and_network(payer_page, asset_symbol="USDC")
    take_layout_shots(run, payer_page, moment="payer_send_screen")
    stray_wei = 30 * NATIVE_UNIT_WEI
    pause_watcher(context)
    try:
        wrong_coin_hash = send_transaction(context.http_client, sender=payer_account.address, to=deposit_address,
                                           value_wei=stray_wei)  # fmt: skip
        wait_for_receipt(context.http_client, wrong_coin_hash)
        other_address_hash = send_transaction(context.http_client, sender=payer_account.address,
                                              to=create_local_account().address, value_wei=NATIVE_UNIT_WEI)  # fmt: skip
        wait_for_receipt(context.http_client, other_address_hash)
        # Past `finalized` (2 blocks behind on this fork): final, and unseen by the paused watcher.
        call_anvil(context.http_client, "anvil_mine", hex(4))
        payer_page.click("#i_paid_button")
        expect(payer_page.get_by_test_id("payment_check_message")).to_have_text(
            f"Nothing for this invoice on {chain_name} yet.", timeout=30_000
        )
        expect(payer_page.locator("#transaction_hash_input")).to_be_visible()
        run.take_screenshot(payer_page, "payer_i_paid_asks_for_hash")
        take_layout_shots(run, payer_page, moment="payer_i_paid_sheet")
        messages["tron_style_hash"] = check_transaction_hash_in_sheet(
            payer_page, transaction_hash_text=wrong_coin_hash.removeprefix("0x"),
            expected=re.compile(rf"^That doesn't look like a {re.escape(chain_name)} transaction\."),
        )  # fmt: skip
        run.take_screenshot(payer_page, "payer_i_paid_not_a_polygon_hash")
        messages["random_hash"] = check_transaction_hash_in_sheet(
            payer_page, transaction_hash_text="0x" + "5c" * 32,
            expected=re.compile(rf"^There's no transaction with that hash on {re.escape(chain_name)}\. If you sent on "
                                r"another network, the money doesn't arrive here: contact the seller\.$"),
        )  # fmt: skip
        run.take_screenshot(payer_page, "payer_i_paid_not_on_chain")
        messages["other_address_hash"] = check_transaction_hash_in_sheet(
            payer_page, transaction_hash_text=other_address_hash,
            expected=re.compile(r"^That transaction didn't send money to this invoice's address\."),
        )  # fmt: skip
        run.take_screenshot(payer_page, "payer_i_paid_other_address")
        messages["wrong_coin_hash"] = check_transaction_hash_in_sheet(
            payer_page, transaction_hash_text=wrong_coin_hash,
            expected=re.compile(rf"^We got 30\.00 {NATIVE_SYMBOL}\. This link takes USDC only, so it's being sent back to the "
                                r"address it came from\.$"),
        )  # fmt: skip
        run.take_screenshot(payer_page, "payer_i_paid_wrong_coin_recorded")
        recorded = read_public_invoice(context, usdc_invoice_id)
        stray_payments = list(recorded["stray_payments"])  # ty: ignore[invalid-argument-type]
        check(len(stray_payments) == 1, f"stray payments after the hash: {stray_payments}")
        stray_payment = dict(stray_payments[0])  # ty: ignore[no-matching-overload]
        check(stray_payment["tx_hash"] == wrong_coin_hash and stray_payment["sender"] == payer_account.address
              and stray_payment["asset_symbol"] == NATIVE_SYMBOL and stray_payment["final"] is True
              and stray_payment["refund_handling"] == "automatic", f"stray payment {stray_payment}")  # fmt: skip
        check(recorded["status"] == "awaiting_payment" and recorded["amount_received"] == "0",
              f"the hash check credited the invoice: {recorded['status']} {recorded['amount_received']}")  # fmt: skip
        # The same hash again records nothing more.
        repeat_reply = context.http_client.post(f"{API_URL}/public/invoices/{usdc_invoice_id}/payment-checks",
                                                json={"transaction_hash": wrong_coin_hash})  # fmt: skip
        check(repeat_reply.status_code == 200 and repeat_reply.json()["outcome"] == "wrong_coin_recorded"
              and len(repeat_reply.json()["invoice"]["stray_payments"]) == 1,
              f"the repeat check answered {repeat_reply.status_code} {repeat_reply.text[:300]}")  # fmt: skip
        payer_page.click("#close_payment_check_button")
        expect(payer_page.get_by_test_id("payment_check_sheet")).to_have_count(0)
        payer_balance_before_refund = read_native_balance(context.http_client, payer_account.address)
    finally:
        unpause_watcher(context)

    # Once the watcher runs again, the recorded POL goes back by itself, once.
    refunded_invoice = wait_for_invoice(
        context, usdc_invoice_id, description=f"final automatic refund of the {NATIVE_SYMBOL} the hash check recorded",
        condition=lambda current: find_final_stray_refund(current, asset_symbol=NATIVE_SYMBOL) is not None,
    )  # fmt: skip
    pol_refunds = [refund for refund in refunded_invoice["refunds"] if refund["asset_symbol"] == NATIVE_SYMBOL]  # ty: ignore[not-iterable]
    check(len(pol_refunds) == 1 and pol_refunds[0]["amount"] == "30"
          and pol_refunds[0]["recipient"] == payer_account.address, f"{NATIVE_SYMBOL} refunds {pol_refunds}")  # fmt: skip
    refunded_wei = read_native_balance(context.http_client, payer_account.address) - payer_balance_before_refund
    check(refunded_wei == stray_wei, f"the payer got {refunded_wei} wei back, sent {stray_wei}")
    expect(payer_page.get_by_test_id("invoice_stray_payment_line")).to_have_text(
        f"We got 30.00 {NATIVE_SYMBOL}. This link takes USDC only, so it was sent back to the address it came from.",
        timeout=60_000,
    )
    expect(payer_page.get_by_test_id("invoice_refund_row")).to_contain_text("30.00")
    run.take_screenshot(payer_page, "payer_i_paid_wrong_coin_sent_back")
    payer_page.context.close()

    # Desktop (the sheet is a Dialog there), then the confirm screen in dark.
    desktop_context = browser.new_context(
        viewport=ACCOUNT_DESKTOP_VIEWPORT,  # ty: ignore[invalid-argument-type]
        locale=BROWSER_LOCALE,
        reduced_motion="reduce",
    )
    desktop_page = run.watch_page(desktop_context.new_page())
    desktop_page.goto(f"{PUBLIC_BASE_URL}/i/{usdc_invoice_id}")
    wait_for_page_status(desktop_page, "Awaiting payment", timeout_milliseconds=30_000)
    run.take_screenshot(desktop_page, "desktop_confirm_usdc")
    confirm_coin_and_network(desktop_page, asset_symbol="USDC")
    desktop_page.click("#i_paid_button")
    expect(desktop_page.get_by_role("dialog")).to_be_visible()
    expect(desktop_page.get_by_test_id("payment_check_message")).to_contain_text(
        f"We got 30.00 {NATIVE_SYMBOL}", timeout=30_000
    )
    run.take_screenshot(desktop_page, "desktop_i_paid_dialog")
    desktop_context.close()
    dark_page = open_phone_page(run, color_scheme="dark")
    dark_page.goto(f"{PUBLIC_BASE_URL}/i/{usdc_invoice_id}")
    wait_for_page_status(dark_page, "Awaiting payment", timeout_milliseconds=30_000)
    run.take_screenshot(dark_page, "payer_confirm_usdc_dark")
    dark_page.context.close()
    run.check_no_page_errors()
    return {"pol_invoice_id": pol_invoice_id, "usdc_invoice_id": usdc_invoice_id, "payment_tx": payment_hash,
            "wrong_coin_tx": wrong_coin_hash, "other_address_tx": other_address_hash, "messages": messages,
            "pol_refunds": pol_refunds, "screenshots": run.screenshot_paths}  # fmt: skip


def hide_and_show_page(payer_page: Page) -> None:
    """What a trip to the wallet app does to the page: `visibilitychange` to hidden, then back to visible. Headless
    Chromium never hides a page by itself, so `document.visibilityState` is overridden around each event."""
    payer_page.evaluate("""() => {
      for (const visibilityState of ["hidden", "visible"]) {
        Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibilityState })
        document.dispatchEvent(new Event("visibilitychange"))
      }
      delete document.visibilityState
    }""")


def wait_for_receipt_status(http_client: httpx.Client, transaction_hash: str) -> str:
    """The mined receipt's status ("0x1" or "0x0"); `wait_for_receipt` refuses a revert, which is the point here."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        receipt = call_anvil(http_client, "eth_getTransactionReceipt", transaction_hash)
        if isinstance(receipt, dict):
            return str(receipt["status"])
        time.sleep(0.5)
    raise FlowFailedError(f"transaction {transaction_hash} was not mined in 30 s")


def create_invoice_within_write_budget(context: FlowContext, **fields: object) -> dict[str, object]:
    """`create_invoice`, waiting out the API's 10 writes a minute per IP (GCRA: one more every 6 s), which the scenario
    before and this one's own page checks spend."""
    for _ in range(15):
        try:
            return create_invoice(context, **fields)
        except FlowFailedError as error:
            if "rate_limited" not in str(error):
                raise
            time.sleep(6)
    return create_invoice(context, **fields)


def pay_with_wallet_and_wait_for_lock(payer_page: Page, wallet: InjectedBrowserWallet) -> str:
    """Tap "Pay with wallet" and wait for the hash and the lock; the one transaction's hash."""
    payer_page.click("#pay_with_wallet_button")
    expect(payer_page.get_by_test_id("invoice_wallet_message")).to_contain_text(
        "Sent. This page updates by itself", timeout=30_000
    )
    expect(payer_page.locator("#pay_with_wallet_button")).to_have_text("Waiting for confirmation…")
    check(len(wallet.sent_transaction_hashes) == 1, f"wallet sent {wallet.sent_transaction_hashes}")
    return wallet.sent_transaction_hashes[0]


def scenario_send_lock_survives_a_reload(context: FlowContext, *, browser: Browser) -> dict[str, object]:
    """8. The wallet send's lock is kept per invoice in the tab's sessionStorage: a reload before the invoice moves
    locks the page again and asks the chain about that hash. The watcher is paused for the on-its-way case, so only
    the stored hash can explain the lock."""
    run = BrowserScenarioRun(context=context, browser=browser, scenario_name="send_lock")
    chain_name = FORK_CHAIN_NAME
    messages: dict[str, str] = {}

    # The manual path has no hash to lock on: after a copy and a trip away, "I've paid" is pointed at.
    payer_account = PAYER_ACCOUNTS[10]
    pol_invoice = create_invoice_within_write_budget(
        context, amount="2", currency="USD", assets=[NATIVE_SYMBOL], destination_address=create_local_account().address,
        note="Browser e2e: send lock",
    )  # fmt: skip
    pol_invoice_id = str(pol_invoice["id"])
    wallet = InjectedBrowserWallet(http_client=context.http_client, account=payer_account)
    payer_page = run.watch_page(run.open_payer_context(wallet).new_page())
    payer_page.goto(f"{PUBLIC_BASE_URL}/i/{pol_invoice_id}")
    wait_for_page_status(payer_page, "Awaiting payment", timeout_milliseconds=30_000)
    confirm_coin_and_network(payer_page, asset_symbol=NATIVE_SYMBOL)
    nudge_line = payer_page.get_by_test_id("payment_check_nudge")
    hide_and_show_page(payer_page)
    check(nudge_line.count() == 0, "the nudge showed without a copy or a wallet hand-off")
    payer_page.get_by_test_id("invoice_address_copy").click()
    hide_and_show_page(payer_page)
    expect(nudge_line).to_have_text("Sent it? Tap “I've paid” and we'll look for it.")
    check(payer_page.locator("#i_paid_button").count() == 1, "the nudge added a second I've paid")
    run.take_screenshot(payer_page, "payer_i_paid_nudge")
    take_layout_shots(run, payer_page, moment="payer_i_paid_nudge")

    pay_button = payer_page.locator("#pay_with_wallet_button")
    wallet_message = payer_page.get_by_test_id("invoice_wallet_message")
    pause_watcher(context)
    try:
        payment_hash = pay_with_wallet_and_wait_for_lock(payer_page, wallet)
        wait_for_receipt(context.http_client, payment_hash)
        payer_page.reload()
        expect(payer_page.get_by_test_id("invoice_deposit_address")).to_be_visible(timeout=30_000)
        expect(pay_button).to_have_text("Waiting for confirmation…")
        expect(pay_button).to_be_disabled()
        expect(wallet_message).to_have_text(
            re.compile(rf"^Your payment is on its way\. This page updates by itself once {re.escape(chain_name)} "
                       r"confirms it\."), timeout=STORED_SEND_CHECK_TIMEOUT_MILLISECONDS,
        )  # fmt: skip
        messages["on_its_way"] = wallet_message.inner_text()
        expect(payer_page.get_by_test_id("invoice_qr_lock_overlay")).to_be_visible()
        check(nudge_line.count() == 0, "the nudge shows while the wallet send is locked")
        unseen = read_public_invoice(context, pol_invoice_id)
        check(unseen["status"] == "awaiting_payment" and not unseen["payments"],
              f"the paused watcher counted it already: {unseen['status']} {unseen['payments']}")  # fmt: skip
        run.take_screenshot(payer_page, "payer_reloaded_still_locked")
        take_layout_shots(run, payer_page, moment="payer_reloaded_still_locked")
        # A second reload still holds: the page never offers a live "Pay with wallet" while the money is on its way.
        payer_page.reload()
        expect(wallet_message).to_contain_text(
            "Your payment is on its way", timeout=STORED_SEND_CHECK_TIMEOUT_MILLISECONDS
        )
        expect(pay_button).to_be_disabled()
    finally:
        unpause_watcher(context)
    wait_for_page_status(payer_page, "Paid")
    paid_invoice = read_public_invoice(context, pol_invoice_id)
    check(len(wallet.sent_transaction_hashes) == 1, f"the wallet sent {wallet.sent_transaction_hashes}")
    check(paid_invoice["status"] == "paid" and len(list(paid_invoice["payments"])) == 1,  # ty: ignore[invalid-argument-type]
          f"paid once? {paid_invoice['status']}, payments {paid_invoice['payments']}")  # fmt: skip
    payer_page.context.close()

    # A wallet send that reverted on chain: a reload clears the lock and says no money moved.
    failing_payer = PAYER_ACCOUNTS[11]  # POL for gas, no USDC
    usdc_invoice = create_invoice_within_write_budget(
        context, amount="1", currency="USD", assets=["USDC"], destination_address=create_local_account().address,
        note="Browser e2e: send lock, reverted",
    )  # fmt: skip
    usdc_invoice_id = str(usdc_invoice["id"])
    wallet = InjectedBrowserWallet(http_client=context.http_client, account=failing_payer, fixed_gas=120_000)
    payer_page = run.watch_page(run.open_payer_context(wallet).new_page())
    payer_page.goto(f"{PUBLIC_BASE_URL}/i/{usdc_invoice_id}")
    wait_for_page_status(payer_page, "Awaiting payment", timeout_milliseconds=30_000)
    confirm_coin_and_network(payer_page, asset_symbol="USDC")
    reverted_hash = pay_with_wallet_and_wait_for_lock(payer_page, wallet)
    reverted_status = wait_for_receipt_status(context.http_client, reverted_hash)
    check(reverted_status == "0x0", f"the uncovered USDC transfer mined with status {reverted_status}")
    payer_page.reload()
    wallet_message = payer_page.get_by_test_id("invoice_wallet_message")
    expect(wallet_message).to_have_text(
        f"Your last payment failed on {chain_name}, so no money moved. You can send it again.",
        timeout=STORED_SEND_CHECK_TIMEOUT_MILLISECONDS,
    )
    messages["failed"] = wallet_message.inner_text()
    pay_button = payer_page.locator("#pay_with_wallet_button")
    expect(pay_button).to_have_text("Pay with wallet")
    expect(pay_button).to_be_enabled()
    check(payer_page.get_by_test_id("invoice_qr_lock_overlay").count() == 0, "the QR stays dimmed after a failed send")
    run.take_screenshot(payer_page, "payer_reloaded_failed_send")
    payer_page.context.close()

    # A wallet send that went to another address: a reload clears the lock with one plain line.
    other_invoice = create_invoice_within_write_budget(
        context, amount="1", currency="USD", assets=[NATIVE_SYMBOL], destination_address=create_local_account().address,
        note="Browser e2e: send lock, elsewhere",
    )  # fmt: skip
    other_invoice_id = str(other_invoice["id"])
    wallet = InjectedBrowserWallet(http_client=context.http_client, account=failing_payer,
                                   redirect_native_sends_to=create_local_account().address)  # fmt: skip
    payer_page = run.watch_page(run.open_payer_context(wallet).new_page())
    payer_page.goto(f"{PUBLIC_BASE_URL}/i/{other_invoice_id}")
    wait_for_page_status(payer_page, "Awaiting payment", timeout_milliseconds=30_000)
    confirm_coin_and_network(payer_page, asset_symbol=NATIVE_SYMBOL)
    elsewhere_hash = pay_with_wallet_and_wait_for_lock(payer_page, wallet)
    wait_for_receipt(context.http_client, elsewhere_hash)
    payer_page.reload()
    wallet_message = payer_page.get_by_test_id("invoice_wallet_message")
    expect(wallet_message).to_have_text(
        "Your last send didn't go to this invoice's address, so it wasn't counted.",
        timeout=STORED_SEND_CHECK_TIMEOUT_MILLISECONDS,
    )
    messages["other_address"] = wallet_message.inner_text()
    pay_button = payer_page.locator("#pay_with_wallet_button")
    expect(pay_button).to_have_text("Pay with wallet")
    expect(pay_button).to_be_enabled()
    run.take_screenshot(payer_page, "payer_reloaded_other_address")
    check(not page_scrolls_sideways(payer_page), "the other-address line scrolls sideways at 390 px")
    payer_page.context.close()
    # The scenarios before spend the 10-a-minute write budget, so a reload's check can get a 429; the page asks again
    # after Retry-After (the lines above prove it got its answer), but Chromium still logs the refused fetch.
    run.page_errors = [error for error in run.page_errors if "status of 429 (Too Many Requests)" not in error]
    run.check_no_page_errors()
    return {"pol_invoice_id": pol_invoice_id, "payment_tx": payment_hash, "usdc_invoice_id": usdc_invoice_id,
            "reverted_tx": reverted_hash, "other_invoice_id": other_invoice_id, "elsewhere_tx": elsewhere_hash,
            "messages": messages, "screenshots": run.screenshot_paths}  # fmt: skip


# ---- Settings: per-network defaults and sheet widths (scenario 10) ----

# A 375 px iPhone (SE 3rd gen, 12 mini): the narrowest phone the pages are built for. In Chromium: Playwright's WebKit
# does not send a Secure cookie to http://localhost, so it cannot be signed in on the stack.
NARROW_PHONE_VIEWPORT = {"width": 375, "height": 667}
SETTINGS_GROUP_HEADINGS = ["Getting paid", "Payer page", "Security", "Connect", "Records"]
SETTINGS_SHEET_ROW_IDS = [
    "settings_payout_address_row", "settings_accepted_coins_row", "settings_support_contact_row",
    "settings_passkeys_row", "settings_discord_row", "settings_webhook_row", "settings_api_tokens_row",
    "settings_invoice_export_row",
]  # fmt: skip
CHAIN_LIFETIME_MINUTES = 45
CHAIN_ACCURACY_PERCENT = "2.5"


def open_narrow_phone_page(run: BrowserScenarioRun) -> Page:
    """A signed-in 375 px phone page: mobile viewport, touch, no motion."""
    phone_context = run.browser.new_context(
        viewport=NARROW_PHONE_VIEWPORT,  # ty: ignore[invalid-argument-type]
        locale=BROWSER_LOCALE,
        device_scale_factor=2,
        is_mobile=True,
        has_touch=True,
        reduced_motion="reduce",
    )
    phone_context.add_cookies([{
        "name": ACCOUNT_SESSION_COOKIE_NAME, "value": run.context.account_session_token,
        "domain": PUBLIC_BASE_HOST, "path": "/", "secure": True, "httpOnly": True, "sameSite": "Strict",
    }])  # fmt: skip
    return run.watch_page(phone_context.new_page())


def read_account_session(context: FlowContext) -> dict[str, object]:
    reply = context.http_client.get(f"{API_URL}/sessions/current", headers=context.account_headers)
    reply.raise_for_status()
    return reply.json()


def save_account_settings_through_api(context: FlowContext, **fields: object) -> dict[str, object]:
    reply = context.http_client.patch(f"{API_URL}/accounts/current", json=fields, headers=context.account_headers)
    check(reply.status_code == 200, f"PATCH /accounts/current {fields} answered {reply.status_code} {reply.text}")
    return reply.json()


def read_invoice_window_seconds(invoice: dict[str, object]) -> float:
    created_at = datetime.fromisoformat(str(invoice["created_at"]))
    return (datetime.fromisoformat(str(invoice["expires_at"])) - created_at).total_seconds()


def check_invoice_takes_chain_defaults(invoice: dict[str, object], *, what: str) -> None:
    window_seconds = read_invoice_window_seconds(invoice)
    check(invoice["accuracy_percent"] == CHAIN_ACCURACY_PERCENT
          and abs(window_seconds - CHAIN_LIFETIME_MINUTES * 60) < 5,
          f"{what}: {invoice['accuracy_percent']}% and {window_seconds} s, wanted the network's "
          f"{CHAIN_ACCURACY_PERCENT}% and {CHAIN_LIFETIME_MINUTES} min")  # fmt: skip


def check_settings_sheets_fit(run: BrowserScenarioRun, page: Page) -> None:
    """/settings' groups in order, and every row's sheet within the 375 px screen."""
    page.goto(f"{PUBLIC_BASE_URL}/settings")
    expect(page.locator("#settings_accepted_coins_row")).to_be_visible(timeout=30_000)
    headings = [heading.strip() for heading in page.locator("h2").all_inner_texts()]
    check(headings == SETTINGS_GROUP_HEADINGS, f"/settings groups read {headings}")
    check(not page_scrolls_sideways(page), "/settings scrolls sideways at 375 px")
    run.take_screenshot(page, "settings")
    for row_id in SETTINGS_SHEET_ROW_IDS:
        page.click(f"#{row_id}")
        expect(page.get_by_role("dialog").first).to_be_visible()
        if row_id == "settings_invoice_export_row":
            page.fill("#settings_invoice_export_from_input", "2026-10-01")
            page.fill("#settings_invoice_export_to_input", "2026-10-22")
        check_open_sheets_fit_the_screen(page, what=f"{row_id}'s sheet")
        run.take_screenshot(page, f"{row_id}_sheet")
        page.keyboard.press("Escape")
        expect(page.get_by_role("dialog")).to_have_count(0)


def save_chain_defaults_in_coins_sheet(run: BrowserScenarioRun, page: Page) -> None:
    """The Coins sheet's section for the fork's network: a wrong window refused under its field, then this network's
    time to pay and shortfall saved, and shown again after a reload."""
    page.click("#settings_accepted_coins_row")
    expect(page.locator(f"#settings_chain_section_{FORK_CHAIN_KEY}")).to_be_visible()
    expect(page.locator(f"#settings_finality_note_{FORK_CHAIN_KEY}")).to_contain_text("finalized")
    check(page.locator(f"#settings_confirmations_input_{FORK_CHAIN_KEY}").count() == 0,
          "an EVM network offers a confirmation count")  # fmt: skip
    lifetime_input = page.locator(f"#settings_lifetime_minutes_input_{FORK_CHAIN_KEY}")
    lifetime_input.fill("3")
    expect(page.locator(f"#settings_lifetime_minutes_error_{FORK_CHAIN_KEY}")).to_be_visible()
    expect(page.locator("#settings_save_accepted_coins_button")).to_be_disabled()
    lifetime_input.fill(str(CHAIN_LIFETIME_MINUTES))
    page.locator(f"#settings_accuracy_percent_input_{FORK_CHAIN_KEY}").fill(CHAIN_ACCURACY_PERCENT)
    check_open_sheets_fit_the_screen(page, what="the filled Coins sheet")
    run.take_screenshot(page, "coins_sheet_filled")
    page.click("#settings_save_accepted_coins_button")
    expect(page.get_by_role("dialog")).to_have_count(0)
    session = read_account_session(run.context)
    check(session["invoice_lifetime_seconds_by_chain"] == {FORK_CHAIN_KEY: CHAIN_LIFETIME_MINUTES * 60}
          and session["accuracy_percent_by_chain"] == {FORK_CHAIN_KEY: CHAIN_ACCURACY_PERCENT},
          f"the session after Save: {session}")  # fmt: skip
    page.reload()
    page.click("#settings_accepted_coins_row")
    expect(page.locator(f"#settings_lifetime_minutes_input_{FORK_CHAIN_KEY}")).to_have_value(
        str(CHAIN_LIFETIME_MINUTES)
    )
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0)


def create_invoice_on_narrow_front_page(page: Page) -> str:
    """/ at 375 px with Options left empty ("Chain default"), its sheets within the screen; returns the invoice id."""
    page.goto(f"{PUBLIC_BASE_URL}/")
    expect(page.locator("#create_button")).to_be_enabled(timeout=30_000)
    page.click("#invoice_options_button")
    expect(page.locator("#lifetime_minutes_input")).to_have_attribute("placeholder", "Chain default")
    check_open_sheets_fit_the_screen(page, what="the options sheet on /")
    page.click("#invoice_options_done_button")
    expect(page.get_by_role("dialog")).to_have_count(0)
    page.click("#accepted_coins_button")
    expect(page.locator('[data-testid^="accepted_coin_toggle_"]').first).to_be_visible()
    check_open_sheets_fit_the_screen(page, what="the coins sheet on /")
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0)
    page.fill("#amount_input", "2")
    page.click("#create_button")
    page.wait_for_url(re.compile(r"/i/inv_"), timeout=30_000)
    expect(page.get_by_test_id("creator_share_overlay")).to_be_visible()
    check_open_sheets_fit_the_screen(page, what="the share Drawer")
    return read_invoice_id_from_url(page.url)


def check_link_payer_invoice_takes_chain_defaults(context: FlowContext) -> str:
    """A quantity link made without terms reads null and gives its payer's invoice the network's; returns its id."""
    link_reply = context.http_client.post(
        f"{API_URL}/payment-links", headers=context.account_headers,
        json={"amount": "2", "currency": "USD", "chain": FORK_CHAIN_KEY, "assets": ["USDC"], "quantity": 1,
              "note": "Browser e2e: chain default link"},
    )  # fmt: skip
    check(link_reply.status_code == 201, f"link: {link_reply.status_code} {link_reply.text}")
    payment_link = link_reply.json()
    check(payment_link["lifetime_seconds"] is None and payment_link["accuracy_percent"] is None,
          f"a link made without terms reads {payment_link['lifetime_seconds']}, "
          f"{payment_link['accuracy_percent']}")  # fmt: skip
    slot_reply = context.http_client.post(
        f"{API_URL}/public/payment-links/{payment_link['id']}/invoices", json={"asset": "USDC", "chain": FORK_CHAIN_KEY}
    )
    check(slot_reply.status_code == 201, f"slot: {slot_reply.status_code} {slot_reply.text}")
    link_invoice = read_invoice(context, str(slot_reply.json()["id"]))
    check_invoice_takes_chain_defaults(link_invoice, what="the link payer's invoice")
    close_reply = context.http_client.post(
        f"{API_URL}/payment-links/{payment_link['id']}/close", headers=context.account_headers
    )
    close_reply.raise_for_status()
    return str(link_invoice["id"])


def check_chain_default_refusals(context: FlowContext) -> None:
    """An unknown network and a shortfall out of bounds are refused."""
    unknown_reply = context.http_client.patch(
        f"{API_URL}/accounts/current", json={"invoice_lifetime_seconds_by_chain": {"no_such_chain": 600}},
        headers=context.account_headers,
    )  # fmt: skip
    check(unknown_reply.status_code == 400 and unknown_reply.json()["code"] == "chain_not_enabled",
          f"an unknown network answered {unknown_reply.status_code} {unknown_reply.text}")  # fmt: skip
    bounds_reply = context.http_client.patch(
        f"{API_URL}/accounts/current", json={"accuracy_percent_by_chain": {FORK_CHAIN_KEY: "5.01"}},
        headers=context.account_headers,
    )  # fmt: skip
    check(bounds_reply.status_code in {400, 422}, f"5.01% answered {bounds_reply.status_code} {bounds_reply.text}")


def scenario_settings_per_chain_defaults_and_sheet_widths(
    context: FlowContext,
    *,
    browser: Browser,
) -> dict[str, object]:
    """/settings at 375 px: the groups in order, every row's sheet within the screen; the Coins sheet's per-network
    time to pay and shortfall saved and taken by an invoice made on / with Options left empty, an account invoice and
    a quantity link's payer invoice made without them over the API, but not by an invoice that names its own; an
    unknown network and an out-of-bounds shortfall refused; everything cleared again at the end."""
    run = BrowserScenarioRun(context=context, browser=browser, scenario_name="settings_widths")
    page = open_narrow_phone_page(run)
    try:
        check_settings_sheets_fit(run, page)
        save_chain_defaults_in_coins_sheet(run, page)
        form_invoice = read_invoice(context, create_invoice_on_narrow_front_page(page))
        check_invoice_takes_chain_defaults(form_invoice, what="/'s invoice with Options left empty")
        defaulted_invoice = create_invoice_within_write_budget(context, amount="2", currency="USD", assets=["USDC"])
        check_invoice_takes_chain_defaults(defaulted_invoice, what="an API invoice without terms")
        explicit_invoice = create_invoice_within_write_budget(
            context, amount="2", currency="USD", assets=["USDC"], lifetime_seconds=600, accuracy_percent="0.5"
        )
        check(explicit_invoice["accuracy_percent"] == "0.5"
              and abs(read_invoice_window_seconds(explicit_invoice) - 600) < 5,
              f"an API invoice with its own terms: {explicit_invoice}")  # fmt: skip
        link_invoice_id = check_link_payer_invoice_takes_chain_defaults(context)
        check_chain_default_refusals(context)
        run.check_no_page_errors()
        return {"form_invoice_id": form_invoice["id"], "defaulted_invoice_id": defaulted_invoice["id"],
                "explicit_invoice_id": explicit_invoice["id"], "link_invoice_id": link_invoice_id,
                "screenshots": run.screenshot_paths}  # fmt: skip
    finally:
        # Every later scenario expects the server's 30 minutes and 1%.
        cleared = save_account_settings_through_api(
            context,
            invoice_lifetime_seconds_by_chain={FORK_CHAIN_KEY: None},
            accuracy_percent_by_chain={FORK_CHAIN_KEY: None},
        )
        check(cleared["invoice_lifetime_seconds_by_chain"] == {} and cleared["accuracy_percent_by_chain"] == {},
              f"the defaults did not clear: {cleared}")  # fmt: skip
        page.context.close()


# ---- Watcher silence (scenario 9) ----

# The API alerts after 120 s without a tick and checks once a minute: about three minutes, with slack.
WATCHER_SILENT_ALERT_TIMEOUT_SECONDS = 330
WATCHER_NOT_TICKING_ALERT_COUNT_STATEMENT = (
    "SELECT count(*) FROM account_events WHERE event_type = 'operator.alert' "
    "AND data->>'kind' = 'watcher_not_ticking' AND account_id IS NOT NULL"
)


def run_compose_service_command(context: FlowContext, *arguments: str) -> None:
    run_command(compose_arguments(context.environment_file, *arguments),
                environment=compose_environment(context.environment_file),
                log_path=context.scratch_folder / "commands.log")  # fmt: skip


def scenario_watcher_silence_alerts_the_operator(context: FlowContext, *, browser: Browser) -> dict[str, object]:
    """With the watcher stopped, the API records one ``watcher_not_ticking`` operator alert on the operator's account
    (what its /activity streams); the watcher then starts again."""
    del browser
    # An earlier scenario's watcher pause may already have raised it, and then this stop counts as the same problem.
    earlier_alert_count = run_sql(context, WATCHER_NOT_TICKING_ALERT_COUNT_STATEMENT).strip()
    check(earlier_alert_count == "0", f"{earlier_alert_count} watcher_not_ticking alerts before the watcher stopped")
    started = time.monotonic()
    run_compose_service_command(context, "stop", "watcher")
    try:
        while run_sql(context, WATCHER_NOT_TICKING_ALERT_COUNT_STATEMENT).strip() == "0":
            check(time.monotonic() - started < WATCHER_SILENT_ALERT_TIMEOUT_SECONDS, "no watcher_not_ticking alert")
            time.sleep(2)
    finally:
        run_compose_service_command(context, "up", "-d", "--wait", "watcher")
    alert_count = run_sql(context, WATCHER_NOT_TICKING_ALERT_COUNT_STATEMENT).strip()
    check(alert_count == "1", f"{alert_count} watcher_not_ticking alerts recorded, not 1")
    alert_seconds = f"{time.monotonic() - started:.0f}"
    context.write_log("watcher_not_ticking_alert_recorded", seconds=alert_seconds)
    return {"alert_seconds": alert_seconds}


BROWSER_SCENARIOS: list[Callable[..., dict[str, object]]] = [
    scenario_account_form_usdc_paid_with_browser_wallet,
    scenario_eur_invoice_underpaid_then_wallet_top_up,
    scenario_overpaid_refunded_by_itself,
    scenario_underpaid_refund_requested_on_page_after_the_window,
    scenario_quantity_link_two_payers_then_sold_out,
    scenario_wrong_coin_shown_and_sent_back,
    scenario_i_paid_checks_the_chain,
    scenario_send_lock_survives_a_reload,
    scenario_settings_per_chain_defaults_and_sheet_widths,
    # Last: it stops the watcher for about three minutes.
    scenario_watcher_silence_alerts_the_operator,
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--scratch-folder", type=Path, default=Path(tempfile.gettempdir()) / "pay-7331-e2e" / "browser_flow"
    )
    parser.add_argument("--keep-stack", action="store_true", help="leave the stack running for a look")
    parser.add_argument(
        "--scenario", action="append", default=[], help="run only this scenario (its function name); repeatable"
    )
    options = parser.parse_args()
    unknown_names = set(options.scenario) - {scenario.__name__ for scenario in BROWSER_SCENARIOS}
    if unknown_names:
        parser.error(f"no such scenario: {', '.join(sorted(unknown_names))}")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            return run_scenarios_on_fork_stack(
                [
                    (scenario.__name__, functools.partial(scenario, browser=browser))
                    for scenario in BROWSER_SCENARIOS
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
