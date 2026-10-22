"""Manual Arbitrum fork e2e: a down or recovering sequencer refuses a quote despite a healthy fallback RPC.

Run with PAY_E2E_FORK_CHAIN=arbitrum. Changes only the fork's uptime-feed code, restores it afterwards, and
uses the real API, Multicall3, Redis and Postgres. No transaction goes to mainnet.
"""

import argparse
from pathlib import Path
import time

from eth_abi import encode
from fork_stack import (
    API_URL,
    FORK_CHAIN_KEY,
    FlowContext,
    call_anvil,
    check,
    compose_arguments,
    compose_environment,
    create_invoice,
    run_command,
    run_scenarios_on_fork_stack,
)
import httpx
from operator_key_scenarios import set_environment_setting

# Standalone runner, like the other manually invoked fork flows.
# ruff: noqa: INP001

UPTIME_FEED_ADDRESS = "0xFdB631F5EE196F0ed6FAa767959853A9F217697D"


def scenario_sequencer_refusal_blocks_a_healthy_fallback(context: FlowContext) -> dict[str, object]:
    invoice = create_invoice(context, amount="1", currency="USD", assets=["ETH", "USDC"])
    # The second endpoint really reports up on mainnet. Before the fix its answer could override the fork's down.
    set_environment_setting(
        context,
        "PAY_PRICE_RPC_URLS_LOCAL_ARBITRUM_FORK",
        "http://anvil:8545,https://arbitrum-one-rpc.publicnode.com",
    )
    run_command(
        compose_arguments(context.environment_file, "up", "-d", "--no-deps", "--force-recreate", "api"),
        environment=compose_environment(context.environment_file),
        log_path=context.scratch_folder / "commands.log",
    )
    deadline = time.monotonic() + 30
    while True:
        try:
            if context.http_client.get(f"{API_URL}/health").status_code == 200:
                break
        except httpx.TransportError:
            context.write_log("waiting_for_api_restart")
        check(time.monotonic() < deadline, "API did not restart")
        time.sleep(1)
    original_code = call_anvil(context.http_client, "eth_getCode", UPTIME_FEED_ADDRESS, "latest")
    refusals = []
    try:
        for label, answer, started_at in (
            ("down", 1, int(time.time()) - 7200),
            ("recovery_grace", 0, int(time.time()) - 60),
            ("uninitialized", 0, 0),
            ("healthy", 0, int(time.time()) - 7200),
        ):
            round_data = encode(
                ["uint80", "int256", "uint256", "uint256", "uint80"], [1, answer, started_at, int(time.time()), 1]
            )
            # Runtime copies and returns the five ABI words, exactly as latestRoundData does.
            call_anvil(
                context.http_client,
                "anvil_setCode",
                UPTIME_FEED_ADDRESS,
                "0x60a0600c60003960a06000f3" + round_data.hex(),
            )
            run_command(
                compose_arguments(context.environment_file, "exec", "-T", "redis", "redis-cli", "FLUSHDB"),
                environment=compose_environment(context.environment_file),
                log_path=context.scratch_folder / "commands.log",
            )
            reply = context.http_client.post(
                f"{API_URL}/public/invoices/{invoice['id']}/asset",
                json={"chain": FORK_CHAIN_KEY, "asset": "ETH"},
                headers=context.account_headers,
            )
            expected_status = 200 if label == "healthy" else 503
            check(reply.status_code == expected_status, f"{label}: got {reply.status_code}, expected {expected_status}")
            if label != "healthy":
                check(reply.json().get("code") == "price_unavailable", f"{label}: wrong refusal")
                refusals.append(label)
    finally:
        call_anvil(context.http_client, "anvil_setCode", UPTIME_FEED_ADDRESS, original_code)
    return {"invoice_id": invoice["id"], "refused": refusals, "healthy_quote_accepted": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch-folder", type=Path, required=True)
    options = parser.parse_args()
    check(FORK_CHAIN_KEY == "local_arbitrum_fork", "this flow requires the Arbitrum fork")
    raise SystemExit(
        run_scenarios_on_fork_stack(
            scratch_folder=options.scratch_folder,
            keep_stack=False,
            scenarios=[
                (
                    "scenario_sequencer_refusal_blocks_a_healthy_fallback",
                    scenario_sequencer_refusal_blocks_a_healthy_fallback,
                )
            ],
        )
    )
