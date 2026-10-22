"""End-to-end run on live Polygon mainnet against the production stack (PLAN.md section 12). Run by hand:

    e2e/run_live_flow.sh [scenario ...]        # all scenarios when none is named
    e2e/run_live_flow.sh return_leftovers      # only send the payers' leftovers back
    e2e/run_live_flow.sh settle_abandoned_refund <invoice id> <operation id> <nonce-taking hash>
                                               # finish an abandoned-refund run stopped after its race
    e2e/run_live_flow.sh pay_invoice <invoice id> <amount> [symbol]
                                               # pay an existing invoice <amount> USDC (or USDT, POL) from a payer the
                                               # float funds; run it twice for an underpay then a top-up

The wrapper runs this file in a one-off container of the production `watcher` service, so it holds production's
settings (operator key, database URL) and networks (the database, the API as http://api:8000, the public RPCs), and
can call the operator CLI (`python -m app.cli`) itself. The account session comes in as PAY_LIVE_ACCOUNT_SESSION_TOKEN
and its username as PAY_LIVE_ACCOUNT_USERNAME: the wrapper first signs up a throwaway account (payout address
`PAY_DEFAULT_DESTINATION_ADDRESS`) on `/` in headless Chromium with a virtual passkey (e2e/account_passkey_session.py),
and the run deletes that account, and so its passkey and sessions, at the end. `pay_invoice` uses no account: it
reads and picks the invoice's coin through the public routes, as the payer page does, so the wrapper signs nobody up.

Real money: payers derived from the operator key are funded from the operator float for the run and their
leftovers go back to the operator at the end (a crashed run's, at the next run). The operator key's own sends take
their nonces from the gateway's allocator (`operator_nonces`), as the watcher does, so its leak check never sees a
nonce it did not hand out (2026-10-13): the run refuses to start when the chain's `latest` count is past the row, reads
fees and gas, then takes each nonce in its own committed `UPDATE ... RETURNING` before signing, fills that nonce at
once with a 0-value transfer to itself when anything fails before an endpoint accepted the send, and first (before
its in-flight check, since the watcher's operations would wait behind it) fills any nonce a crashed run took but never
sent. With no sweeper keys the refund key signs every sweep, and one unused nonce would stall them all.
Invoices name the operator as their destination, so swept money returns to the float and a run costs gas only, except
the refund scenarios: refunds are only for an operator account's own invoices paying `PAY_DEFAULT_DESTINATION_ADDRESS`,
so theirs are made with that account's API token (PAY_LIVE_OPERATOR_API_TOKEN, which the wrapper reads from the file
PAY_LIVE_OPERATOR_API_TOKEN_FILE names; never printed; unset, those scenarios are skipped and say why) and pay that
address, and the invoice amount moves from the float to the owner's payout wallet. Nothing is written to disk. A
scenario that needs more POL float than the operator holds is skipped and says so; a token float too small for the
chosen token scenarios (the refund key's top-up target, chains.toml) refuses the run up front, naming them.
Results print as JSON lines, then one summary.
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import os
import re
import subprocess
import sys
import threading
import time

from eth_abi import encode
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount
from eth_utils import function_signature_to_4byte_selector, keccak
import httpx
import psycopg

RPC_URLS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.drpc.org",
    "https://tenderly.rpc.polygon.community",
]
API_URL = "http://api:8000/api/v1"
CHAIN_ID = 137
# chains.toml key: the operator_nonces and outgoing_operations rows and the invoices' network.
CHAIN_KEY = "polygon"
GWEI = 10**9
WEI_PER_POL = 10**18
NATIVE_TRANSFER_GAS = 21_000
# A deposit with its forwarder clone runs code on receipt (~23,700 gas); room to spare, no estimate from a lagging node.
DEPLOYED_DEPOSIT_TRANSFER_GAS = 50_000
# Polygon's pools refuse a tip under 25 gwei (https://docs.polygon.technology/tools/gas/polygon-gas-station.md).
MINIMUM_TIP = 30 * GWEI
POL_USD_FEED = "0xAB594600376Ec9fD91F8e885dADF0CE036862dE0"
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"
AGGREGATE3_VALUE_SELECTOR = function_signature_to_4byte_selector("aggregate3Value((address,bool,uint256,bytes)[])")
FACTORY_ADDRESS = "0x91749BD4e0Db4050D4F0B07f0E8f929Da552a661"
# backend/chains.toml; both have 6 decimals.
TOKEN_ADDRESSES = {
    "USDC": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
    "USDT": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
}
TOKEN_DECIMALS = 6
TRANSFER_SELECTOR = function_signature_to_4byte_selector("transfer(address,uint256)")
BALANCE_OF_SELECTOR = function_signature_to_4byte_selector("balanceOf(address)")
SWEEP_TO_DESTINATION_SELECTOR = function_signature_to_4byte_selector("sweepToDestination(bytes32,address,address[])")
RESCAN_WAIT_SECONDS = 720
# Five minutes of nonce-taken wait, then every endpoint's receipt check; public endpoints time out.
NEEDS_OWNER_WAIT_SECONDS = 900
ACTIVE_OPERATION_STATUSES = ("pending", "waiting_for_float", "broadcast", "mined")

operator: LocalAccount = Account.from_key(os.environ["PAY_OPERATOR_PRIVATE_KEY_POLYGON"])


def derive_payer(index: int) -> LocalAccount:
    """A throwaway payer derived from the operator key: the same each run, so a crashed run's leftover is returned by
    the next one (2026-10-09: a random in-memory key would strand it). Never printed; its address is."""
    seed = bytes(operator.key) + f"pay-7331 live e2e payer {index}".encode()
    return Account.from_key(keccak(seed))


payer = derive_payer(0)
# The two-senders scenario's second payer, funded from the payer; its leftover goes back at the end too.
second_payer = derive_payer(1)
# None for pay_invoice, which the wrapper runs without signing an account up.
live_account_username = os.environ.get("PAY_LIVE_ACCOUNT_USERNAME")
# The account routes take only the passkey session cookie, and a change needs the public origin as Origin.
account_headers = {
    "Cookie": f"__Host-pay_account_session={os.environ.get('PAY_LIVE_ACCOUNT_SESSION_TOKEN', '')}",
    "Origin": os.environ["PAY_PUBLIC_BASE_URL"].rstrip("/"),
}
# The operator's own payout address: the only destination the float refunds for (PLAN.md section 6).
refundable_destination = os.environ["PAY_DEFAULT_DESTINATION_ADDRESS"]
# An API token of one of the operator's accounts (PAY_OPERATOR_ACCOUNT_IDS): float refunds are only for those
# accounts' own invoices, so the refund scenarios make theirs with it and the throwaway account's would get none.
# Empty: those scenarios are skipped. Never printed.
operator_account_api_token = os.environ.get("PAY_LIVE_OPERATOR_API_TOKEN", "")
operator_account_headers = {"Authorization": f"Bearer {operator_account_api_token}"}
# Invoices made with the operator account's token: read with it too, since the throwaway account cannot see them.
operator_account_invoice_ids: set[str] = set()
OPERATOR_ACCOUNT_API_TOKEN_MISSING = (
    "PAY_LIVE_OPERATOR_API_TOKEN is not set: only an operator account's invoices get float refunds"
)
http_client = httpx.Client(timeout=30, headers={"user-agent": "pay-7331-live-e2e"})
# The newest block a receipt this run waited for was mined in; return_leftovers reads balances there.
newest_receipt_block = 0
database_url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
minimum_refund_usd = Decimal(os.environ.get("PAY_MINIMUM_REFUND_USD", "1.00"))


class ScenarioFailedError(Exception):
    pass


class ScenarioSkippedError(Exception):
    pass


def write_log_line(step: str, **details: object) -> None:
    print(json.dumps({"time": time.strftime("%H:%M:%S"), "step": step, **details}, default=str), flush=True)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise ScenarioFailedError(message)


# ---- Chain ----


def call_rpc(method: str, *params: object) -> object:
    """The first public endpoint that answers without an error. A load balancer's lagging backend can refuse what
    another serves (publicnode answered a fresh transaction's receipt with "Archive requests require a personal token",
    2026-10-13), so an error moves to the next endpoint; only every endpoint refusing raises."""
    failures = []
    for rpc_url in RPC_URLS:
        request_body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)}
        try:
            reply = http_client.post(rpc_url, json=request_body).json()
        except (httpx.HTTPError, ValueError) as error:
            failures.append(f"{rpc_url}: {type(error).__name__}")
            continue
        if "error" in reply:
            failures.append(f"{rpc_url}: {reply['error']}")
            continue
        return reply["result"]
    raise ScenarioFailedError(f"{method}: no RPC answered ({failures})")


def broadcast_to_every_rpc(raw_transaction: str) -> dict[str, str]:
    """Send to every endpoint at once, for wide propagation; what each answered."""

    def send_to(rpc_url: str) -> str:
        request_body = {"jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction", "params": [raw_transaction]}
        try:
            reply = http_client.post(rpc_url, json=request_body).json()
        except (httpx.HTTPError, ValueError) as error:
            return f"error {type(error).__name__}"
        if "error" not in reply:
            return "accepted"
        # Some endpoints answer the error as a bare string, not {code, message}.
        error = reply["error"]
        return f"error {error.get('message') if isinstance(error, dict) else error}"

    with ThreadPoolExecutor(len(RPC_URLS)) as pool:
        return dict(zip(RPC_URLS, pool.map(send_to, RPC_URLS), strict=True))


def read_native_balance(address: str, *, block_tag: str = "latest") -> int:
    return int(str(call_rpc("eth_getBalance", address, block_tag)), 16)


def read_pending_nonce(address: str) -> int:
    return int(str(call_rpc("eth_getTransactionCount", address, "pending")), 16)


# The newest nonce each account had mined here: a load-balanced endpoint's lagging node hands back a used nonce.
mined_nonces: dict[str, int] = {}


def next_nonce(address: str) -> int:
    return max(read_pending_nonce(address), mined_nonces.get(address, -1) + 1)


def read_receipt(transaction_hash: str) -> dict[str, str] | None:
    receipt = call_rpc("eth_getTransactionReceipt", transaction_hash)
    return receipt if isinstance(receipt, dict) else None


def wait_for_receipt(transaction_hash: str, *, timeout_seconds: float = 180) -> dict[str, str]:
    deadline = time.monotonic() + timeout_seconds
    while (receipt := read_receipt(transaction_hash)) is None:
        check(time.monotonic() < deadline, f"{transaction_hash} not mined in {timeout_seconds} s")
        time.sleep(2)
    global newest_receipt_block
    newest_receipt_block = max(newest_receipt_block, int(receipt["blockNumber"], 16))
    check(receipt["status"] == "0x1", f"{transaction_hash} reverted")
    return receipt


def read_fees() -> tuple[int, int, int]:
    """(base fee, max fee, tip) the way the watcher picks them."""
    latest_block = call_rpc("eth_getBlockByNumber", "latest", False)
    base_fee = int(latest_block["baseFeePerGas"], 16) if isinstance(latest_block, dict) else 0
    tip = max(int(str(call_rpc("eth_maxPriorityFeePerGas")), 16), MINIMUM_TIP)
    return base_fee, 2 * base_fee + tip, tip


def sign_transfer(
    account: LocalAccount, *, nonce: int, to: str, value_wei: int, maximum_fee: int, tip: int, gas: int
) -> tuple[str, str]:
    signed = account.sign_transaction({
        "type": 2, "chainId": CHAIN_ID, "nonce": nonce, "to": to, "value": value_wei, "gas": gas,
        "maxPriorityFeePerGas": tip, "maxFeePerGas": maximum_fee,
    })  # fmt: skip
    return "0x" + signed.raw_transaction.hex().removeprefix("0x"), "0x" + signed.hash.hex().removeprefix("0x")


def estimate_transfer_gas(account: LocalAccount, *, to: str, value_wei: int) -> int:
    """21,000 for an account; more for a deposit that already holds the forwarder clone (it runs code)."""
    estimate = int(str(call_rpc("eth_estimateGas", {"from": account.address, "to": to, "value": hex(value_wei)})), 16)
    return NATIVE_TRANSFER_GAS if estimate <= NATIVE_TRANSFER_GAS else estimate * 5 // 4


def send_native(account: LocalAccount, *, to: str, value_wei: int, nonce: int | None = None) -> str:
    """Send with the fees a wallet would pick and wait until mined."""
    _, maximum_fee, tip = read_fees()
    gas = estimate_transfer_gas(account, to=to, value_wei=value_wei)
    nonce = next_nonce(account.address) if nonce is None else nonce
    raw_transaction, transaction_hash = sign_transfer(
        account, nonce=nonce, to=to, value_wei=value_wei, maximum_fee=maximum_fee, tip=tip, gas=gas,
    )  # fmt: skip
    broadcast_to_every_rpc(raw_transaction)
    wait_for_receipt(transaction_hash)
    mined_nonces[account.address] = nonce
    return transaction_hash


def send_contract_call(account: LocalAccount, *, to: str, value_wei: int, call_data: bytes) -> str:
    _, maximum_fee, tip = read_fees()
    estimate_request = {"from": account.address, "to": to, "value": hex(value_wei), "data": "0x" + call_data.hex()}
    gas = int(str(call_rpc("eth_estimateGas", estimate_request)), 16) * 5 // 4
    nonce = next_nonce(account.address)
    signed = account.sign_transaction({
        "type": 2, "chainId": CHAIN_ID, "nonce": nonce, "to": to, "value": value_wei,
        "data": call_data, "gas": gas, "maxPriorityFeePerGas": tip, "maxFeePerGas": maximum_fee,
    })  # fmt: skip
    transaction_hash = "0x" + signed.hash.hex().removeprefix("0x")
    broadcast_to_every_rpc("0x" + signed.raw_transaction.hex().removeprefix("0x"))
    wait_for_receipt(transaction_hash)
    mined_nonces[account.address] = nonce
    return transaction_hash


def to_token_units(amount_text: str) -> int:
    return int(Decimal(amount_text).scaleb(TOKEN_DECIMALS))


def read_token_balance(symbol: str, address: str, *, block_tag: str = "latest") -> int:
    call_data = "0x" + (BALANCE_OF_SELECTOR + encode(["address"], [address])).hex()
    return int(str(call_rpc("eth_call", {"to": TOKEN_ADDRESSES[symbol], "data": call_data}, block_tag)), 16)


def send_token(account: LocalAccount, symbol: str, *, to: str, base_units: int) -> str:
    return send_contract_call(account, to=TOKEN_ADDRESSES[symbol], value_wei=0,
                              call_data=TRANSFER_SELECTOR + encode(["address", "uint256"], [to, base_units]))  # fmt: skip


def wait_for_balance_change(read_balance: Callable[[], int], *, before: int, expected_change: int) -> int:
    """The change once it shows: a lagging node can still answer the balance from before the block."""
    deadline = time.monotonic() + 90
    while (change := read_balance() - before) != expected_change and time.monotonic() < deadline:
        time.sleep(3)
    return change


def send_held_back(account: LocalAccount, *, to: str, value_wei: int) -> tuple[int, str, dict[str, str]]:
    """A valid payment whose fee cap sits under the base fee, so it waits in the pools unmined: what a payer's
    wallet leaves behind before a speed-up or a cancel. (nonce, hash, what each RPC answered)."""
    base_fee, _, _ = read_fees()
    maximum_fee = min(50 * GWEI, max(26 * GWEI, base_fee // 3))
    if maximum_fee >= base_fee:
        raise ScenarioSkippedError(f"base fee {base_fee / GWEI:.0f} gwei is too low to hold a transaction back")
    nonce = read_pending_nonce(account.address)
    raw_transaction, transaction_hash = sign_transfer(
        account, nonce=nonce, to=to, value_wei=value_wei, maximum_fee=maximum_fee, tip=25 * GWEI,
        gas=NATIVE_TRANSFER_GAS,
    )  # fmt: skip
    return nonce, transaction_hash, broadcast_to_every_rpc(raw_transaction)


# ---- Gateway ----


def to_wei(amount_text: str) -> int:
    return int(Decimal(amount_text).scaleb(18))


def post_invoice_waiting_out_rate_limit(request_body: dict, *, headers: dict[str, str]) -> httpx.Response:
    """``POST /invoices``, waiting out a 429's ``Retry-After``: account invoice creation shares the per-network budget of
    10 writes a minute (app/rate_limit_middleware.py), which bad_input_never_errors_500 spends on its own."""
    for _ in range(3):
        reply = http_client.post(f"{API_URL}/invoices", headers=headers, json=request_body)
        if reply.status_code != 429:
            return reply
        time.sleep(int(reply.headers.get("Retry-After", "60")) + 1)
    return reply


def create_invoice(request_body: dict, *, refundable: bool) -> dict:
    """``refundable``: made by the operator's account with its API token and paying ``refundable_destination``, so the
    float refunds on it; without the token those scenarios are left out up front (``REFUND_PATH_SCENARIO_NAMES``)."""
    if refundable:
        request_body = {**request_body, "destination_address": refundable_destination}
    reply = post_invoice_waiting_out_rate_limit(
        request_body, headers=operator_account_headers if refundable else account_headers
    )
    check(reply.status_code == 201, f"create invoice: {reply.status_code} {reply.text}")
    invoice = reply.json()
    if refundable:
        operator_account_invoice_ids.add(invoice["id"])
    return invoice


def create_pol_invoice(amount: str, *, note: str, refundable: bool = False, **fields: object) -> dict:
    request_body = {"amount": amount, "currency": "POL", "chain": CHAIN_KEY, "assets": ["POL"], "note": note,
                    "destination_address": operator.address, **fields}  # fmt: skip
    return create_invoice(request_body, refundable=refundable)


def create_token_invoice(amount: str, symbol: str, *, note: str, refundable: bool = False) -> dict:
    request_body = {"amount": amount, "currency": symbol, "chain": CHAIN_KEY, "assets": [symbol], "note": note,
                    "destination_address": operator.address}  # fmt: skip
    return create_invoice(request_body, refundable=refundable)


def read_invoice(invoice_id: str) -> dict:
    headers = operator_account_headers if invoice_id in operator_account_invoice_ids else account_headers
    reply = http_client.get(f"{API_URL}/invoices/{invoice_id}", headers=headers)
    check(reply.status_code == 200, f"read invoice: {reply.status_code} {reply.text}")
    return reply.json()


def wait_for_invoice(
    invoice_id: str, *, description: str, condition: Callable[[dict], bool], timeout_seconds: float = 240
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while not condition(invoice := read_invoice(invoice_id)):
        check(time.monotonic() < deadline, f"{invoice_id}: no {description} in {timeout_seconds:.0f} s "
                                           f"(status {invoice['status']})")  # fmt: skip
        time.sleep(2)
    return invoice


def is_swept_final(invoice: dict) -> bool:
    return bool(invoice["sweeps"]) and all(sweep["status"] == "final" for sweep in invoice["sweeps"])


def is_paid_and_swept(invoice: dict) -> bool:
    return invoice["status"] == "paid" and is_swept_final(invoice)


def request_signed_refund(invoice_id: str, *, signer: LocalAccount) -> httpx.Response:
    """Ask for the payer's offer and sign it with ``signer`` (the payer, or someone pretending to be)."""
    offers = http_client.post(f"{API_URL}/public/invoices/{invoice_id}/refund-offer").json()["offers"]
    offer = next(item for item in offers if item["sender"] == payer.address)
    signature = Account.sign_message(encode_defunct(text=offer["message"]), private_key=signer.key).signature
    request_body = {"nonce": offer["nonce"], "signature": "0x" + signature.hex().removeprefix("0x")}
    return http_client.post(f"{API_URL}/public/invoices/{invoice_id}/refund-requests", json=request_body)


def run_operator_cli(*arguments: str) -> tuple[int, str]:
    completed = subprocess.run(  # noqa: S603 (fixed argument lists)
        [sys.executable, "-m", "app.cli", *arguments], capture_output=True, text=True, check=False,
        env={**os.environ, "LOG_LEVEL": "WARNING"},
    )  # fmt: skip
    return completed.returncode, (completed.stdout + completed.stderr).strip()[-600:]


def query_database(statement: str, parameters: tuple[object, ...] = ()) -> list[tuple]:
    with psycopg.connect(database_url, autocommit=True) as connection:
        return connection.execute(statement, parameters).fetchall()


# ---- The operator key's nonces, through the gateway's allocator (2026-10-13) ----


def read_operator_latest_nonce() -> int:
    """The highest ``latest`` count any endpoint answers: a lagging node's lower one would look like an unused nonce."""
    counts = []
    for rpc_url in RPC_URLS:
        request_body = {"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionCount",
                        "params": [operator.address, "latest"]}  # fmt: skip
        try:
            counts.append(int(str(http_client.post(rpc_url, json=request_body).json()["result"]), 16))
        except (httpx.HTTPError, ValueError, KeyError):
            continue
    check(bool(counts), "no RPC answered the operator's nonce")
    return max(counts)


def read_operator_next_nonce() -> int:
    rows = query_database("SELECT next_nonce FROM operator_nonces WHERE chain_key = %s AND operator_address = %s",
                          (CHAIN_KEY, operator.address))  # fmt: skip
    check(len(rows) == 1, "the operator key has no operator_nonces row: let the watcher adopt it first")
    return int(rows[0][0])


def allocate_operator_nonce() -> int:
    """The runner's order: the nonce is committed taken (autocommit) before anything is signed with it."""
    ((nonce,),) = query_database(
        "UPDATE operator_nonces SET next_nonce = next_nonce + 1 WHERE chain_key = %s AND operator_address = %s "
        "RETURNING next_nonce - 1", (CHAIN_KEY, operator.address),
    )  # fmt: skip
    return int(nonce)


def fill_unused_operator_nonce(nonce: int) -> None:
    """A 0-value transfer of the operator to itself at a nonce taken but never sent, so the nonces after it can mine.
    A failure is logged, not raised: the next run's ``prepare_operator_nonces`` fills it."""
    try:
        filler_hash = send_native(operator, to=operator.address, value_wei=0, nonce=nonce)
    except Exception as error:  # noqa: BLE001 (any failure leaves it to the next run)
        write_log_line("unused_operator_nonce_fill_failed", nonce=nonce, error=f"{type(error).__name__}: {error}")
        return
    write_log_line("unused_operator_nonce_filled", nonce=nonce, transaction=filler_hash)


def send_from_operator(*, to: str, value_wei: int, call_data: bytes = b"") -> str:
    """An operator send at a nonce from the allocator, waited until mined. Fees and gas are read first, so an RPC
    error or a reverting estimate takes no nonce; anything failing between taking the nonce and a broadcast some
    endpoint accepted fills that nonce at once. With no sweeper keys the refund key signs every sweep, so an
    unused nonce would hold all of them back."""
    _, maximum_fee, tip = read_fees()
    estimate_request = {"from": operator.address, "to": to, "value": hex(value_wei), "data": "0x" + call_data.hex()}
    gas = (int(str(call_rpc("eth_estimateGas", estimate_request)), 16) * 5 // 4 if call_data
           else estimate_transfer_gas(operator, to=to, value_wei=value_wei))  # fmt: skip
    nonce = allocate_operator_nonce()
    try:
        signed = operator.sign_transaction({
            "type": 2, "chainId": CHAIN_ID, "nonce": nonce, "to": to, "value": value_wei,
            "data": call_data, "gas": gas, "maxPriorityFeePerGas": tip, "maxFeePerGas": maximum_fee,
        })  # fmt: skip
        transaction_hash = "0x" + signed.hash.hex().removeprefix("0x")
        answers = broadcast_to_every_rpc("0x" + signed.raw_transaction.hex().removeprefix("0x"))
        check(any(answer == "accepted" or "already known" in answer.lower() for answer in answers.values()),
              f"no endpoint accepted the operator's send at nonce {nonce}: {answers}")  # fmt: skip
    except BaseException:  # Ctrl-C included: the nonce is taken and nothing went out
        fill_unused_operator_nonce(nonce)
        raise
    wait_for_receipt(transaction_hash)
    mined_nonces[operator.address] = nonce
    return transaction_hash


def prepare_operator_nonces() -> str | None:
    """None when the operator key may send; else why the run must not start. The chain is read before the row, as
    the watcher's leak check does. A nonce between them that no active operation holds was taken by a crashed run and
    never sent, or abandoned by an ended one: a 0-value transfer to itself uses it, so the nonces after it can mine."""
    latest_nonce = read_operator_latest_nonce()
    next_nonce_of_row = read_operator_next_nonce()
    if latest_nonce > next_nonce_of_row:
        return (f"the operator key's latest nonce {latest_nonce} is past the allocator's {next_nonce_of_row}: it was "
                "used outside the gateway, and the watcher freezes it")  # fmt: skip
    held_nonces = {int(nonce) for (nonce,) in query_database(
        "SELECT nonce FROM outgoing_operations WHERE chain_key = %s AND operator_address = %s AND nonce >= %s "
        "AND status = ANY(%s)", (CHAIN_KEY, operator.address, latest_nonce, list(ACTIVE_OPERATION_STATUSES)))}  # fmt: skip
    for nonce in range(latest_nonce, next_nonce_of_row):
        if nonce in held_nonces:
            continue
        filler_hash = send_native(operator, to=operator.address, value_wei=0, nonce=nonce)
        write_log_line("unused_operator_nonce_filled", nonce=nonce, transaction=filler_hash)
    return None


def find_token_float_shortfalls(chosen_names: list[str]) -> list[str]:
    """What the operator's token float lacks for the chosen scenarios: the payer's funding up front plus what the
    float itself pays out (a refund). The float is only the refund key's top-up target (chains.toml), so a
    run must not start and then fail at a funding transfer the float cannot cover."""
    needed_units: dict[str, int] = {}
    scenarios_by_symbol: dict[str, list[str]] = {}
    for name in chosen_names:
        for symbol, amount_text in [*TOKEN_FUNDING.get(name, {}).items(), *TOKEN_FLOAT_PAYOUTS.get(name, {}).items()]:
            needed_units[symbol] = needed_units.get(symbol, 0) + to_token_units(amount_text)
            scenarios_by_symbol.setdefault(symbol, []).append(name)
    shortfalls = []
    for symbol, units in needed_units.items():
        held_units = read_token_balance(symbol, operator.address)
        if held_units < units:
            scenarios = ", ".join(dict.fromkeys(scenarios_by_symbol[symbol]))
            shortfalls.append(f"{symbol}: needs {Decimal(units).scaleb(-TOKEN_DECIMALS)}, the operator holds "
                              f"{Decimal(held_units).scaleb(-TOKEN_DECIMALS)} (for {scenarios})")  # fmt: skip
    return shortfalls


def read_pol_price_usd() -> Decimal:
    """Chainlink's POL/USD feed on Polygon (chains.toml), ``latestRoundData`` answer with 8 decimals."""
    reply = str(call_rpc("eth_call", {"to": POL_USD_FEED, "data": "0xfeaf968c"}, "latest"))
    return Decimal(int(reply[2 + 64 : 2 + 128], 16)).scaleb(-8)


def require_float_for_refund(excess_pol: Decimal) -> None:
    """Skip when the excess would be under the minimum refund or the float cannot pay it."""
    if excess_pol * read_pol_price_usd() < minimum_refund_usd:
        raise ScenarioSkippedError(f"{excess_pol} POL is under the {minimum_refund_usd} USD minimum refund")
    if read_native_balance(operator.address) < to_wei(str(excess_pol)) + to_wei("0.5"):
        raise ScenarioSkippedError(f"the float cannot pay a {excess_pol} POL refund")


# ---- Scenarios ----


def scenario_speed_up() -> dict[str, object]:
    """A payment held back under the base fee, then the same payment at its nonce with real fees (a new TXID)."""
    invoice = create_pol_invoice("0.6", note="live e2e: speed-up")
    deposit_address = invoice["deposit_address"]
    nonce, held_back_hash, held_back_answers = send_held_back(payer, to=deposit_address, value_wei=to_wei("0.6"))
    time.sleep(20)
    held_view = read_invoice(invoice["id"])
    check(held_view["status"] == "awaiting_payment", f"status {held_view['status']} while the payment was held back")
    check(read_receipt(held_back_hash) is None, "the held-back payment mined")
    replacement_hash = send_native(payer, to=deposit_address, value_wei=to_wei("0.6"), nonce=nonce)
    invoice = wait_for_invoice(invoice["id"], description="paid and swept", condition=is_paid_and_swept)
    payment_hashes = [payment["tx_hash"] for payment in invoice["payments"]]
    check(payment_hashes == [replacement_hash], f"payments {payment_hashes}, expected only {replacement_hash}")
    check(invoice["amount_received"] == "0.6", f"received {invoice['amount_received']}")
    check(read_receipt(held_back_hash) is None, "the replaced payment has a receipt")
    return {"invoice": invoice["id"], "abandoned": held_back_hash, "abandoned_broadcast": held_back_answers,
            "replacement": replacement_hash}  # fmt: skip


def scenario_cancel_then_pay() -> dict[str, object]:
    """A payment held back, cancelled by a self-transfer at its nonce; nothing is credited; then a real payment."""
    invoice = create_pol_invoice("0.6", note="live e2e: cancel then pay")
    deposit_address = invoice["deposit_address"]
    nonce, held_back_hash, held_back_answers = send_held_back(payer, to=deposit_address, value_wei=to_wei("0.6"))
    time.sleep(10)
    cancel_hash = send_native(payer, to=payer.address, value_wei=0, nonce=nonce)
    time.sleep(15)
    cancelled_view = read_invoice(invoice["id"])
    check(cancelled_view["status"] == "awaiting_payment" and not cancelled_view["payments"],
          f"after the cancel: {cancelled_view['status']}, payments {cancelled_view['payments']}")  # fmt: skip
    check(read_native_balance(deposit_address) == 0, "the deposit holds money after the cancel")
    payment_hash = send_native(payer, to=deposit_address, value_wei=to_wei("0.6"))
    invoice = wait_for_invoice(invoice["id"], description="paid and swept", condition=is_paid_and_swept)
    check([payment["tx_hash"] for payment in invoice["payments"]] == [payment_hash], f"payments {invoice['payments']}")
    return {"invoice": invoice["id"], "abandoned": held_back_hash, "abandoned_broadcast": held_back_answers,
            "cancel": cancel_hash, "payment": payment_hash}  # fmt: skip


def scenario_late_payment_swept() -> dict[str, object]:
    """Money sent to a deposit after its invoice was paid and swept: flagged and swept by the rescan."""
    invoice = create_pol_invoice("0.6", note="live e2e: late payment")
    send_native(payer, to=invoice["deposit_address"], value_wei=to_wei("0.6"))
    wait_for_invoice(invoice["id"], description="paid and swept", condition=is_paid_and_swept)
    late_hash = send_native(payer, to=invoice["deposit_address"], value_wei=to_wei("0.6"))
    invoice = wait_for_invoice(
        invoice["id"], description="late payment swept", timeout_seconds=RESCAN_WAIT_SECONDS,
        condition=lambda current: current["late_payment"] and len(current["sweeps"]) >= 2 and is_swept_final(current),
    )  # fmt: skip
    return {"invoice": invoice["id"], "late_payment": late_hash, "sweeps": invoice["sweeps"]}


def scenario_dust_on_a_closed_deposit_is_not_swept() -> dict[str, object]:
    """1 wei to a paid and swept deposit must not make the operator pay a sweep's gas (dust griefing)."""
    invoice = create_pol_invoice("0.6", note="live e2e: dust after the sweep")
    send_native(payer, to=invoice["deposit_address"], value_wei=to_wei("0.6"))
    invoice = wait_for_invoice(invoice["id"], description="paid and swept", condition=is_paid_and_swept)
    dust_hash = send_native(payer, to=invoice["deposit_address"], value_wei=1)
    started = time.monotonic()
    while time.monotonic() - started < RESCAN_WAIT_SECONDS:
        current = read_invoice(invoice["id"])
        check(len(current["sweeps"]) == 1, f"the dust was swept: {current['sweeps']}")
        time.sleep(10)
    return {"invoice": invoice["id"], "dust": dust_hash, "watched_seconds": RESCAN_WAIT_SECONDS}


def scenario_overpaid_refund_attempt_abandoned() -> dict[str, object]:
    """Overpay; the automatic refund's attempt is raced by another operator transaction at its nonce (the attempt
    abandoned): needs_owner with the amount reserved and no endless bumps, the owner's mark-paid refuses the wrong
    transaction, mark-unpaid with the nonce-taking transaction as proof frees it with no automatic second refund, a
    wrong signer is refused and the payer's signed
    request pays it."""
    excess_pol = Decimal("10.5")
    require_float_for_refund(excess_pol)
    invoice = create_pol_invoice("0.6", note="live e2e: overpaid, refund attempt abandoned", refundable=True)
    invoice_id = invoice["id"]
    race: dict[str, object] = {}

    def take_the_refund_nonce() -> None:
        deadline = time.monotonic() + 600  # finality, then the refund's first attempt
        with psycopg.connect(database_url, autocommit=True) as connection:
            while (row := connection.execute(
                "SELECT o.id, o.nonce, a.transaction_hash, a.max_fee_per_gas FROM outgoing_attempts a JOIN "
                "outgoing_operations o ON o.id = a.operation_id WHERE o.invoice_id = %s AND o.kind = 'refund' "
                "ORDER BY a.id LIMIT 1", (invoice_id,)).fetchone()) is None:  # fmt: skip
                if time.monotonic() > deadline:
                    race["error"] = "no refund attempt in 600 s"
                    return
                time.sleep(0.02)
        operation_id, nonce, attempt_hash, attempt_maximum_fee = row
        raw_transaction, taking_hash = sign_transfer(
            operator, nonce=int(nonce), to=operator.address, value_wei=0,
            maximum_fee=max(int(attempt_maximum_fee) * 2, 2_000 * GWEI), tip=1_500 * GWEI, gas=NATIVE_TRANSFER_GAS,
        )  # fmt: skip
        race.update(operation_id=operation_id, nonce=int(nonce), attempt=attempt_hash, taking=taking_hash,
                    taking_broadcast=broadcast_to_every_rpc(raw_transaction))  # fmt: skip

    racer = threading.Thread(target=take_the_refund_nonce)
    racer.start()
    send_native(payer, to=invoice["deposit_address"], value_wei=to_wei(str(Decimal("0.6") + excess_pol)))
    racer.join()
    check("error" not in race, str(race.get("error")))
    deadline = time.monotonic() + 120
    while (attempt_receipt := read_receipt(str(race["attempt"]))) is None and read_receipt(str(race["taking"])) is None:
        check(time.monotonic() < deadline, "neither the attempt nor the nonce-taking transaction mined")
        time.sleep(2)
    race["won"] = attempt_receipt is None
    write_log_line("race", **race)
    if not race["won"]:
        invoice = wait_for_invoice(invoice_id, description="final refund",
                                   condition=lambda current: current["refunds"][-1]["status"] == "final")  # fmt: skip
        return {"invoice": invoice_id, "race": race, "note": "the watcher's attempt mined first; refunded normally"}
    return {"race": race, **settle_abandoned_refund(invoice_id, operation_id=int(str(race["operation_id"])),
                                                    nonce_taking_hash=str(race["taking"]))}  # fmt: skip


def settle_abandoned_refund(invoice_id: str, *, operation_id: int, nonce_taking_hash: str) -> dict[str, object]:
    """The owner's half of an abandoned refund: needs_owner with the amount reserved, mark-paid refuses the
    nonce-taking transaction, mark-unpaid with it as proof frees the amount with no automatic second refund, a wrong
    signer is refused and the payer's signed request pays it. Also run alone (``settle_abandoned_refund``) to finish
    a run stopped after the race."""
    invoice = wait_for_invoice(invoice_id, description="needs_owner", timeout_seconds=NEEDS_OWNER_WAIT_SECONDS,
                               condition=lambda current: current["refunds"][-1]["status"] == "needs_owner")  # fmt: skip
    check(
        not invoice["refund_offer"]["available"],
        f"needs_owner must keep the amount reserved: {invoice['refund_offer']}",
    )
    excess_pol = Decimal(invoice["refunds"][-1]["amount"])
    ((attempt_count,),) = query_database("SELECT count(*) FROM outgoing_attempts WHERE operation_id = %s",
                                         (operation_id,))  # fmt: skip
    check(attempt_count <= 3, f"{attempt_count} attempts: the watcher kept bumping a used nonce")
    wrong_paid_code, wrong_paid_output = run_operator_cli("refunds", "mark-paid", str(operation_id),
                                                       "--transaction-hash", nonce_taking_hash)  # fmt: skip
    check(wrong_paid_code == 1, f"mark-paid accepted the nonce-taking transaction: {wrong_paid_output}")
    unpaid_code, unpaid_output = run_operator_cli("refunds", "mark-unpaid", str(operation_id), "--nonce-taken-by",
                                               nonce_taking_hash)  # fmt: skip
    check(unpaid_code == 0 and "is now failed" in unpaid_output, f"mark-unpaid: {unpaid_output}")
    time.sleep(10)  # a few watcher ticks: no automatic second refund to a sender that had one
    invoice = read_invoice(invoice_id)
    check(len(invoice["refunds"]) == 1, f"an automatic second refund followed mark-unpaid: {invoice['refunds']}")
    check(invoice["refund_offer"]["available"], "the freed amount is not offered again")
    wrong_signer_reply = request_signed_refund(invoice_id, signer=Account.create())
    check(wrong_signer_reply.status_code == 400, f"a wrong signer got {wrong_signer_reply.status_code}")
    payer_balance_before = read_native_balance(payer.address)
    second_reply = request_signed_refund(invoice_id, signer=payer)
    check(second_reply.status_code == 200, f"second refund request: {second_reply.status_code} {second_reply.text}")
    invoice = wait_for_invoice(invoice_id, description="final refund",
                               condition=lambda current: current["refunds"][-1]["status"] == "final")  # fmt: skip
    refunded_wei = wait_for_balance_change(lambda: read_native_balance(payer.address), before=payer_balance_before,
                                           expected_change=to_wei(str(excess_pol)))  # fmt: skip
    check(refunded_wei == to_wei(str(excess_pol)), f"the payer got {refunded_wei} wei back, expected {excess_pol} POL")
    return {"invoice": invoice_id, "attempts_on_abandoned_operation": attempt_count, "refunds": invoice["refunds"]}


def scenario_two_senders_split_one_invoice() -> dict[str, object]:
    """Two accounts pay half each: one invoice, two attributed payments, paid once, swept once."""
    send_native(payer, to=second_payer.address, value_wei=to_wei("0.35"))
    invoice = create_pol_invoice("0.6", note="live e2e: two senders")
    first_hash = send_native(payer, to=invoice["deposit_address"], value_wei=to_wei("0.3"))
    invoice = wait_for_invoice(
        invoice["id"], description="underpaid", condition=lambda current: current["status"] == "underpaid"
    )
    check(invoice["amount_remaining"] == "0.3", f"remaining {invoice['amount_remaining']}")
    second_hash = send_native(second_payer, to=invoice["deposit_address"], value_wei=to_wei("0.3"))
    invoice = wait_for_invoice(invoice["id"], description="paid and swept", condition=is_paid_and_swept)
    senders = {payment["tx_hash"]: payment["sender"] for payment in invoice["payments"]}
    check(senders == {first_hash: payer.address, second_hash: second_payer.address}, f"payments {invoice['payments']}")
    check(len(invoice["sweeps"]) == 1, f"sweeps {invoice['sweeps']}")
    return {"invoice": invoice["id"], "payments": invoice["payments"]}


def scenario_one_contract_call_pays_two_invoices() -> dict[str, object]:
    """Multicall3's aggregate3Value pays two deposits in one transaction (internal transfers, as an exchange batch
    or a smart wallet sends): both invoices are credited and swept, and no sender is invented for them."""
    first = create_pol_invoice("0.6", note="live e2e: batch payment 1")
    second = create_pol_invoice("0.6", note="live e2e: batch payment 2")
    calls = [
        (first["deposit_address"], False, to_wei("0.6"), b""),
        (second["deposit_address"], False, to_wei("0.6"), b""),
    ]
    call_data = AGGREGATE3_VALUE_SELECTOR + encode(["(address,bool,uint256,bytes)[]"], [calls])
    batch_hash = send_contract_call(payer, to=MULTICALL3_ADDRESS, value_wei=to_wei("1.2"), call_data=call_data)
    first = wait_for_invoice(first["id"], description="paid and swept", condition=is_paid_and_swept)
    second = wait_for_invoice(second["id"], description="paid and swept", condition=is_paid_and_swept)
    for invoice in (first, second):
        # No row: money no direct transaction explains is the unknown sender's share (accounting_service.py).
        check(invoice["amount_received"] == "0.6" and invoice["payments"] == [],
              f"{invoice['id']} received {invoice['amount_received']}, payments {invoice['payments']}")  # fmt: skip
    return {"invoices": [first["id"], second["id"]], "batch_transaction": batch_hash,
            "payments": [first["payments"], second["payments"]]}  # fmt: skip


def scenario_accuracy_boundary_then_accept_partial() -> dict[str, object]:
    """1% accuracy: 0.594 of 0.6 POL (a wallet or exchange keeping a fee) is paid; 0.5939 is underpaid, then the account
    accepts it as partial and it sweeps."""
    inside = create_pol_invoice("0.6", note="live e2e: inside the accuracy buffer", accuracy_percent="1")
    outside = create_pol_invoice("0.6", note="live e2e: outside the accuracy buffer", accuracy_percent="1")
    send_native(payer, to=inside["deposit_address"], value_wei=to_wei("0.594"))
    send_native(payer, to=outside["deposit_address"], value_wei=to_wei("0.5939"))
    inside = wait_for_invoice(inside["id"], description="paid and swept", condition=is_paid_and_swept)
    outside = wait_for_invoice(
        outside["id"], description="underpaid", condition=lambda current: current["status"] == "underpaid"
    )
    check(not inside["partial"], "a payment inside the buffer is flagged partial")
    check(not outside["sweeps"], f"an underpaid deposit was swept: {outside['sweeps']}")
    reply = http_client.post(f"{API_URL}/invoices/{outside['id']}/accept-partial", headers=account_headers)
    check(reply.status_code == 200, f"accept-partial: {reply.status_code} {reply.text}")
    outside = wait_for_invoice(outside["id"], description="partial paid and swept", condition=is_paid_and_swept)
    check(outside["partial"] and outside["amount_received"] == "0.5939", f"partial {outside['partial']}")
    second_accept = http_client.post(f"{API_URL}/invoices/{outside['id']}/accept-partial", headers=account_headers)
    check(second_accept.status_code == 409, f"a second accept-partial got {second_accept.status_code}")
    return {"inside": inside["id"], "outside": outside["id"], "outside_sweeps": outside["sweeps"]}


def scenario_cancel_while_a_payment_is_in_flight() -> dict[str, object]:
    """The account cancels while a payment is on its way: either the cancel is refused (the watcher saw it) and the
    invoice is paid, or the cancel wins and the late money is swept and flagged by the rescan. Never lost."""
    invoice = create_pol_invoice("0.6", note="live e2e: cancel while paying")
    _, maximum_fee, tip = read_fees()
    raw_transaction, payment_hash = sign_transfer(
        payer, nonce=read_pending_nonce(payer.address), to=invoice["deposit_address"], value_wei=to_wei("0.6"),
        maximum_fee=maximum_fee, tip=tip, gas=NATIVE_TRANSFER_GAS,
    )  # fmt: skip
    broadcast_to_every_rpc(raw_transaction)
    cancel_reply = http_client.post(f"{API_URL}/invoices/{invoice['id']}/cancel", headers=account_headers)
    wait_for_receipt(payment_hash)
    if cancel_reply.status_code == 409:
        invoice = wait_for_invoice(invoice["id"], description="paid and swept", condition=is_paid_and_swept)
        return {"invoice": invoice["id"], "cancel": "refused", "status": invoice["status"]}
    check(cancel_reply.status_code == 200, f"cancel: {cancel_reply.status_code} {cancel_reply.text}")
    invoice = wait_for_invoice(
        invoice["id"], description="late money swept", timeout_seconds=RESCAN_WAIT_SECONDS,
        condition=lambda current: current["late_payment"] and is_swept_final(current),
    )  # fmt: skip
    check(invoice["status"] == "cancelled", f"status {invoice['status']}")
    return {"invoice": invoice["id"], "cancel": "won", "sweeps": invoice["sweeps"]}


def scenario_three_inputs_from_one_address() -> dict[str, object]:
    """One address pays in three parts: underpaid, still underpaid, then past the amount. Swept once, every input
    attributed to it; the excess goes back by itself once final if it is worth the minimum refund, else it goes to the
    destination with the rest."""
    invoice = create_pol_invoice("0.6", note="live e2e: three inputs from one address", refundable=True)
    input_hashes = []
    for received_after in ("0.25", "0.5"):
        input_hashes.append(send_native(payer, to=invoice["deposit_address"], value_wei=to_wei("0.25")))
        invoice = wait_for_invoice(
            invoice["id"], description=f"underpaid with {received_after}",
            condition=lambda current, received=received_after: current["status"] == "underpaid"
            and current["amount_received"] == received,
        )  # fmt: skip
        check(not invoice["sweeps"], f"an underpaid deposit was swept: {invoice['sweeps']}")
    # The 0.15 excess goes back by itself once final when it is worth the minimum refund, else to the destination.
    excess_refunded = Decimal("0.15") * read_pol_price_usd() >= minimum_refund_usd
    input_hashes.append(send_native(payer, to=invoice["deposit_address"], value_wei=to_wei("0.25")))
    payer_balance_after_inputs = read_native_balance(payer.address)
    if excess_refunded:
        invoice = wait_for_invoice(
            invoice["id"], description="overpaid, swept and refunded",
            condition=lambda current: current["status"] == "overpaid" and is_swept_final(current)
            and bool(current["refunds"]) and current["refunds"][-1]["status"] == "final",
        )  # fmt: skip
        refunded_wei = wait_for_balance_change(lambda: read_native_balance(payer.address),
                                               before=payer_balance_after_inputs, expected_change=to_wei("0.15"))  # fmt: skip
        check(refunded_wei == to_wei("0.15"), f"the payer got {refunded_wei} wei back, expected 0.15 POL")
    else:
        invoice = wait_for_invoice(invoice["id"], description="paid and swept", condition=is_paid_and_swept)
        check(not invoice["refunds"], f"an excess under the minimum was refunded: {invoice['refunds']}")
    check([payment["tx_hash"] for payment in invoice["payments"]] == input_hashes, f"payments {invoice['payments']}")
    check({payment["sender"] for payment in invoice["payments"]} == {payer.address}, "an input attributed elsewhere")
    check(invoice["amount_received"] == "0.75" and [sweep["amount"] for sweep in invoice["sweeps"]] == ["0.75"],
          f"received {invoice['amount_received']}, sweeps {invoice['sweeps']}")  # fmt: skip
    return {
        "invoice": invoice["id"],
        "inputs": input_hashes,
        "sweeps": invoice["sweeps"],
        "refunds": invoice["refunds"],
    }


def scenario_payment_with_too_little_gas_is_not_credited() -> dict[str, object]:
    """A deposit someone already deployed (anyone may run the factory's sweep) runs code on receipt, so a wallet's
    21,000-gas payment reverts. Sent right before a real top-up, it is neither credited nor recorded as a payment (it
    would have claimed a refund share); the top-up pays the invoice."""
    invoice = create_pol_invoice("0.6", note="live e2e: a payment with too little gas")
    deposit = invoice["deposit_address"]
    first_hash = send_native(payer, to=deposit, value_wei=to_wei("0.3"))
    invoice = wait_for_invoice(
        invoice["id"], description="underpaid", condition=lambda current: current["status"] == "underpaid"
    )
    ((invoice_salt,),) = query_database("SELECT invoice_salt FROM invoices WHERE id = %s", (invoice["id"],))
    third_party_sweep_hash = send_contract_call(
        payer, to=FACTORY_ADDRESS, value_wei=0,
        call_data=SWEEP_TO_DESTINATION_SELECTOR + encode(["bytes32", "address", "address[]"], [bytes(invoice_salt), operator.address, []]),
    )  # fmt: skip
    deadline = time.monotonic() + 30  # an endpoint's lagging node may not have the sweep's block yet
    while str(call_rpc("eth_getCode", deposit, "latest")) in {"0x", ""}:
        check(time.monotonic() < deadline, "the third-party sweep deployed no clone")
        time.sleep(2)
    _, maximum_fee, tip = read_fees()
    # From the sweep call itself, not a pending read: a load-balanced endpoint's lagging node handed back a used nonce
    # and estimated 21,000 for a deposit whose clone it had not seen yet (2026-10-09).
    nonce = int(str(call_rpc("eth_getTransactionByHash", third_party_sweep_hash)["nonce"]), 16) + 1
    reverting_raw, reverting_hash = sign_transfer(payer, nonce=nonce, to=deposit, value_wei=to_wei("0.5"),
                                                  maximum_fee=maximum_fee, tip=tip, gas=NATIVE_TRANSFER_GAS)  # fmt: skip
    top_up_raw, top_up_hash = sign_transfer(
        payer, nonce=nonce + 1, to=deposit, value_wei=to_wei("0.3"), maximum_fee=maximum_fee, tip=tip,
        gas=DEPLOYED_DEPOSIT_TRANSFER_GAS,
    )  # fmt: skip
    broadcast_to_every_rpc(reverting_raw)
    broadcast_to_every_rpc(top_up_raw)
    deadline = time.monotonic() + 180
    while (reverting_receipt := read_receipt(reverting_hash)) is None:
        check(time.monotonic() < deadline, "the 21,000-gas payment was not mined in 180 s")
        time.sleep(2)
    check(reverting_receipt["status"] == "0x0", "the 21,000-gas payment to a deployed deposit did not revert")
    wait_for_receipt(top_up_hash)
    # All 0.6 swept: the third party's 0.3, then the gateway's own sweep of the 0.3 top-up (under the dust floor, but
    # the gateway had not swept this deposit itself yet; 2026-10-09).
    invoice = wait_for_invoice(
        invoice["id"], description="paid and all of it swept",
        condition=lambda current: is_paid_and_swept(current)
        and sum(Decimal(sweep["amount"] or "0") for sweep in current["sweeps"]) == Decimal("0.6"),
    )  # fmt: skip
    payment_hashes = [payment["tx_hash"] for payment in invoice["payments"]]
    check(payment_hashes == [first_hash, top_up_hash], f"payments {invoice['payments']}")
    check(invoice["amount_received"] == "0.6", f"received {invoice['amount_received']}")
    return {"invoice": invoice["id"], "third_party_sweep": third_party_sweep_hash, "reverted": reverting_hash,
            "reverted_block": int(reverting_receipt["blockNumber"], 16), "top_up": top_up_hash,
            "top_up_block": int(read_receipt(top_up_hash)["blockNumber"], 16), "sweeps": invoice["sweeps"]}  # fmt: skip


def scenario_usdc_paid_exactly() -> dict[str, object]:
    """A 1 USDC invoice paid exactly: attributed from the Transfer log, paid, swept to the destination."""
    invoice = create_token_invoice("1", "USDC", note="live e2e: USDC paid exactly")
    payment_hash = send_token(payer, "USDC", to=invoice["deposit_address"], base_units=to_token_units("1"))
    invoice = wait_for_invoice(invoice["id"], description="paid and swept", condition=is_paid_and_swept)
    check([payment["tx_hash"] for payment in invoice["payments"]] == [payment_hash], f"payments {invoice['payments']}")
    check(invoice["amount_received"] == "1" and [sweep["amount"] for sweep in invoice["sweeps"]] == ["1"],
          f"received {invoice['amount_received']}, sweeps {invoice['sweeps']}")  # fmt: skip
    return {"invoice": invoice["id"], "payment": payment_hash, "sweeps": invoice["sweeps"]}


def scenario_usdt_underpaid_then_topped_up() -> dict[str, object]:
    """A 1 USDT invoice paid 0.4 (underpaid, left unswept), then topped up with 0.6: paid, swept once."""
    invoice = create_token_invoice("1", "USDT", note="live e2e: USDT underpaid then topped up")
    first_hash = send_token(payer, "USDT", to=invoice["deposit_address"], base_units=to_token_units("0.4"))
    invoice = wait_for_invoice(
        invoice["id"], description="underpaid",
        condition=lambda current: current["status"] == "underpaid" and current["amount_received"] == "0.4",
    )  # fmt: skip
    check(invoice["amount_remaining"] == "0.6", f"remaining {invoice['amount_remaining']}")
    check(not invoice["sweeps"], f"an underpaid deposit was swept: {invoice['sweeps']}")
    top_up_hash = send_token(payer, "USDT", to=invoice["deposit_address"], base_units=to_token_units("0.6"))
    invoice = wait_for_invoice(invoice["id"], description="paid and swept", condition=is_paid_and_swept)
    check([payment["tx_hash"] for payment in invoice["payments"]] == [first_hash, top_up_hash],
          f"payments {invoice['payments']}")  # fmt: skip
    check([sweep["amount"] for sweep in invoice["sweeps"]] == ["1"], f"sweeps {invoice['sweeps']}")
    return {"invoice": invoice["id"], "payments": [first_hash, top_up_hash], "sweeps": invoice["sweeps"]}


def scenario_usdc_overpaid_refunded_by_itself() -> dict[str, object]:
    """A 0.5 USDC invoice paid with 2: the 1.5 excess (over the 1 USD minimum refund) goes back to the payer by itself
    once final, from the operator's USDC float; the destination gets all 2."""
    excess_units = to_token_units("1.5")
    if read_token_balance("USDC", operator.address) < excess_units:
        raise ScenarioSkippedError("the USDC float cannot pay a 1.5 USDC refund")
    invoice = create_token_invoice("0.5", "USDC", note="live e2e: USDC overpaid, refunded by itself", refundable=True)
    payer_balance_before = read_token_balance("USDC", payer.address)
    payment_hash = send_token(payer, "USDC", to=invoice["deposit_address"], base_units=to_token_units("2"))
    invoice = wait_for_invoice(
        invoice["id"], description="overpaid, swept and refunded",
        condition=lambda current: current["status"] == "overpaid" and is_swept_final(current)
        and bool(current["refunds"]) and current["refunds"][-1]["status"] == "final",
    )  # fmt: skip
    refunded_units = wait_for_balance_change(
        lambda: read_token_balance("USDC", payer.address),
        before=payer_balance_before - to_token_units("2"), expected_change=excess_units,
    )  # fmt: skip
    check(refunded_units == excess_units, f"the payer got {refunded_units} base units back, expected 1.5 USDC")
    check([(refund["amount"], refund["recipient"]) for refund in invoice["refunds"]] == [("1.5", payer.address)],
          f"refunds {invoice['refunds']}")  # fmt: skip
    check([sweep["amount"] for sweep in invoice["sweeps"]] == ["2"], f"sweeps {invoice['sweeps']}")
    return {"invoice": invoice["id"], "payment": payment_hash, "refunds": invoice["refunds"],
            "sweeps": invoice["sweeps"]}  # fmt: skip


WATCHER_CRASH_NOTE = "live e2e: watcher killed mid-sweep"


def scenario_sweep_survives_a_watcher_crash() -> dict[str, object]:
    """Run beside e2e/kill_watcher_mid_sweep.sh, which SIGKILLs the watcher once this invoice's sweep attempt is
    stored: the restarted watcher finishes that sweep at its nonce, once."""
    invoice = create_pol_invoice("0.6", note=WATCHER_CRASH_NOTE)
    send_native(payer, to=invoice["deposit_address"], value_wei=to_wei("0.6"))
    invoice = wait_for_invoice(invoice["id"], description="paid and swept after the crash", condition=is_paid_and_swept,
                               timeout_seconds=600)  # fmt: skip
    attempts = query_database(
        "SELECT o.nonce, a.transaction_hash, a.receipt_succeeded, a.last_broadcast_at IS NOT NULL FROM outgoing_attempts a "
        "JOIN outgoing_operations o "
        "ON o.id = a.operation_id WHERE o.invoice_id = %s ORDER BY a.id", (invoice["id"],))  # fmt: skip
    check(len(invoice["sweeps"]) == 1, f"sweeps {invoice['sweeps']}")
    check(len({attempt[0] for attempt in attempts}) == 1, f"attempts at more than one nonce: {attempts}")
    return {"invoice": invoice["id"], "attempts": [list(attempt) for attempt in attempts]}


def scenario_bad_input_never_errors_500() -> dict[str, object]:
    """Malformed and hostile bodies get a 4xx, never a 5xx."""
    base = {
        "amount": "1",
        "currency": "POL",
        "chain": CHAIN_KEY,
        "assets": ["POL"],
        "destination_address": operator.address,
    }
    cases = {
        "zero": {**base, "amount": "0"},
        "negative": {**base, "amount": "-1"},
        "exponent": {**base, "amount": "1e3"},
        "beyond_decimals": {**base, "amount": "1.0000000000000000001"},
        "huge_crypto": {**base, "amount": "9" * 39},
        "huge_fiat": {**base, "currency": "USD", "amount": "9" * 30},
        "fiat_three_decimals": {**base, "currency": "USD", "amount": "1.005"},
        "unknown_asset": {**base, "assets": ["DOGE"], "currency": "DOGE"},
        "unknown_chain": {**base, "chain": "ethereum"},
        "asset_currency_mismatch": {**base, "currency": "USDC"},
        "accuracy_too_high": {**base, "accuracy_percent": "5.01"},
        "lifetime_too_short": {**base, "lifetime_seconds": 299},
        "destination_not_an_address": {**base, "destination_address": "0x123"},
        "destination_zero_address": {**base, "destination_address": "0x" + "0" * 40},
        "note_too_long": {**base, "note": "x" * 501},
        "below_minimum_sweep": {**base, "amount": "0.4"},
        "fiat_below_one_dollar": {**base, "currency": "USD", "amount": "0.99"},
        "webhook_url_is_unknown": {**base, "webhook_url": "https://example.com/hook"},
    }
    statuses = {}
    for name, request_body in cases.items():
        reply = http_client.post(f"{API_URL}/invoices", headers=account_headers, json=request_body)
        statuses[name] = reply.status_code
        if reply.status_code == 201:
            http_client.post(f"{API_URL}/invoices/{reply.json()['id']}/cancel", headers=account_headers)
    public_cases = {
        "refund_bad_nonce": (
            "/public/invoices/inv_doesnotexist0000000000/refund-requests",
            {"nonce": "z", "signature": "0x"},
        ),
        "refund_unknown_invoice": ("/public/invoices/inv_doesnotexist0000000000/refund-offer", {}),
        "asset_unknown_invoice": ("/public/invoices/inv_doesnotexist0000000000/asset", {"asset": "POL"}),
        "public_without_receiver": ("/public/invoices", {"amount_usd": "5"}),
        "public_below_one_dollar": ("/public/invoices", {"amount_usd": "0.5", "destination_address": operator.address}),
    }
    for name, (route, request_body) in public_cases.items():
        statuses[name] = http_client.post(f"{API_URL}{route}", json=request_body).status_code
        time.sleep(6.5)
    statuses["no_session"] = http_client.post(f"{API_URL}/invoices", json=base).status_code
    statuses["bearer_key_only"] = http_client.post(f"{API_URL}/invoices", json=base,
                                                   headers={"Authorization": "Bearer pay_wrong"}).status_code  # fmt: skip
    server_errors = {name: status for name, status in statuses.items() if status >= 500}
    accepted = {name: status for name, status in statuses.items() if status < 400}
    check(not server_errors and not accepted, f"server errors {server_errors}, accepted {accepted}")
    return {"statuses": statuses}


SCENARIOS: dict[str, tuple[Callable[[], dict[str, object]], Decimal]] = {
    # name: (scenario, POL the payer spends on it, gas included)
    "speed_up": (scenario_speed_up, Decimal("0.65")),
    "cancel_then_pay": (scenario_cancel_then_pay, Decimal("0.65")),
    "late_payment_swept": (scenario_late_payment_swept, Decimal("1.25")),
    "dust_on_a_closed_deposit_is_not_swept": (scenario_dust_on_a_closed_deposit_is_not_swept, Decimal("0.65")),
    "overpaid_refund_attempt_abandoned": (scenario_overpaid_refund_attempt_abandoned, Decimal("11.15")),
    "two_senders_split_one_invoice": (scenario_two_senders_split_one_invoice, Decimal("0.7")),
    "one_contract_call_pays_two_invoices": (scenario_one_contract_call_pays_two_invoices, Decimal("1.3")),
    "accuracy_boundary_then_accept_partial": (scenario_accuracy_boundary_then_accept_partial, Decimal("1.25")),
    "cancel_while_a_payment_is_in_flight": (scenario_cancel_while_a_payment_is_in_flight, Decimal("0.65")),
    "bad_input_never_errors_500": (scenario_bad_input_never_errors_500, Decimal(0)),
    "sweep_survives_a_watcher_crash": (scenario_sweep_survives_a_watcher_crash, Decimal("0.65")),
    "three_inputs_from_one_address": (scenario_three_inputs_from_one_address, Decimal("0.8")),
    "payment_with_too_little_gas_is_not_credited": (
        scenario_payment_with_too_little_gas_is_not_credited,
        Decimal("1.25"),
    ),
    "usdc_paid_exactly": (scenario_usdc_paid_exactly, Decimal("0.2")),
    "usdt_underpaid_then_topped_up": (scenario_usdt_underpaid_then_topped_up, Decimal("0.3")),
    "usdc_overpaid_refunded_by_itself": (scenario_usdc_overpaid_refunded_by_itself, Decimal("0.2")),
}
# The scenarios whose invoices the float refunds on: made with the operator account's token, left out without it.
REFUND_PATH_SCENARIO_NAMES = (
    "overpaid_refund_attempt_abandoned",
    "three_inputs_from_one_address",
    "usdc_overpaid_refunded_by_itself",
)
# Tokens the payer gets from the operator up front (the operator key is the watcher's signer: no mid-run sends).
TOKEN_FUNDING: dict[str, dict[str, str]] = {
    "usdc_paid_exactly": {"USDC": "1"},
    "usdt_underpaid_then_topped_up": {"USDT": "1"},
    "usdc_overpaid_refunded_by_itself": {"USDC": "2"},
}
# Tokens the operator's float pays out itself during a scenario (on top of TOKEN_FUNDING), checked before the run.
TOKEN_FLOAT_PAYOUTS: dict[str, dict[str, str]] = {
    "usdc_overpaid_refunded_by_itself": {"USDC": "1.5"},
}


def return_leftovers() -> None:
    """Tokens, then POL, from both derived payers back to the operator; each endpoint's answer is logged. Balances
    are read at the newest block this run's own receipts reached: right after a payment, a lagging endpoint's `latest`
    still showed the paid tokens (the return reverted) and the POL before the payment's gas (every endpoint refused
    the return), 2026-10-15. An endpoint without that block errors, so `call_rpc` asks the next."""
    balance_block_tag = hex(newest_receipt_block) if newest_receipt_block else "latest"
    for account in [second_payer, payer]:
        for symbol in TOKEN_ADDRESSES:
            token_units = read_token_balance(symbol, account.address, block_tag=balance_block_tag)
            if token_units and read_native_balance(account.address):
                token_hash = send_token(account, symbol, to=operator.address, base_units=token_units)
                write_log_line("leftover_returned", account=account.address, token=symbol,
                               amount=Decimal(token_units).scaleb(-TOKEN_DECIMALS), transaction=token_hash)  # fmt: skip
        _, maximum_fee, tip = read_fees()
        # 0.01 POL kept back: a lagging read can still show the balance before the token returns' gas. The key is
        # derived, so the next run returns it.
        leftover_wei = (read_native_balance(account.address, block_tag=hex(newest_receipt_block) if newest_receipt_block else "latest")
                        - NATIVE_TRANSFER_GAS * maximum_fee - to_wei("0.01"))  # fmt: skip
        if leftover_wei <= 0:
            continue
        raw_transaction, leftover_hash = sign_transfer(
            account, nonce=next_nonce(account.address), to=operator.address, value_wei=leftover_wei,
            maximum_fee=maximum_fee, tip=tip, gas=NATIVE_TRANSFER_GAS,
        )  # fmt: skip
        answers = broadcast_to_every_rpc(raw_transaction)
        write_log_line("leftover_broadcast", account=account.address, transaction=leftover_hash, answers=answers)
        wait_for_receipt(leftover_hash)
        write_log_line("leftover_returned", account=account.address, pol=leftover_wei / WEI_PER_POL,
                       transaction=leftover_hash)  # fmt: skip


def delete_live_account() -> None:
    """Delete the account the wrapper signed up (its passkey and sessions cascade; its invoices keep no account)."""
    if live_account_username is None:
        return
    deleted = query_database("DELETE FROM accounts WHERE username = %s RETURNING id", (live_account_username,))
    write_log_line("live_account_deleted", count=len(deleted))


# ---- Paying an existing invoice by hand (2026-10-15) ----

# Open to a payment that counts: a paid, expired or cancelled invoice would only take a late payment.
PAYABLE_INVOICE_STATUSES = ("awaiting_asset", "awaiting_payment", "confirming", "underpaid")
NATIVE_SYMBOL = "POL"
# POL for the payer's gas beyond what it sends: a token transfer, the leftovers' return, and a margin (as the token
# scenarios budget it).
PAY_INVOICE_GAS_FUNDING_POL = Decimal("0.3")
# What the operator keeps after funding the payer, as the scenario runs keep it.
OPERATOR_POL_RESERVE = Decimal("0.5")


def read_public_invoice(invoice_id: str) -> dict:
    reply = http_client.get(f"{API_URL}/public/invoices/{invoice_id}")
    check(reply.status_code == 200, f"read invoice: {reply.status_code} {reply.text}")
    return reply.json()


def choose_invoice_coin(invoice: dict, *, symbol: str) -> dict:
    """The invoice with ``symbol`` on this chain as its coin: picked through ``POST /public/invoices/{id}/asset`` as
    the payer page does while the invoice waits for a pick; refused when it already asks another coin."""
    if invoice["status"] == "awaiting_asset":
        offered_pairs = {(option["chain"]["key"], option["asset"]["symbol"]) for option in invoice["payment_options"]}
        check((CHAIN_KEY, symbol) in offered_pairs, f"{invoice['id']} does not offer {symbol} on {CHAIN_KEY}: "
                                                    f"{sorted(offered_pairs)}")  # fmt: skip
        reply = http_client.post(f"{API_URL}/public/invoices/{invoice['id']}/asset",
                                 json={"asset": symbol, "chain": CHAIN_KEY})  # fmt: skip
        check(reply.status_code == 200, f"pick {symbol}: {reply.status_code} {reply.text}")
        invoice = reply.json()
    asked_pair = (invoice["chain"]["key"], (invoice["asset"] or {}).get("symbol"))
    check(asked_pair == (CHAIN_KEY, symbol), f"{invoice['id']} asks {asked_pair[1]} on {asked_pair[0]}, not {symbol}")
    check(bool(invoice["deposit_address"]), f"{invoice['id']} has no deposit address")
    return invoice


def to_base_units_exactly(amount_text: str, *, decimals: int) -> int:
    """Refuses an amount the coin cannot hold exactly, so nothing is rounded silently."""
    base_units = Decimal(amount_text).scaleb(decimals)
    check(base_units > 0 and base_units == base_units.to_integral_value(),
          f"{amount_text} is not a positive amount with at most {decimals} decimals")  # fmt: skip
    return int(base_units)


def pay_invoice(invoice_id: str, *, amount_text: str, symbol: str) -> dict[str, object]:
    """Pays ``amount_text`` of ``symbol`` to the invoice's deposit from the derived payer, funded from the float for
    this payment and its gas. Waits only until the payment is mined: the watcher credits it once final."""
    check(symbol == NATIVE_SYMBOL or symbol in TOKEN_ADDRESSES,
          f"{symbol}: pay_invoice pays {NATIVE_SYMBOL} or {', '.join(TOKEN_ADDRESSES)}")  # fmt: skip
    invoice = read_public_invoice(invoice_id)
    check(invoice["status"] in PAYABLE_INVOICE_STATUSES, f"{invoice_id} is {invoice['status']}: not open to a payment")
    invoice = choose_invoice_coin(invoice, symbol=symbol)
    asset = invoice["asset"]
    check((asset["token_address"] or "").lower() == TOKEN_ADDRESSES.get(symbol, "").lower(),
          f"{symbol}'s token address on the invoice is {asset['token_address']}, not the one this script sends")  # fmt: skip
    base_units = to_base_units_exactly(amount_text, decimals=int(asset["decimals"]))
    deposit_address = invoice["deposit_address"]
    payer_pol_funding = PAY_INVOICE_GAS_FUNDING_POL + (Decimal(amount_text) if symbol == NATIVE_SYMBOL else 0)
    operator_pol = Decimal(read_native_balance(operator.address)) / WEI_PER_POL
    check(operator_pol >= payer_pol_funding + OPERATOR_POL_RESERVE,
          f"the float holds {operator_pol} POL; the payment needs {payer_pol_funding} plus a "
          f"{OPERATOR_POL_RESERVE} reserve")  # fmt: skip
    if symbol != NATIVE_SYMBOL:
        operator_token_units = read_token_balance(symbol, operator.address)
        check(operator_token_units >= base_units, f"the float holds "
              f"{Decimal(operator_token_units).scaleb(-TOKEN_DECIMALS)} {symbol}, short of {amount_text}")  # fmt: skip
    funding_hash = send_from_operator(to=payer.address, value_wei=to_wei(str(payer_pol_funding)))
    write_log_line("payer_funded", payer=payer.address, pol=payer_pol_funding, transaction=funding_hash)
    if symbol == NATIVE_SYMBOL:
        payment_hash = send_native(payer, to=deposit_address, value_wei=base_units)
    else:
        token_hash = send_from_operator(
            to=TOKEN_ADDRESSES[symbol], value_wei=0,
            call_data=TRANSFER_SELECTOR + encode(["address", "uint256"], [payer.address, base_units]),
        )  # fmt: skip
        write_log_line("payer_funded", payer=payer.address, token=symbol, amount=amount_text, transaction=token_hash)
        payment_hash = send_token(payer, symbol, to=deposit_address, base_units=base_units)
    write_log_line("invoice_paid", invoice=invoice_id, symbol=symbol, amount=amount_text, transaction=payment_hash)
    return {"invoice": invoice_id, "symbol": symbol, "amount": amount_text, "transaction": payment_hash,
            "deposit_address": deposit_address}  # fmt: skip


def run_pay_invoice(arguments: list[str]) -> int:
    """``pay_invoice <invoice id> <amount> [symbol]``. The operator nonce rules hold as for the scenarios: crashed
    runs' nonces are filled first and every operator send takes its nonce from the allocator. No in-flight check: the
    watcher's operations take their nonces from the same allocator, and this command measures no run cost."""
    if len(arguments) not in {2, 3}:
        print("usage: pay_invoice <invoice id> <amount> [symbol, default USDC]", file=sys.stderr)  # noqa: T201
        return 2
    invoice_id, amount_text, *symbol_argument = arguments
    if not re.fullmatch(r"\d+(\.\d+)?", amount_text):
        print(f"{amount_text}: the amount is a plain decimal, e.g. 0.50", file=sys.stderr)  # noqa: T201
        return 2
    refusal = prepare_operator_nonces()
    if refusal is not None:
        print(f"{refusal}; not starting", file=sys.stderr)  # noqa: T201
        return 2
    try:
        result = pay_invoice(invoice_id, amount_text=amount_text, symbol=(symbol_argument or ["USDC"])[0].upper())
    except ScenarioFailedError as error:
        write_log_line("pay_invoice", outcome="failed", error=str(error))
        return 1
    finally:
        return_leftovers()
    print(json.dumps(result, default=str), flush=True)  # noqa: T201
    return 0


def main() -> int:
    try:
        return run_chosen_command()
    finally:
        delete_live_account()


def run_chosen_command() -> int:
    if sys.argv[1:2] == ["pay_invoice"]:
        return run_pay_invoice(sys.argv[2:])
    if sys.argv[1:] == ["return_leftovers"]:
        return_leftovers()
        return 0
    if sys.argv[1:2] == ["settle_abandoned_refund"] and len(sys.argv) == 5:
        _, _, invoice_id, operation_id, nonce_taking_hash = sys.argv
        if not operator_account_api_token:
            print(f"{OPERATOR_ACCOUNT_API_TOKEN_MISSING}; not starting", file=sys.stderr)  # noqa: T201
            return 2
        # The abandoned refund's invoice was made with the operator account's token, so it is read with it.
        operator_account_invoice_ids.add(invoice_id)
        try:
            result = settle_abandoned_refund(invoice_id, operation_id=int(operation_id),
                                             nonce_taking_hash=nonce_taking_hash)  # fmt: skip
        finally:
            return_leftovers()
        print(json.dumps(result, indent=2, default=str), flush=True)  # noqa: T201
        return 0
    chosen_names = sys.argv[1:] or list(SCENARIOS)
    unknown_names = [name for name in chosen_names if name not in SCENARIOS]
    if unknown_names:
        print(f"unknown scenarios {unknown_names}; known: {list(SCENARIOS)}", file=sys.stderr)  # noqa: T201
        return 2
    # Left out before the float checks and the payer's funding, so they neither refuse the run nor cost it.
    skipped_results: dict[str, dict[str, object]] = {}
    if not operator_account_api_token:
        skipped_results = {
            name: {"outcome": "skipped", "reason": OPERATOR_ACCOUNT_API_TOKEN_MISSING}
            for name in chosen_names
            if name in REFUND_PATH_SCENARIO_NAMES
        }
        chosen_names = [name for name in chosen_names if name not in skipped_results]
        for name, skipped_result in skipped_results.items():
            write_log_line(name, phase="end", **skipped_result)
    # First, so a crashed run's taken-but-unsent nonce is filled even while the watcher's operations wait behind it
    # (they would never leave "in flight" otherwise); a nonce an operation holds is skipped.
    refusal = prepare_operator_nonces()
    if refusal is not None:
        print(f"{refusal}; not starting", file=sys.stderr)  # noqa: T201
        return 2
    ((in_flight,),) = query_database("SELECT count(*) FROM outgoing_operations WHERE status = ANY(%s)",
                                     (list(ACTIVE_OPERATION_STATUSES),))  # fmt: skip
    if in_flight:
        print(f"{in_flight} operations in flight; not starting", file=sys.stderr)  # noqa: T201
        return 2
    token_shortfalls = find_token_float_shortfalls(chosen_names)
    if token_shortfalls:
        print(f"the token float is short: {'; '.join(token_shortfalls)}. Leave those scenarios out, or fund the "  # noqa: T201
              "operator by hand; not starting", file=sys.stderr)  # fmt: skip
        return 2
    operator_balance_before = read_native_balance(operator.address)
    # 0.1 for the POL leftover's return, 0.1 more when tokens go back too.
    payer_funding_pol = sum((SCENARIOS[name][1] for name in chosen_names), Decimal("0.1"))
    payer_funding_pol += Decimal("0.1") if any(name in TOKEN_FUNDING for name in chosen_names) else 0
    payer_funding_pol = min(payer_funding_pol, Decimal(operator_balance_before - to_wei("0.5")) / WEI_PER_POL)
    funding_hash = send_from_operator(to=payer.address,
                                      value_wei=to_wei(str(payer_funding_pol.quantize(Decimal("0.0001")))))  # fmt: skip
    write_log_line("payer_funded", payer=payer.address, pol=payer_funding_pol, transaction=funding_hash)
    token_funding: dict[str, Decimal] = {}
    for name in chosen_names:
        for symbol, amount_text in TOKEN_FUNDING.get(name, {}).items():
            token_funding[symbol] = token_funding.get(symbol, Decimal(0)) + Decimal(amount_text)
    for symbol, amount in token_funding.items():
        token_hash = send_from_operator(
            to=TOKEN_ADDRESSES[symbol], value_wei=0,
            call_data=TRANSFER_SELECTOR + encode(["address", "uint256"], [payer.address, to_token_units(str(amount))]),
        )  # fmt: skip
        write_log_line("payer_funded", payer=payer.address, token=symbol, amount=amount, transaction=token_hash)
    results: dict[str, dict[str, object]] = dict(skipped_results)
    try:
        for name in chosen_names:
            scenario, _ = SCENARIOS[name]
            write_log_line(name, phase="start")
            try:
                results[name] = {"outcome": "passed", **scenario()}
            except ScenarioSkippedError as skipped:
                results[name] = {"outcome": "skipped", "reason": str(skipped)}
            # Broad on purpose: one scenario's crash (an HTTP error, a KeyError) must not skip the rest.
            except Exception as error:  # noqa: BLE001
                results[name] = {"outcome": "failed", "error": f"{type(error).__name__}: {error}"}
            write_log_line(name, phase="end", **results[name])
    finally:
        return_leftovers()
    # A scenario can end before another invoice's sweep lands; the cost counts once nothing is in flight.
    deadline = time.monotonic() + 600
    while query_database("SELECT count(*) FROM outgoing_operations WHERE status = ANY(%s)",
                         (list(ACTIVE_OPERATION_STATUSES),))[0][0] and time.monotonic() < deadline:  # fmt: skip
        time.sleep(5)
    operator_balance_after = read_native_balance(operator.address)
    summary = {"results": {name: result["outcome"] for name, result in results.items()},
               "operator_pol_before": operator_balance_before / WEI_PER_POL,
               "operator_pol_after": operator_balance_after / WEI_PER_POL,
               "run_cost_pol": (operator_balance_before - operator_balance_after) / WEI_PER_POL}  # fmt: skip
    print(json.dumps(summary, indent=2), flush=True)  # noqa: T201
    return 0 if all(result["outcome"] != "failed" for result in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
