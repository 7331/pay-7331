"""Account sign-up for the e2e scripts (PLAN.md sections 7 and 12): account routes take only the passkey session
cookie, so a run signs up the way a person does. It opens `/` in Chromium with a CDP virtual authenticator
(https://chromedevtools.github.io/devtools-protocol/tot/WebAuthn/), opens the sign-up form under "New here?" in
the account sheet, fills it (username and payout address), makes the passkey, waits for the top bar to show the
username and hands back the session cookie's token. The token is never printed.

Used by e2e/fork_stack.py, and by e2e/run_live_flow.sh as a command:

    uv run --project backend python e2e/account_passkey_session.py --public-base-url https://pay.7331.org \\
        --username live_e2e_1234 --payout-address 0x... --session-token-file <mode-600 file> --through-local-caddy

Sign-up screens out data-centre addresses, and this host is one, so a live run's Chromium reaches
production through the host's own Caddy (``--through-local-caddy``): the public host name resolves to 127.0.0.1, the
API sees a private peer and skips the lookup by design, and the Cloudflare origin certificate Caddy serves there is
not publicly trusted, so certificate errors are ignored for this one throwaway browser.
"""

import argparse
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, CDPSession, Page, expect, sync_playwright

ACCOUNT_SESSION_COOKIE_NAME = "__Host-pay_account_session"
SIGN_IN_TIMEOUT_MILLISECONDS = 30_000
# A platform authenticator that holds discoverable credentials and verifies the user on every request, with no prompt.
VIRTUAL_AUTHENTICATOR_OPTIONS = {
    "protocol": "ctap2",
    "transport": "internal",
    "hasResidentKey": True,
    "hasUserVerification": True,
    "isUserVerified": True,
    "automaticPresenceSimulation": True,
}


class AccountSignUpFailedError(Exception):
    pass


def add_virtual_authenticator(page: Page) -> CDPSession:
    """Gives the page's target a virtual authenticator; keep the returned session open while the page signs in."""
    cdp_session = page.context.new_cdp_session(page)
    cdp_session.send("WebAuthn.enable")
    cdp_session.send("WebAuthn.addVirtualAuthenticator", {"options": VIRTUAL_AUTHENTICATOR_OPTIONS})
    return cdp_session


def read_account_session_token(browser_context: BrowserContext) -> str:
    for cookie in browser_context.cookies():
        if cookie["name"] == ACCOUNT_SESSION_COOKIE_NAME:
            return cookie["value"]
    raise AccountSignUpFailedError("no account session cookie after signing up")


def sign_up_account_on_page(page: Page, *, public_base_url: str, username: str, payout_address: str) -> str:
    """Signs up in the account sheet on `/` of a page that has a virtual authenticator and waits for the top bar to
    show "@username" (the sheet closes once signed in); returns the session cookie's token."""
    page.goto(f"{public_base_url}/")
    page.click("#account_sheet_trigger")
    page.click("#show_sign_up_form_button")
    page.fill("#sign_up_username_input", username)
    page.fill("#sign_up_payout_address_input", payout_address)
    page.click("#sign_up_button")
    expect(page.locator("#account_sheet_trigger")).to_have_text(
        f"@{username.lower()}", timeout=SIGN_IN_TIMEOUT_MILLISECONDS
    )
    return read_account_session_token(page.context)


def account_request_headers(session_token: str, *, public_base_url: str) -> dict[str, str]:
    """What an e2e HTTP client sends on account routes: the session cookie, and the Origin a change needs."""
    return {"Cookie": f"{ACCOUNT_SESSION_COOKIE_NAME}={session_token}", "Origin": public_base_url}


def sign_up_account_in_new_browser(
    *, public_base_url: str, username: str, payout_address: str, through_local_caddy: bool = False
) -> str:
    """Signs up in a throwaway headless Chromium; returns the session cookie's token."""
    launch_arguments = (
        [f"--host-resolver-rules=MAP {urlsplit(public_base_url).hostname} 127.0.0.1"] if through_local_caddy else []
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=launch_arguments)
        try:
            page = browser.new_context(ignore_https_errors=through_local_caddy).new_page()
            add_virtual_authenticator(page)
            return sign_up_account_on_page(
                page, public_base_url=public_base_url, username=username, payout_address=payout_address
            )
        finally:
            browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--public-base-url", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--payout-address", required=True)
    parser.add_argument("--session-token-file", type=Path, required=True, help="written mode 600")
    parser.add_argument(
        "--through-local-caddy", action="store_true", help="reach the public host through this host's Caddy"
    )
    options = parser.parse_args()
    session_token = sign_up_account_in_new_browser(
        public_base_url=options.public_base_url.rstrip("/"),
        username=options.username,
        payout_address=options.payout_address,
        through_local_caddy=options.through_local_caddy,
    )
    file_descriptor = os.open(options.session_token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(file_descriptor, "w") as session_token_file:
        session_token_file.write(session_token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
