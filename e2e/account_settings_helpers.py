"""Shared by the fork e2e's scenarios that need an account's own server (polygon_fork_flow.py's API token, webhook and
Discord scenarios, merchant_operations_scenarios.py): the webhook receiver container (e2e/webhook_receiver.py) and the
/settings steps that make an API token and set the webhook behind the virtual authenticator's passkey."""

from collections.abc import Callable
import json
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit

from fork_stack import (
    BACKEND_IMAGE,
    COMPOSE_PROJECT,
    SCENARIO_TIMEOUT_SECONDS,
    FlowContext,
    FlowFailedError,
    run_command,
)
from playwright.sync_api import Page, expect

WEBHOOK_RECEIVER_CONTAINER = f"{COMPOSE_PROJECT}-webhook-receiver"
WEBHOOK_RECEIVER_PATH = "/hooks/pay?source=e2e"
WEBHOOK_RECEIVER_URL = f"http://{WEBHOOK_RECEIVER_CONTAINER}:8080{WEBHOOK_RECEIVER_PATH}"
PASSKEY_STEP_TIMEOUT_MILLISECONDS = 30_000


def start_webhook_receiver_container(context: FlowContext, *, receiver_environment: dict[str, str]) -> None:
    """e2e/webhook_receiver.py in a container on the stack's egress network (the api's way out), configured by
    ``receiver_environment`` (its docstring names the variables)."""
    environment_arguments = [
        argument for name, value in receiver_environment.items() for argument in ("--env", f"{name}={value}")
    ]
    run_command(
        ["docker", "run", "-d", "--rm", "--name", WEBHOOK_RECEIVER_CONTAINER, "--network", f"{COMPOSE_PROJECT}_egress",
         *environment_arguments,
         # As an argument, not a bind mount: the image's user cannot read a file owned by the host's user.
         "--entrypoint", "python", BACKEND_IMAGE, "-c",
         (Path(__file__).resolve().parent / "webhook_receiver.py").read_text()],
        log_path=context.scratch_folder / "commands.log",
    )  # fmt: skip


def stop_webhook_receiver() -> None:
    subprocess.run(["docker", "rm", "-f", WEBHOOK_RECEIVER_CONTAINER], capture_output=True, check=False)  # noqa: S603, S607


def read_webhook_receiver_records() -> list[dict[str, object]]:
    output = subprocess.run(  # noqa: S603
        ["docker", "exec", WEBHOOK_RECEIVER_CONTAINER, "cat", "/tmp/webhook_received.jsonl"],  # noqa: S607, S108
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def read_webhook_record_type(record: dict[str, object]) -> str:
    if record.get("discord"):
        return "discord"
    return str(json.loads(str(record["body"])).get("type"))


def wait_for_webhook_records(
    context: FlowContext, *, description: str, condition: Callable[[list[dict[str, object]]], bool]
) -> list[dict[str, object]]:
    deadline = time.monotonic() + SCENARIO_TIMEOUT_SECONDS
    records = read_webhook_receiver_records()
    while not condition(records):
        if time.monotonic() > deadline:
            context.write_log("timeout", waiting_for=description,
                              received=[(read_webhook_record_type(record), record["answered_status"])
                                        for record in records])  # fmt: skip
            raise FlowFailedError(f"webhook receiver: no {description} in {SCENARIO_TIMEOUT_SECONDS} s")
        time.sleep(1)
        records = read_webhook_receiver_records()
    return records


def select_webhook_records(records: list[dict[str, object]], *, event_type: str) -> list[dict[str, object]]:
    return [record for record in records if read_webhook_record_type(record) == event_type]


def close_settings_sheet(page: Page) -> None:
    """Closes the open /settings sheet (Escape closes its Dialog or Drawer) so the next row can open its own."""
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0)


def make_api_token_in_settings(page: Page) -> str:
    """Opens the API tokens row's sheet and makes a token there; the sheet stays open on the list."""
    page.click("#settings_api_tokens_row")
    page.click("#settings_create_api_token_button")
    page.fill("#settings_api_token_name_input", "e2e server")
    page.click("#settings_confirm_api_token_button")
    created_api_token = page.locator("#settings_created_api_token")
    expect(created_api_token).to_contain_text("pay_tok_", timeout=PASSKEY_STEP_TIMEOUT_MILLISECONDS)
    token_match = re.search(r"pay_tok_[A-Za-z0-9_-]+", created_api_token.inner_text())
    if token_match is None:
        raise FlowFailedError("settings showed no pay_tok_ token")
    page.click("#settings_api_token_done_button")
    expect(page.get_by_test_id("settings_api_token_row")).to_have_count(1)
    return token_match.group(0)


def set_webhook_in_settings(page: Page, *, webhook_url: str = WEBHOOK_RECEIVER_URL) -> str:
    """Opens the Webhook row's sheet and sets the URL there (the receiver container's unless given); the sheet stays
    open on the webhook."""
    page.click("#settings_webhook_row")
    page.click("#settings_set_webhook_button")
    page.fill("#settings_webhook_url_input", webhook_url)
    page.click("#settings_confirm_webhook_button")
    webhook_secret = page.locator("#settings_webhook_secret")
    expect(webhook_secret).to_contain_text("whsec_", timeout=PASSKEY_STEP_TIMEOUT_MILLISECONDS)
    secret_match = re.search(r"whsec_[A-Za-z0-9+/=]+", webhook_secret.inner_text())
    if secret_match is None:
        raise FlowFailedError("settings showed no whsec_ secret")
    page.click("#settings_webhook_secret_done_button")
    expect(page.locator("#settings_webhook_url")).to_contain_text(str(urlsplit(webhook_url).hostname))
    return secret_match.group(0)
