"""The e2e stack on an anvil fork of Polygon mainnet (PLAN.md section 12), shared by e2e/polygon_fork_flow.py and
e2e/browser_flow.py.

`PAY_E2E_FORK_CHAIN` picks the fork (2026-10-14, PLAN.md section 15): `polygon` (the default, chain key `local_fork`),
`arbitrum` (an anvil fork of Arbitrum One, chain key `local_arbitrum_fork`; the same scenarios, with ETH where they say
POL, whole coins, so "2 POL" is "2 ETH") or `mixed`: the Polygon fork as above plus a second anvil, `anvil-arbitrum`
(compose profile `local-arbitrum-chain`), an Arbitrum One fork of its own VM on its own port, both chains enabled and both
read live prices over public endpoints (`PAY_PRICE_RPC_URLS_*`), so the two networks' feeds are real and move. Mixed is for
checks that need both networks (the payer picks the network, quantity link slot races); its scenarios are the caller's:
`FlowContext.second_chain` carries the Arbitrum fork's profile and factory, and the chain-taking helpers below
(`send_transaction`, `read_native_balance`, `fund_with_usdc`, ...) accept `chain=ARBITRUM_CHAIN`.

`run_scenarios_on_fork_stack` brings up its own Compose project (pay-7331-e2e, image tag pay-7331-backend:e2e, both renamed by
`PAY_E2E_STACK_NAME`) with
the `local-chain` profile: anvil forks Polygon from drpc (`--block-time 1 --slots-in-an-epoch 1`, so `finalized`
trails `latest` by 2 blocks), the factory is deployed with the contracts' forge script, then migrate, api and watcher
start against it. It signs up an account on `/` in Chromium with a virtual authenticator
(e2e/account_passkey_session.py) whose payout address is the stack's operator destination
(`PAY_DEFAULT_DESTINATION_ADDRESS`, the only receiver the float refunds for) and which is the stack's operator account
(`PAY_OPERATOR_ACCOUNT_IDS`: the first account signed up, so id 1, checked after the sign-up; operator alerts go to it
and only its invoices get float refunds), runs each scenario (one failing does
not skip the rest), writes the log and results to the scratch folder and removes the stack unless asked to keep it.
Invoice and refund events are read from the `account_events` table. The operator is anvil's dev account #0; payers are
fresh local keys (`create_local_account`); nothing is sent to a real network.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
import tomllib

from account_passkey_session import (
    AccountSignUpFailedError,
    account_request_headers,
    add_virtual_authenticator,
    sign_up_account_in_new_browser,
    sign_up_account_on_page,
)
from eth_abi import encode
from eth_account import Account
from eth_account.signers.local import LocalAccount
import httpx
from playwright.sync_api import Browser
from playwright.sync_api import Error as PlaywrightError

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
# PAY_E2E_STACK_NAME, PAY_E2E_API_PORT and PAY_E2E_ANVIL_PORT let parallel runs each have their own stack
# (2026-10-11); the defaults are the one stack a person runs. PAY_E2E_ANVIL_ARBITRUM_PORT is the mixed stack's second anvil.
E2E_STACK_NAME = os.environ.get("PAY_E2E_STACK_NAME", "e2e")
COMPOSE_PROJECT = f"pay-7331-{E2E_STACK_NAME}"
API_PORT = int(os.environ.get("PAY_E2E_API_PORT", "18081"))
ANVIL_PORT = int(os.environ.get("PAY_E2E_ANVIL_PORT", "18546"))
ANVIL_ARBITRUM_PORT = int(os.environ.get("PAY_E2E_ANVIL_ARBITRUM_PORT", "18547"))
# Its own tag: a build here must not replace the image the pay-7331 stack restarts from.
BACKEND_IMAGE = f"pay-7331-backend:{E2E_STACK_NAME}"
ANVIL_BINARY = Path.home() / ".foundry/versions/foundry-rs/foundry/v1.8.5/anvil"
FORGE_BINARY = Path.home() / ".foundry/bin/forge"
# localhost, not 127.0.0.1: its host is the passkey RP id, which cannot be an IP address. The HTTP client still talks to
# 127.0.0.1, where compose publishes the port; the Origin header carries this.
PUBLIC_BASE_URL = f"http://localhost:{API_PORT}"
API_URL = f"http://127.0.0.1:{API_PORT}/api/v1"
# PAY_OPERATOR_ACCOUNT_IDS: the stack's own account is the first signed up on a fresh database (an identity column).
OPERATOR_ACCOUNT_ID = 1
CHAINS_TOML = tomllib.loads((REPOSITORY_ROOT / "backend/chains.toml").read_text())["chains"]
WEI_PER_COIN = 10**18


@dataclass(frozen=True)
class ForkChain:
    """One forked network of the stack: its chains.toml key and facts, its anvil, and the token and feed the scenarios use."""

    name: str
    chain_key: str
    # The environment variable suffix of the key (``PAY_RPC_URLS_<SUFFIX>``).
    environment_suffix: str
    # chains.toml `name`: what the payer's page shows for the network.
    display_name: str
    chain_id: int
    native_symbol: str
    # The native coin's Chainlink USD feed, which the feed-outage scenario overwrites with code that reverts.
    native_usd_feed_address: str
    usdc_address: str
    # A holder of a lot of USDC: anvil_dealERC20 finds no balance slot in USDC, so it is impersonated and pays.
    usdc_holder_address: str
    anvil_url: str
    # The compose service and the host port of its anvil; the in-network RPC the watcher reads.
    anvil_service: str
    anvil_port: int
    anvil_container_url: str
    # drpc serves Polygon's archive state on its free tier (publicnode is pruned, PLAN.md section 12); Arbitrum's own
    # endpoint keeps the state of about the last 80 minutes (2026-10-14: 4,000 blocks back answered, 20,000 did not),
    # which is the longest a run on the Arbitrum fork can last (the fork reads remote state lazily, at its fork block).
    fork_url: str
    # Public endpoints for live prices (the mixed stack, ``PAY_PRICE_RPC_URLS_*``).
    live_price_rpc_urls: tuple[str, ...]

    @property
    def native_unit_wei(self) -> int:
        return WEI_PER_COIN


def build_fork_chain(
    *, name: str, chain_key: str, suffix: str, usdc_holder: str, anvil_service: str, anvil_port: int, fork_url: str,
    live_price_rpc_urls: tuple[str, ...],
) -> ForkChain:  # fmt: skip
    table = CHAINS_TOML[chain_key]
    native_asset = next(asset for asset in table["assets"] if not asset["token_address"])
    usdc_asset = next(asset for asset in table["assets"] if asset["symbol"] == "USDC")
    return ForkChain(
        name=name, chain_key=chain_key, environment_suffix=suffix, display_name=table["name"], chain_id=table["chain_id"],
        native_symbol=native_asset["symbol"], native_usd_feed_address=native_asset["usd_feed"],
        usdc_address=usdc_asset["token_address"], usdc_holder_address=usdc_holder,
        anvil_url=f"http://127.0.0.1:{anvil_port}", anvil_service=anvil_service, anvil_port=anvil_port,
        anvil_container_url=f"http://{anvil_service}:8545", fork_url=fork_url, live_price_rpc_urls=live_price_rpc_urls,
    )  # fmt: skip


# Aave v3 aPolUSDCn holds about 11M native USDC (2026-10-09); aArbUSDCn about 37M (2026-10-14).
POLYGON_CHAIN = build_fork_chain(
    name="polygon", chain_key="local_fork", suffix="LOCAL_FORK",
    usdc_holder="0xA4D94019934D8333Ef880ABFFbF2FDd611C762BD", anvil_service="anvil", anvil_port=ANVIL_PORT,
    fork_url="https://polygon.drpc.org",
    live_price_rpc_urls=("https://polygon-bor-rpc.publicnode.com", "https://polygon.drpc.org"),
)  # fmt: skip
ARBITRUM_CHAIN = build_fork_chain(
    name="arbitrum", chain_key="local_arbitrum_fork", suffix="LOCAL_ARBITRUM_FORK",
    usdc_holder="0x724dc807b04555b71ed48a6896b6F41593b8C637",
    anvil_service="anvil-arbitrum" if os.environ.get("PAY_E2E_FORK_CHAIN") == "mixed" else "anvil",
    anvil_port=ANVIL_ARBITRUM_PORT if os.environ.get("PAY_E2E_FORK_CHAIN") == "mixed" else ANVIL_PORT,
    fork_url="https://arb1.arbitrum.io/rpc",
    live_price_rpc_urls=("https://arb1.arbitrum.io/rpc", "https://arbitrum-one-rpc.publicnode.com"),
)  # fmt: skip

FORK_CHAIN_SELECTION = os.environ.get("PAY_E2E_FORK_CHAIN", "polygon")
if FORK_CHAIN_SELECTION not in {"polygon", "arbitrum", "mixed"}:
    raise SystemExit(f"PAY_E2E_FORK_CHAIN is polygon, arbitrum or mixed, not {FORK_CHAIN_SELECTION!r}")
# The chain the scenarios run on (in a mixed stack Polygon, as before), the second one a mixed stack adds, and the keys
# `PAY_ENABLED_CHAINS` names, in the order the payer's `/` offers them.
PRIMARY_CHAIN = ARBITRUM_CHAIN if FORK_CHAIN_SELECTION == "arbitrum" else POLYGON_CHAIN
SECOND_CHAIN = ARBITRUM_CHAIN if FORK_CHAIN_SELECTION == "mixed" else None
STACK_CHAINS = [PRIMARY_CHAIN] + ([SECOND_CHAIN] if SECOND_CHAIN is not None else [])

# What the scenarios say, from the primary chain (``polygon`` keeps every old value).
FORK_CHAIN_KEY = PRIMARY_CHAIN.chain_key
FORK_CHAIN_NAME = PRIMARY_CHAIN.display_name
FORK_CHAIN_ID = PRIMARY_CHAIN.chain_id
NATIVE_SYMBOL = PRIMARY_CHAIN.native_symbol
NATIVE_UNIT_WEI = PRIMARY_CHAIN.native_unit_wei
NATIVE_USD_FEED_ADDRESS = PRIMARY_CHAIN.native_usd_feed_address
USDC_ADDRESS = PRIMARY_CHAIN.usdc_address
USDC_HOLDER_ADDRESS = PRIMARY_CHAIN.usdc_holder_address
FORK_URL = PRIMARY_CHAIN.fork_url
ANVIL_URL = PRIMARY_CHAIN.anvil_url
# The blocks a native rise stays unattributed after: the watcher reads this long (chains.toml, `NATIVE_ATTRIBUTION_MAXIMUM_SECONDS`
# 400 over the fork's `seconds_per_block`) of blocks back, so a scenario that mines past it mines this many and a bit more.
NATIVE_ATTRIBUTION_WINDOW_BLOCKS = math.ceil(400 / float(CHAINS_TOML[FORK_CHAIN_KEY]["seconds_per_block"]))
USDC_BASE_UNITS = 10**6
SCENARIO_TIMEOUT_SECONDS = 180

# anvil's public dev keys (mnemonic "test test ... junk"); #0 is the operator, #1 deploys the factory.
DEV_PRIVATE_KEYS = [
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
]
DEV_ADDRESSES = [Account.from_key(private_key).address for private_key in DEV_PRIVATE_KEYS]

# Payers and third parties are fresh keys signed for locally: on a Polygon fork the dev accounts are locked
# ("No Signer available") and carry EIP-7702 delegations sweeper bots set on mainnet. warm_fork_state funds each.
LOCAL_ACCOUNTS: dict[str, LocalAccount] = {}


def create_local_account() -> LocalAccount:
    account = Account.create()
    LOCAL_ACCOUNTS[account.address] = account
    return account


# The sweeper pool (PAY_SWEEPER_PRIVATE_KEYS_LOCAL_FORK, 2026-10-13): fresh keys, so a scenario can also sign with one
# directly (a key used elsewhere). The treasury Safe's owner, whose key reaches the CLI by its variable's name only
# (``treasury execute-setup --owner-key-environment-variable``).
SWEEPER_ACCOUNTS = [create_local_account() for _ in range(3)]
TREASURY_OWNER_ACCOUNT = create_local_account()
TREASURY_OWNER_KEY_SETTING = "PAY_E2E_TREASURY_OWNER_PRIVATE_KEY"


def private_key_text_of(account: LocalAccount) -> str:
    return "0x" + account.key.hex().removeprefix("0x")


class FlowFailedError(Exception):
    pass


@dataclass
class FlowContext:
    scratch_folder: Path
    environment_file: Path
    http_client: httpx.Client
    # The account session cookie and the Origin account changes need (account_passkey_session.account_request_headers).
    account_headers: dict[str, str]
    # The cookie's value, for a browser context that should be signed in as the account.
    account_session_token: str
    factory_address: str
    # PAY_DEFAULT_DESTINATION_ADDRESS, the account's payout address: refunds happen only on the account's own invoices
    # paying it (the account is the stack's PAY_OPERATOR_ACCOUNT_IDS).
    operator_destination_address: str
    # The mixed stack's Arbitrum fork (``SECOND_CHAIN``) and the factory deployed on it; None on a one-chain stack.
    second_chain: ForkChain | None = None
    second_chain_factory_address: str | None = None

    def write_log(self, step: str, **details: object) -> None:
        write_log_line(self.scratch_folder, step, **details)


def write_log_line(scratch_folder: Path, step: str, **details: object) -> None:
    line = {"time": time.strftime("%H:%M:%S"), "step": step, **details}
    with (scratch_folder / "e2e_log.jsonl").open("a") as log_file:
        log_file.write(json.dumps(line, default=str) + "\n")
    print(f"[{line['time']}] {step} {json.dumps(details, default=str)[:300]}", flush=True)  # noqa: T201


def check(condition: bool, message: str) -> None:
    if not condition:
        raise FlowFailedError(message)


def run_command(arguments: list[str], *, environment: dict[str, str] | None = None, log_path: Path) -> str:
    """Run a command, appending its output to log_path; raise on a non-zero exit."""
    completed = subprocess.run(  # noqa: S603 (fixed argument lists)
        arguments, cwd=REPOSITORY_ROOT, env=environment, capture_output=True, text=True, check=False
    )
    with log_path.open("a") as log_file:
        log_file.write(f"$ {' '.join(arguments[:6])} ...\n{completed.stderr[-4000:]}\n")
    if completed.returncode != 0:
        raise FlowFailedError(f"{arguments[0]} {arguments[1]} failed with {completed.returncode}; see {log_path}")
    return completed.stdout


def compose_environment(environment_file: Path) -> dict[str, str]:
    return {**os.environ, "PAY_ENV_FILE": str(environment_file)}


def compose_arguments(environment_file: Path, *arguments: str) -> list[str]:
    return [
        "docker", "compose", "-p", COMPOSE_PROJECT, "--env-file", str(environment_file),
        "--profile", "local-chain", *(["--profile", "local-arbitrum-chain"] if SECOND_CHAIN is not None else []),
        *arguments,
    ]  # fmt: skip


def write_environment_file(
    environment_file: Path, *, factory_addresses: dict[str, str], fork_block_numbers: dict[str, int] | None = None
) -> None:
    """``factory_addresses`` by chain key (empty strings before the deploy); ``fork_block_numbers`` by chain key."""
    settings = {
        "POSTGRES_PASSWORD": secrets.token_hex(24),
        "LOG_LEVEL": "INFO",
        "PAY_BACKEND_IMAGE": BACKEND_IMAGE,
        "PAY_PUBLIC_BASE_URL": PUBLIC_BASE_URL,
        "PAY_API_PORT": str(API_PORT),
        "PAY_ENABLED_CHAINS": ",".join(chain.chain_key for chain in STACK_CHAINS),
        TREASURY_OWNER_KEY_SETTING: private_key_text_of(TREASURY_OWNER_ACCOUNT),
        "PAY_DEFAULT_DESTINATION_ADDRESS": Account.create().address,
        # The account run_scenarios_on_fork_stack signs up first, on an empty accounts table.
        "PAY_OPERATOR_ACCOUNT_IDS": str(OPERATOR_ACCOUNT_ID),
        "PAY_MINIMUM_REFUND_USD": "1.00",
        # Every scenario creates from 127.0.0.1; the cap of 5 would stop the run.
        "PAY_MAXIMUM_OPEN_INVOICES_PER_IP": "1000",
        "PAY_IP_API_KEY": "",
        "PAY_IP_HASH_SALT": secrets.token_hex(32),
        # A fresh test seed per stack; the recovery scenario derives salts from it as the CLI does.
        "PAY_INVOICE_SALT_SEED": secrets.token_hex(32),
        # The webhook scenario's receiver is a container on the stack's own network: plain http, a private address.
        "PAY_WEBHOOK_ALLOW_PRIVATE_HOSTS": "true",
        # The Discord scenario's stand-in for https://discord.com/api: the same receiver container. Read by the API only
        # because the fork chains are the only enabled ones.
        "PAY_E2E_DISCORD_API_BASE_URL": f"http://{COMPOSE_PROJECT}-webhook-receiver:8080/api",
        "PAY_ANVIL_BINARY": str(ANVIL_BINARY),
        "PAY_ANVIL_PORT": str(ANVIL_PORT),
        "PAY_ANVIL_CHAIN_ID": str(PRIMARY_CHAIN.chain_id),
        "PAY_ANVIL_FORK_URL": PRIMARY_CHAIN.fork_url,
        "PAY_ANVIL_FORK_BLOCK_NUMBER": str((fork_block_numbers or {}).get(PRIMARY_CHAIN.chain_key, read_fork_block_number(PRIMARY_CHAIN))),
    }  # fmt: skip
    for chain in STACK_CHAINS:
        suffix = chain.environment_suffix
        settings[f"PAY_RPC_URLS_{suffix}"] = chain.anvil_container_url
        settings[f"PAY_FACTORY_ADDRESS_{suffix}"] = factory_addresses.get(chain.chain_key, "")
        settings[f"PAY_OPERATOR_PRIVATE_KEY_{suffix}"] = DEV_PRIVATE_KEYS[0]
        settings[f"PAY_SWEEPER_PRIVATE_KEYS_{suffix}"] = ",".join(
            private_key_text_of(account) for account in SWEEPER_ACCOUNTS
        )
        if SECOND_CHAIN is not None:
            # The mixed stack's feeds are the real ones, over public endpoints: the forks' own would stand still.
            settings[f"PAY_PRICE_RPC_URLS_{suffix}"] = ",".join(chain.live_price_rpc_urls)
    if SECOND_CHAIN is not None:
        settings["PAY_ANVIL_ARBITRUM_PORT"] = str(ANVIL_ARBITRUM_PORT)
        settings["PAY_ANVIL_ARBITRUM_FORK_URL"] = SECOND_CHAIN.fork_url
        settings["PAY_ANVIL_ARBITRUM_FORK_BLOCK_NUMBER"] = str(
            (fork_block_numbers or {}).get(SECOND_CHAIN.chain_key, read_fork_block_number(SECOND_CHAIN))
        )
    if environment_file.exists():
        # Keep the secrets and fork blocks of the first write; only the factory addresses change.
        kept = dict(line.split("=", 1) for line in environment_file.read_text().splitlines() if "=" in line)
        settings = {**settings, **kept}
        for chain in STACK_CHAINS:
            settings[f"PAY_FACTORY_ADDRESS_{chain.environment_suffix}"] = factory_addresses.get(chain.chain_key, "")
    environment_file.touch(mode=0o600)
    environment_file.write_text("".join(f"{key}={value}\n" for key, value in settings.items()))


def read_fork_block_number(chain: ForkChain) -> int:
    """A head a few seconds back: every backend behind the load-balanced RPC serves its header (one did not at the
    head, 2026-10-09: "header for hash not found"): 20 Polygon blocks, 80 Arbitrum blocks (0.25 s each)."""
    reply = httpx.post(
        chain.fork_url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}, timeout=10
    )
    return int(reply.json()["result"], 16) - (80 if chain.chain_id == ARBITRUM_CHAIN.chain_id else 20)


# ---- Chain access through the fork ----


def call_anvil(http_client: httpx.Client, method: str, *params: object, chain: ForkChain = PRIMARY_CHAIN) -> object:
    """``chain`` is the fork whose anvil is asked: the primary chain's, unless a mixed-stack scenario names the other."""
    body = call_anvil_raw(http_client, method, list(params), chain=chain)
    if "error" in body:
        raise FlowFailedError(f"{method}: {body['error']}")
    return body["result"]


def call_anvil_raw(
    http_client: httpx.Client, method: str, params: list[object], *, chain: ForkChain = PRIMARY_CHAIN
) -> dict[str, object]:
    """The whole JSON-RPC reply, error included (the injected browser wallet passes errors on to the page)."""
    reply = http_client.post(chain.anvil_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    return reply.json()


def send_transaction(
    http_client: httpx.Client, *, sender: str, to: str, value_wei: int = 0, data: str = "0x", gas: int | None = None,
    chain: ForkChain = PRIMARY_CHAIN,
) -> str:  # fmt: skip
    """Sign locally for a local account (eth_sendRawTransaction); anyone else must be impersonated."""
    if sender not in LOCAL_ACCOUNTS:
        transaction: dict[str, str] = {"from": sender, "to": to, "value": hex(value_wei), "data": data}
        if gas is not None:
            transaction["gas"] = hex(gas)
        return str(call_anvil(http_client, "eth_sendTransaction", transaction, chain=chain))
    raw_transaction = sign_local_transaction(
        http_client, sender=sender, to=to, value_wei=value_wei, data=data, gas=gas, chain=chain
    )
    return str(call_anvil(http_client, "eth_sendRawTransaction", raw_transaction, chain=chain))


def sign_local_transaction(
    http_client: httpx.Client, *, sender: str, to: str, value_wei: int = 0, data: str = "0x", gas: int | None = None,
    chain: ForkChain = PRIMARY_CHAIN,
) -> str:  # fmt: skip
    """A local account's signed transaction at its pending nonce, as 0x hex (sent, or replayed by ``anvil_reorg``)."""
    account = LOCAL_ACCOUNTS[sender]
    nonce = int(str(call_anvil(http_client, "eth_getTransactionCount", sender, "pending", chain=chain)), 16)
    if gas is None:
        estimate = {"from": sender, "to": to, "value": hex(value_wei), "data": data}
        gas = int(str(call_anvil(http_client, "eth_estimateGas", estimate, chain=chain)), 16) * 3 // 2
    latest_block = call_anvil(http_client, "eth_getBlockByNumber", "latest", False, chain=chain)
    base_fee = int(latest_block["baseFeePerGas"], 16) if isinstance(latest_block, dict) else 0
    tip = 50 * 10**9
    signed = account.sign_transaction({
        "type": 2, "chainId": chain.chain_id, "nonce": nonce, "to": to, "value": value_wei, "data": data,
        "gas": gas, "maxPriorityFeePerGas": tip, "maxFeePerGas": 2 * base_fee + tip,
    })  # fmt: skip
    return "0x" + signed.raw_transaction.hex().removeprefix("0x")


def wait_for_receipt(
    http_client: httpx.Client, transaction_hash: str, *, chain: ForkChain = PRIMARY_CHAIN
) -> dict[str, str]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        receipt = call_anvil(http_client, "eth_getTransactionReceipt", transaction_hash, chain=chain)
        if isinstance(receipt, dict):
            if receipt["status"] != "0x1":
                raise FlowFailedError(f"transaction {transaction_hash} reverted")
            return receipt
        time.sleep(0.5)
    raise FlowFailedError(f"transaction {transaction_hash} was not mined in 30 s")


def pause_mining(http_client: httpx.Client, *, chain: ForkChain = PRIMARY_CHAIN) -> None:
    """No blocks until ``resume_mining`` or an explicit ``evm_mine``; sent transactions wait in anvil's pool."""
    call_anvil(http_client, "evm_setIntervalMining", 0, chain=chain)
    call_anvil(http_client, "evm_setAutomine", False, chain=chain)


def resume_mining(http_client: httpx.Client, *, chain: ForkChain = PRIMARY_CHAIN) -> None:
    call_anvil(http_client, "evm_setIntervalMining", 1, chain=chain)


def read_native_balance(http_client: httpx.Client, address: str, *, chain: ForkChain = PRIMARY_CHAIN) -> int:
    return int(str(call_anvil(http_client, "eth_getBalance", address, "latest", chain=chain)), 16)


def read_usdc_balance(http_client: httpx.Client, address: str, *, chain: ForkChain = PRIMARY_CHAIN) -> int:
    call_data = "0x70a08231" + encode(["address"], [address]).hex()
    return int(
        str(call_anvil(http_client, "eth_call", {"to": chain.usdc_address, "data": call_data}, "latest", chain=chain)),
        16,
    )


def build_erc20_transfer_data(recipient: str, base_units: int) -> str:
    return "0xa9059cbb" + encode(["address", "uint256"], [recipient, base_units]).hex()


def fund_with_usdc(
    http_client: httpx.Client, recipient: str, *, base_units: int, chain: ForkChain = PRIMARY_CHAIN
) -> None:
    """Move USDC from the impersonated holder (warm_fork_state impersonates it and gives it gas)."""
    transfer_data = build_erc20_transfer_data(recipient, base_units)
    wait_for_receipt(
        http_client,
        send_transaction(
            http_client, sender=chain.usdc_holder_address, to=chain.usdc_address, data=transfer_data, chain=chain
        ),
        chain=chain,
    )


def to_base_units(amount_text: str, *, decimals: int) -> int:
    return int(Decimal(amount_text).scaleb(decimals))


# ---- The API and the database ----


def create_invoice(context: FlowContext, **fields: object) -> dict[str, object]:
    """The account's invoice; the destination is the account's payout address (the operator's) unless given."""
    reply = context.http_client.post(
        f"{API_URL}/invoices", json={"chain": FORK_CHAIN_KEY, **fields}, headers=context.account_headers
    )
    if reply.status_code != 201:
        raise FlowFailedError(f"create invoice: {reply.status_code} {reply.text}")
    return reply.json()


def read_invoice(context: FlowContext, invoice_id: str) -> dict[str, object]:
    reply = context.http_client.get(f"{API_URL}/invoices/{invoice_id}", headers=context.account_headers)
    reply.raise_for_status()
    return reply.json()


def read_public_invoice(context: FlowContext, invoice_id: str) -> dict[str, object]:
    reply = context.http_client.get(f"{API_URL}/public/invoices/{invoice_id}")
    reply.raise_for_status()
    return reply.json()


def wait_for_invoice(
    context: FlowContext,
    invoice_id: str,
    *,
    description: str,
    condition: Callable[[dict[str, object]], bool],
    timeout_seconds: float = SCENARIO_TIMEOUT_SECONDS,
    signed_out: bool = False,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    read_any_invoice = read_public_invoice if signed_out else read_invoice
    invoice = read_any_invoice(context, invoice_id)
    while not condition(invoice):
        if time.monotonic() > deadline:
            context.write_log("timeout", invoice_id=invoice_id, waiting_for=description, invoice=invoice)
            raise FlowFailedError(f"{invoice_id}: no {description} in {timeout_seconds} s")
        time.sleep(1)
        invoice = read_any_invoice(context, invoice_id)
    return invoice


def last_sweep_final(invoice: dict[str, object]) -> bool:
    sweeps = invoice["sweeps"]
    return isinstance(sweeps, list) and bool(sweeps) and sweeps[-1]["status"] == "final"


def last_refund_final(invoice: dict[str, object]) -> bool:
    refunds = invoice["refunds"]
    return isinstance(refunds, list) and bool(refunds) and refunds[-1]["status"] == "final"


def read_environment_setting(context: FlowContext, name: str) -> str:
    for line in context.environment_file.read_text().splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1]
    raise FlowFailedError(f"the stack's settings have no {name}")


def run_sql(context: FlowContext, statement: str) -> str:
    return run_command(
        [*compose_arguments(context.environment_file, "exec", "-T", "postgres"), "psql", "-U", "pay", "-d", "pay",
         "-tAc", statement],
        environment=compose_environment(context.environment_file),
        log_path=context.scratch_folder / "commands.log",
    ).strip()  # fmt: skip


def expire_invoice_now(context: FlowContext, *, invoice_id: str, signed_out: bool = False) -> None:
    """Five minutes is the shortest lifetime: move the deadline and let the watcher expire it. ``signed_out`` reads
    the payer's view, for an invoice of an account other than the stack's own."""
    run_sql(context, f"update invoices set expires_at = now() - interval '1 second' where id = '{invoice_id}'")  # noqa: S608
    wait_for_invoice(
        context,
        invoice_id,
        description="expired",
        condition=lambda current: current["status"] == "expired",
        signed_out=signed_out,
    )


def run_operator_cli(
    context: FlowContext, *arguments: str, input_text: str | None = None, run_options: Sequence[str] = ()
) -> tuple[int, str]:
    """``python -m app.cli`` in a one-off watcher container (the api's holds no operator key, 2026-10-13); the exit
    code and everything it printed (log lines included).
    ``input_text`` goes to its stdin; ``run_options`` (e.g. ``-e NAME=value``) go to ``docker compose run``."""
    completed = subprocess.run(  # noqa: S603 (fixed argument lists)
        compose_arguments(context.environment_file, "run", "--rm", "-T", *run_options, "watcher", "python", "-m",
                          "app.cli", *arguments),
        cwd=REPOSITORY_ROOT, env=compose_environment(context.environment_file), input=input_text, capture_output=True,
        text=True, check=False,
    )  # fmt: skip
    context.write_log("operator_cli", arguments=list(arguments), exit_code=completed.returncode)
    return completed.returncode, completed.stdout + completed.stderr


def read_account_event_types(context: FlowContext, invoice_id: str) -> list[str]:
    """The invoice's events in the order they were recorded (`account_events`, PLAN.md section 8.1)."""
    if not re.fullmatch(r"inv_[0-9A-Za-z]+", invoice_id):
        raise FlowFailedError(f"not an invoice id: {invoice_id!r}")
    rows = run_sql(context, f"SELECT event_type FROM account_events WHERE invoice_id = '{invoice_id}' ORDER BY id")  # noqa: S608
    return [row for row in rows.splitlines() if row]


def wait_for_account_events(context: FlowContext, invoice_id: str, *, expected_types: set[str]) -> list[str]:
    deadline = time.monotonic() + 60
    recorded_types = read_account_event_types(context, invoice_id)
    while not expected_types <= set(recorded_types):
        if time.monotonic() > deadline:
            raise FlowFailedError(f"{invoice_id}: events {sorted(expected_types - set(recorded_types))} missing")
        time.sleep(1)
        recorded_types = read_account_event_types(context, invoice_id)
    return recorded_types


# ---- Starting and stopping the stack ----


def deploy_factory(chain: ForkChain, *, scratch_folder: Path) -> str:
    """The factory on the fork's anvil, with the contracts' forge script and dev key #1; its address."""
    # Build output goes to the scratch folder, not contracts/.
    forge_environment = {**os.environ, "FOUNDRY_OUT": str(scratch_folder / "forge-out"),
                         "FOUNDRY_CACHE_PATH": str(scratch_folder / "forge-cache"),
                         "FOUNDRY_BROADCAST": str(scratch_folder / f"forge-broadcast-{chain.name}")}  # fmt: skip
    deploy_output = run_command(
        [str(FORGE_BINARY), "script", "--root", "contracts",
         "contracts/script/DeployDepositForwarderFactory.s.sol:DeployDepositForwarderFactory",
         "--rpc-url", chain.anvil_url, "--broadcast", "--private-key", DEV_PRIVATE_KEYS[1]],
        environment=forge_environment, log_path=scratch_folder / "commands.log",
    )  # fmt: skip
    factory_match = re.search(r"DepositForwarderFactory: (0x[0-9a-fA-F]{40})", deploy_output)
    if factory_match is None:
        raise FlowFailedError(f"forge script printed no factory address on {chain.chain_key}")
    return factory_match.group(1)


def start_stack(scratch_folder: Path, environment_file: Path) -> dict[str, str]:
    """Start the anvil(s), deploy the factory on each with forge script, then the rest; return the factory addresses
    by chain key (one entry on a one-chain stack, two on the mixed one)."""
    commands_log = scratch_folder / "commands.log"
    environment = compose_environment(environment_file)
    write_environment_file(environment_file, factory_addresses={})
    run_command(compose_arguments(environment_file, "build"), environment=environment, log_path=commands_log)
    run_command(compose_arguments(environment_file, "up", "-d", "--wait", *{chain.anvil_service for chain in STACK_CHAINS}),
                environment=environment, log_path=commands_log)  # fmt: skip
    factory_addresses = {
        chain.chain_key: deploy_factory(chain, scratch_folder=scratch_folder) for chain in STACK_CHAINS
    }
    write_environment_file(environment_file, factory_addresses=factory_addresses)
    # Named services only: the anvils (and the factories on them) must not be recreated.
    run_command(compose_arguments(environment_file, "up", "-d", "--wait", "postgres", "redis", "migrate", "api",
                                  "watcher"), environment=environment, log_path=commands_log)  # fmt: skip
    return factory_addresses


def warm_fork_state(http_client: httpx.Client) -> None:
    """Read every Chainlink feed, Multicall3 and the tokens once through each anvil: a fork fetches remote state lazily
    and the first feed read took 34 s (2026-10-09), past the API's RPC timeout. Then fund the operator and every
    local account with the native coin and give the USDC holder gas."""
    for fork_chain in STACK_CHAINS:
        chain = CHAINS_TOML[fork_chain.chain_key]
        feed_addresses = [chain["eur_usd_feed"], *(asset["usd_feed"] for asset in chain["assets"])]
        if chain.get("sequencer_uptime_feed"):
            feed_addresses.append(chain["sequencer_uptime_feed"])
        token_addresses = [asset["token_address"] for asset in chain["assets"] if asset["token_address"]]
        calls = [(feed, selector) for feed in feed_addresses for selector in ("0xfeaf968c", "0x313ce567")]
        calls += [(token, "0x70a08231" + encode(["address"], [DEV_ADDRESSES[0]]).hex()) for token in token_addresses]
        calls.append(("0xcA11bde05977b3631167028862bE2a173976CA11", "0x42cbb15c"))
        for target, data in calls:
            call_anvil(http_client, "eth_call", {"to": target, "data": data}, "latest", chain=fork_chain)
        # Dev account #0 (the refund key) carries an EIP-7702 delegation on mainnet to a contract that forwards what it
        # receives: a native top-up from the treasury would leave at once. The fork drops it.
        call_anvil(http_client, "anvil_setCode", DEV_ADDRESSES[0], "0x", chain=fork_chain)
        call_anvil(http_client, "anvil_impersonateAccount", fork_chain.usdc_holder_address, chain=fork_chain)
        call_anvil(
            http_client, "anvil_setBalance", fork_chain.usdc_holder_address, hex(10 * NATIVE_UNIT_WEI), chain=fork_chain
        )
        for funded_address in [DEV_ADDRESSES[0], *LOCAL_ACCOUNTS]:
            call_anvil(http_client, "anvil_setBalance", funded_address, hex(10_000 * NATIVE_UNIT_WEI), chain=fork_chain)


def read_operator_destination_address(environment_file: Path) -> str:
    for line in environment_file.read_text().splitlines():
        if line.startswith("PAY_DEFAULT_DESTINATION_ADDRESS="):
            return line.split("=", 1)[1]
    raise FlowFailedError("the stack's settings name no PAY_DEFAULT_DESTINATION_ADDRESS")


def check_operator_account_signed_up(context: FlowContext) -> None:
    """The stack's account must be the one ``PAY_OPERATOR_ACCOUNT_IDS`` names, or no refund or operator alert scenario
    can pass."""
    operator_account_payout_address = run_sql(
        context,
        f"SELECT payout_address FROM accounts WHERE id = {OPERATOR_ACCOUNT_ID}",  # noqa: S608
    )
    check(
        operator_account_payout_address.lower() == context.operator_destination_address.lower(),
        f"account {OPERATOR_ACCOUNT_ID} (PAY_OPERATOR_ACCOUNT_IDS) is not the stack's signed-up account",
    )


def sign_up_account(*, payout_address: str, browser: Browser | None) -> str:
    """Sign up an account on `/` (payout address: the operator's), in the given browser or a throwaway one; return
    the session cookie's token."""
    username = f"e2e_{secrets.token_hex(4)}"
    try:
        if browser is None:
            return sign_up_account_in_new_browser(
                public_base_url=PUBLIC_BASE_URL, username=username, payout_address=payout_address
            )
        browser_context = browser.new_context()
        try:
            page = browser_context.new_page()
            add_virtual_authenticator(page)
            return sign_up_account_on_page(
                page, public_base_url=PUBLIC_BASE_URL, username=username, payout_address=payout_address
            )
        finally:
            browser_context.close()
    except (AccountSignUpFailedError, PlaywrightError) as error:
        raise FlowFailedError(f"account sign-up failed: {error}") from error


def stop_stack(scratch_folder: Path, environment_file: Path) -> None:
    commands_log = scratch_folder / "commands.log"
    with (scratch_folder / "stack.log").open("w") as stack_log:
        subprocess.run(  # noqa: S603
            compose_arguments(
                environment_file, "logs", "--no-color", "api", "watcher", *{chain.anvil_service for chain in STACK_CHAINS}
            ),
            cwd=REPOSITORY_ROOT, env=compose_environment(environment_file), stdout=stack_log, stderr=stack_log,
            check=False,
        )  # fmt: skip
    run_command(compose_arguments(environment_file, "down", "-v"), environment=compose_environment(environment_file),
                log_path=commands_log)  # fmt: skip


def forget_rate_limit_budgets(context: FlowContext) -> None:
    """Drop every per-IP and per-token rate budget in Redis (keys `pay:rate:*`): every request here comes from one IP,
    so without this a scenario inherits what the one before it spent of the 10 writes a minute and fails by timing."""
    run_command(
        [*compose_arguments(context.environment_file, "exec", "-T", "redis"), "redis-cli", "EVAL",
         "for _, key in ipairs(redis.call('KEYS', ARGV[1])) do redis.call('DEL', key) end return 0", "0", "pay:rate:*"],
        environment=compose_environment(context.environment_file),
        log_path=context.scratch_folder / "commands.log",
    )  # fmt: skip


def run_scenarios_on_fork_stack(
    scenarios: Sequence[tuple[str, Callable[[FlowContext], dict[str, object]]]],
    *,
    scratch_folder: Path,
    keep_stack: bool,
    browser: Browser | None = None,
) -> int:
    """Start the stack, run each named scenario, stop the stack; return the process exit code. ``browser`` signs up
    the account when given (Playwright's sync API cannot start twice in one thread), else a throwaway one."""
    scratch_folder.mkdir(parents=True, exist_ok=True)
    environment_file = scratch_folder / "e2e.env"
    environment_file.unlink(missing_ok=True)
    results: dict[str, object] = {}
    failed = False
    with httpx.Client(timeout=120) as http_client:
        try:
            factory_addresses = start_stack(scratch_folder, environment_file)
            factory_address = factory_addresses[PRIMARY_CHAIN.chain_key]
            warm_fork_state(http_client)
            operator_destination_address = read_operator_destination_address(environment_file)
            account_session_token = sign_up_account(payout_address=operator_destination_address, browser=browser)
            context = FlowContext(
                scratch_folder=scratch_folder,
                environment_file=environment_file,
                http_client=http_client,
                account_headers=account_request_headers(account_session_token, public_base_url=PUBLIC_BASE_URL),
                account_session_token=account_session_token,
                factory_address=factory_address,
                operator_destination_address=operator_destination_address,
                second_chain=SECOND_CHAIN,
                second_chain_factory_address=factory_addresses[SECOND_CHAIN.chain_key] if SECOND_CHAIN else None,
            )
            check_operator_account_signed_up(context)
            context.write_log("stack_started", factory_addresses=factory_addresses, fork_chain=FORK_CHAIN_SELECTION)
            for scenario_name, scenario in scenarios:
                try:
                    forget_rate_limit_budgets(context)
                    results[scenario_name] = {"passed": True, **scenario(context)}
                # Broad on purpose: one scenario's crash (an HTTP error, a KeyError) must not skip the rest.
                except Exception as error:  # noqa: BLE001
                    failed = True
                    results[scenario_name] = {"passed": False, "error": str(error)}
                    context.write_log(scenario_name, passed=False, error=str(error))
                    continue
                context.write_log(scenario_name, passed=True)
        except FlowFailedError as error:
            failed = True
            results["stack"] = {"passed": False, "error": str(error)}
        finally:
            (scratch_folder / "e2e_results.json").write_text(json.dumps(results, indent=2, default=str))
            if not keep_stack:
                stop_stack(scratch_folder, environment_file)
    print(json.dumps(results, indent=2, default=str))  # noqa: T201
    return 1 if failed else 0
