"""Real Arbitrum One pay-through, invoked by hand through run_arbitrum_live_flow.sh.

Pays native ETH, USDT0 through a quantity link, and a USDC overpayment whose excess is refunded. Float refunds are only
for an operator account's own invoices, so the overpaid one is made with that account's API token
(PAY_LIVE_OPERATOR_API_TOKEN, read by the wrapper from the file PAY_LIVE_OPERATOR_API_TOKEN_FILE names; never printed);
with none set the overpayment is skipped and the run says so. Every payer key is derived from the operator key so
another run recovers leftovers after a crash. Operator sends allocate their nonce in Postgres before signing, exactly as
the watcher does. No key, cookie or request body is logged.
"""

import argparse
from decimal import Decimal
import json
import os
import sys
import time

from eth_abi import encode
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils import keccak
import httpx
import psycopg

# This standalone end-to-end runner follows the existing live/fork runners' positional command helpers.
# ruff: noqa: INP001, PLR0917

CHAIN_KEY = "arbitrum"
CHAIN_ID = 42161
RPC_URLS = ["https://arb1.arbitrum.io/rpc", "https://arbitrum-one-rpc.publicnode.com", "https://arbitrum.drpc.org"]
API_URL = "http://api:8000/api/v1"
TOKEN_ADDRESSES = {
    "USDC": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    "USDT": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
}
TRANSFER_SELECTOR = bytes.fromhex("a9059cbb")
BALANCE_SELECTOR = bytes.fromhex("70a08231")
RECEIPT_TIMEOUT_SECONDS = 120
PAY_THROUGH_TIMEOUT_SECONDS = 3600
MAXIMUM_TRANSACTION_FEE_WEI = 10**15

operator_account = Account.from_key(os.environ["PAY_OPERATOR_PRIVATE_KEY_ARBITRUM"])
payer_account = Account.from_key(keccak(bytes(operator_account.key) + b"pay-7331 arbitrum live e2e payer"))
database_url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
account_headers = {
    "Cookie": f"__Host-pay_account_session={os.environ['PAY_LIVE_ACCOUNT_SESSION_TOKEN']}",
    "Origin": os.environ["PAY_PUBLIC_BASE_URL"].rstrip("/"),
}
# An API token of one of the operator's accounts (PAY_OPERATOR_ACCOUNT_IDS), for the invoice the float refunds on.
operator_account_api_token = os.environ.get("PAY_LIVE_OPERATOR_API_TOKEN", "")
operator_account_headers = {"Authorization": f"Bearer {operator_account_api_token}"}
http_client = httpx.Client(timeout=20, trust_env=False)


def record_live_step(step: str, **details: object) -> None:
    print(json.dumps({"step": step, **details}, default=str), flush=True)  # noqa: T201


def require_live_condition(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def call_arbitrum_rpc(method: str, parameters: list[object]) -> object:
    for rpc_url in RPC_URLS:
        try:
            response = http_client.post(
                rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": parameters}
            )
            response.raise_for_status()
            result = response.json()
            if "result" in result:
                return result["result"]
        except (httpx.HTTPError, ValueError):
            continue
    raise RuntimeError(f"Arbitrum RPC did not answer {method}")


def read_database_rows(statement: str, parameters: tuple[object, ...] = ()) -> list[tuple]:
    with psycopg.connect(database_url, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(statement, parameters)
        return cursor.fetchall()


def read_native_balance(address: str) -> int:
    return int(str(call_arbitrum_rpc("eth_getBalance", [address, "latest"])), 16)


def read_token_balance(symbol: str, address: str) -> int:
    result = call_arbitrum_rpc(
        "eth_call",
        [
            {"to": TOKEN_ADDRESSES[symbol], "data": "0x" + (BALANCE_SELECTOR + encode(["address"], [address])).hex()},
            "latest",
        ],
    )
    return int(str(result), 16)


def wait_for_mined_receipt(transaction_hash: str) -> dict:
    deadline = time.monotonic() + RECEIPT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        receipt = call_arbitrum_rpc("eth_getTransactionReceipt", [transaction_hash])
        if receipt is not None:
            require_live_condition(int(receipt["status"], 16) == 1, f"transaction reverted: {transaction_hash}")
            return receipt
        time.sleep(2)
    raise RuntimeError(f"transaction was not mined within {RECEIPT_TIMEOUT_SECONDS}s: {transaction_hash}")


def prepare_transaction(account: LocalAccount, *, destination: str, amount_wei: int, call_data: bytes = b"") -> dict:
    latest = call_arbitrum_rpc("eth_getBlockByNumber", ["latest", False])
    priority_fee = int(str(call_arbitrum_rpc("eth_maxPriorityFeePerGas", [])), 16)
    maximum_fee = 2 * int(latest["baseFeePerGas"], 16) + priority_fee
    estimate_fields = {
        "from": account.address,
        "to": destination,
        "value": hex(amount_wei),
        "data": "0x" + call_data.hex(),
    }
    # Nitro's estimate includes the L1 posting component. A fixed 21,000 gas limit is not a native-transfer estimate.
    gas_limit = int(str(call_arbitrum_rpc("eth_estimateGas", [estimate_fields])), 16) * 5 // 4 + 10_000
    require_live_condition(
        gas_limit * maximum_fee <= MAXIMUM_TRANSACTION_FEE_WEI, "live run fee estimate exceeds its limit"
    )
    return {
        "type": 2,
        "chainId": CHAIN_ID,
        "to": destination,
        "value": amount_wei,
        "data": call_data,
        "gas": gas_limit,
        "maxFeePerGas": maximum_fee,
        "maxPriorityFeePerGas": priority_fee,
    }


def broadcast_signed_transaction(account: LocalAccount, transaction: dict) -> str:
    signed = account.sign_transaction(transaction)
    expected_hash = "0x" + signed.hash.hex().removeprefix("0x")
    raw_transaction = "0x" + signed.raw_transaction.hex().removeprefix("0x")
    # Rebroadcast the same signed transaction only; an unclear response never allocates a replacement nonce.
    for rpc_url in RPC_URLS:
        try:
            response = http_client.post(
                rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction", "params": [raw_transaction]},
            ).json()
            if response.get("result") == expected_hash:
                wait_for_mined_receipt(expected_hash)
                return expected_hash
        except (httpx.HTTPError, ValueError):
            continue
    wait_for_mined_receipt(expected_hash)
    return expected_hash


def send_payer_transaction(*, destination: str, amount_wei: int, call_data: bytes = b"") -> str:
    transaction = prepare_transaction(
        payer_account, destination=destination, amount_wei=amount_wei, call_data=call_data
    )
    transaction["nonce"] = int(
        str(call_arbitrum_rpc("eth_getTransactionCount", [payer_account.address, "pending"])), 16
    )
    return broadcast_signed_transaction(payer_account, transaction)


def send_operator_transaction(*, destination: str, amount_wei: int, call_data: bytes = b"") -> str:
    transaction = prepare_transaction(
        operator_account, destination=destination, amount_wei=amount_wei, call_data=call_data
    )
    rows = read_database_rows(
        "UPDATE operator_nonces SET next_nonce = next_nonce + 1 WHERE chain_key = %s AND operator_address = %s "
        "RETURNING next_nonce - 1",
        (CHAIN_KEY, operator_account.address),
    )
    require_live_condition(len(rows) == 1, "watcher has not adopted Arbitrum operator nonce")
    transaction["nonce"] = int(rows[0][0])
    # No fallthrough retry at a fresh nonce: on a broadcast outage this exact nonce must be recovered next run.
    return broadcast_signed_transaction(operator_account, transaction)


def recover_unused_operator_nonces() -> None:
    latest_nonce = int(str(call_arbitrum_rpc("eth_getTransactionCount", [operator_account.address, "latest"])), 16)
    pending_nonce = int(str(call_arbitrum_rpc("eth_getTransactionCount", [operator_account.address, "pending"])), 16)
    rows = read_database_rows(
        "SELECT next_nonce FROM operator_nonces WHERE chain_key=%s AND operator_address=%s",
        (CHAIN_KEY, operator_account.address),
    )
    require_live_condition(len(rows) == 1, "Arbitrum operator nonce is not initialized")
    allocated_nonce = int(rows[0][0])
    require_live_condition(latest_nonce <= allocated_nonce, "operator nonce advanced outside its allocator")
    require_live_condition(
        latest_nonce == pending_nonce, "operator has a pending transaction; finish it before this run"
    )
    for nonce in range(latest_nonce, allocated_nonce):
        assigned = read_database_rows(
            "SELECT id FROM outgoing_operations WHERE chain_key=%s AND operator_address=%s AND nonce=%s",
            (CHAIN_KEY, operator_account.address, nonce),
        )
        require_live_condition(not assigned, "allocated nonce belongs to a gateway operation; refusing to replace it")
        transaction = prepare_transaction(operator_account, destination=operator_account.address, amount_wei=0)
        transaction["nonce"] = nonce
        record_live_step(
            "unused_operator_nonce_filled", transaction=broadcast_signed_transaction(operator_account, transaction)
        )


def request_gateway(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    expected_status: int = 200,
    headers: dict[str, str] | None = None,
) -> dict:
    for attempt in range(6):
        try:
            response = http_client.request(method, API_URL + path, headers=headers or account_headers, json=body)
        except httpx.TransportError:
            if method != "GET" or attempt == 5:
                raise
            time.sleep(2)
            continue
        if method == "GET" and response.status_code >= 500 and attempt < 5:
            time.sleep(2)
            continue
        break
    require_live_condition(
        response.status_code == expected_status, f"gateway {method} {path} returned {response.status_code}"
    )
    return response.json()


def create_chosen_invoice(*, symbol: str, destination: str, note: str, by_operator_account: bool = False) -> dict:
    """A 1 USD invoice on both networks, picked on Arbitrum. ``by_operator_account``: made with the operator account's
    API token (``POST /invoices``) rather than signed out, so the float refunds on it."""
    offered_coins = {
        "chains": ["polygon", CHAIN_KEY],
        "assets": ["USDC", "ETH"] if symbol == "ETH" else [symbol],
        "assets_by_chain": {"polygon": ["USDC"] if symbol == "ETH" else [symbol], CHAIN_KEY: [symbol]},
        "destination_address": destination,
        "note": note,
    }
    if by_operator_account:
        invoice = request_gateway(
            "POST",
            "/invoices",
            expected_status=201,
            body={"amount": "1", "currency": "USD", **offered_coins},
            headers=operator_account_headers,
        )
    else:
        invoice = request_gateway(
            "POST", "/public/invoices", expected_status=201, body={"amount_usd": "1", **offered_coins}
        )
    require_live_condition(
        invoice["status"] == "awaiting_asset" and invoice["deposit_address"] is None,
        "mixed invoice exposed a deposit before the network choice",
    )
    options = invoice["payment_options"]
    require_live_condition(
        any(option["chain"]["key"] == CHAIN_KEY and option["asset"]["symbol"] == symbol for option in options),
        "invoice offers no requested Arbitrum coin",
    )
    return request_gateway(
        "POST", f"/public/invoices/{invoice['id']}/asset", body={"chain": CHAIN_KEY, "asset": symbol}
    )


def return_payer_leftovers() -> None:
    for symbol, token_address in TOKEN_ADDRESSES.items():
        token_balance = read_token_balance(symbol, payer_account.address)
        if token_balance:
            transaction_hash = send_payer_transaction(
                destination=token_address,
                amount_wei=0,
                call_data=TRANSFER_SELECTOR + encode(["address", "uint256"], [operator_account.address, token_balance]),
            )
            record_live_step(
                "payer_token_returned", symbol=symbol, base_units=token_balance, transaction=transaction_hash
            )
    balance = read_native_balance(payer_account.address)
    if balance:
        transaction = prepare_transaction(payer_account, destination=operator_account.address, amount_wei=0)
        remaining = balance - transaction["gas"] * transaction["maxFeePerGas"]
        if remaining > 0:
            transaction["value"] = remaining
            transaction["nonce"] = int(
                str(call_arbitrum_rpc("eth_getTransactionCount", [payer_account.address, "pending"])), 16
            )
            record_live_step(
                "payer_native_returned", transaction=broadcast_signed_transaction(payer_account, transaction)
            )


def wait_for_paid_and_settled_invoices(invoices: list[dict]) -> None:
    deadline = time.monotonic() + PAY_THROUGH_TIMEOUT_SECONDS
    last_statuses: dict[str, str] = {}
    while time.monotonic() < deadline:
        complete = True
        for invoice in invoices:
            view = request_gateway("GET", f"/public/invoices/{invoice['id']}")
            status = view["status"]
            if last_statuses.get(invoice["id"]) != status:
                record_live_step("invoice_status", invoice=invoice["id"], status=status)
                last_statuses[invoice["id"]] = status
            require_live_condition(status not in {"expired", "cancelled"}, f"on-time live payment closed as {status}")
            if status not in {"paid", "overpaid"}:
                complete = False
                continue
            if not any(sweep["status"] == "final" for sweep in view["sweeps"]):
                complete = False
            if invoice.get("expect_refund") and not any(refund["status"] == "final" for refund in view["refunds"]):
                complete = False
        if complete:
            return
        time.sleep(10)
    raise RuntimeError("Arbitrum live payments or outgoing operations did not finalize within one hour")


def verify_final_invoices(invoices: list[dict]) -> None:
    for invoice in invoices:
        final_view = request_gateway("GET", f"/public/invoices/{invoice['id']}")
        require_live_condition(final_view["chain"]["key"] == CHAIN_KEY, "payment settled on the wrong network")
        if invoice.get("expect_refund"):
            refunds = [refund for refund in final_view["refunds"] if refund["status"] == "final"]
            require_live_condition(
                any(
                    refund["recipient"] == payer_account.address and Decimal(refund["amount"]) >= Decimal("1.49")
                    for refund in refunds
                ),
                "overpay refund amount or recipient differs",
            )
        record_live_step(
            "scenario_passed", invoice=invoice["id"], chain=final_view["chain"]["key"], status=final_view["status"]
        )


def run_live_pay_through() -> int:
    require_live_condition(
        int(str(call_arbitrum_rpc("eth_chainId", [])), 16) == CHAIN_ID, "RPC answered the wrong chain"
    )
    recover_unused_operator_nonces()
    active = read_database_rows(
        "SELECT count(*) FROM outgoing_operations WHERE chain_key=%s AND operator_address=%s AND status = ANY(%s)",
        (CHAIN_KEY, operator_account.address, ["pending", "waiting_for_float", "broadcast", "mined"]),
    )
    require_live_condition(active[0][0] == 0, "operator has active Arbitrum operations")
    require_live_condition(
        read_native_balance(operator_account.address) > 2 * 10**15, "ETH float too small for live run"
    )
    require_live_condition(
        read_token_balance("USDC", operator_account.address) >= 5_500_000, "USDC float too small for live run"
    )
    require_live_condition(
        read_token_balance("USDT", operator_account.address) >= 2_000_000, "USDT float too small for live run"
    )
    record_live_step(
        "payer_native_funded",
        transaction=send_operator_transaction(destination=payer_account.address, amount_wei=10**15),
    )
    return_payer_leftovers()
    record_live_step(
        "payer_native_funded",
        transaction=send_operator_transaction(destination=payer_account.address, amount_wei=10**15),
    )
    for symbol, base_units in [("USDC", 2_500_000), ("USDT", 2_000_000)]:
        record_live_step(
            "payer_token_funded",
            symbol=symbol,
            transaction=send_operator_transaction(
                destination=TOKEN_ADDRESSES[symbol],
                amount_wei=0,
                call_data=TRANSFER_SELECTOR + encode(["address", "uint256"], [payer_account.address, base_units]),
            ),
        )
    invoices: list[dict] = []
    try:
        native_invoice = create_chosen_invoice(
            symbol="ETH", destination=operator_account.address, note="live e2e: Arbitrum native"
        )
        native_amount = int(Decimal(native_invoice["amount_due"]).scaleb(18))
        native_hash = send_payer_transaction(destination=native_invoice["deposit_address"], amount_wei=native_amount)
        invoices.append(native_invoice)
        record_live_step("native_payment_sent", invoice=native_invoice["id"], transaction=native_hash)

        link = request_gateway(
            "POST",
            "/payment-links",
            expected_status=201,
            body={
                "amount": "1",
                "currency": "USD",
                "chains": ["polygon", CHAIN_KEY],
                "assets": ["USDT"],
                "quantity": 1,
                "destination_address": operator_account.address,
                "note": "live e2e: Arbitrum quantity link",
            },
        )
        token_invoice = request_gateway(
            "POST",
            f"/public/payment-links/{link['id']}/invoices",
            expected_status=201,
            body={"chain": CHAIN_KEY, "asset": "USDT"},
        )
        token_amount = int(Decimal(token_invoice["amount_due"]).scaleb(6))
        token_hash = send_payer_transaction(
            destination=TOKEN_ADDRESSES["USDT"],
            amount_wei=0,
            call_data=TRANSFER_SELECTOR
            + encode(["address", "uint256"], [token_invoice["deposit_address"], token_amount]),
        )
        invoices.append(token_invoice)
        refusal = http_client.post(
            API_URL + f"/public/payment-links/{link['id']}/invoices",
            headers=account_headers,
            json={"chain": "polygon", "asset": "USDT"},
        )
        require_live_condition(refusal.status_code == 409, "quantity link allowed a second payer")
        record_live_step("quantity_payment_sent", invoice=token_invoice["id"], transaction=token_hash, sold_out=True)

        if operator_account_api_token:
            excess_invoice = create_chosen_invoice(
                symbol="USDC",
                destination=os.environ["PAY_DEFAULT_DESTINATION_ADDRESS"],
                note="live e2e: Arbitrum overpay",
                by_operator_account=True,
            )
            excess_hash = send_payer_transaction(
                destination=TOKEN_ADDRESSES["USDC"],
                amount_wei=0,
                call_data=TRANSFER_SELECTOR
                + encode(["address", "uint256"], [excess_invoice["deposit_address"], 2_500_000]),
            )
            excess_invoice["expect_refund"] = True
            invoices.append(excess_invoice)
            record_live_step("overpayment_sent", invoice=excess_invoice["id"], transaction=excess_hash)
        else:
            record_live_step(
                "overpayment_skipped",
                reason="PAY_LIVE_OPERATOR_API_TOKEN is not set: only an operator account's invoices get float refunds",
            )
        wait_for_paid_and_settled_invoices(invoices)
        verify_final_invoices(invoices)
        return 0
    finally:
        return_payer_leftovers()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume-invoices", nargs="+", metavar="INVOICE_ID")
    parser.add_argument("--refund-invoice", metavar="INVOICE_ID")
    options = parser.parse_args()
    try:
        if options.resume_invoices:
            invoices = [
                {"id": invoice_id, "expect_refund": invoice_id == options.refund_invoice}
                for invoice_id in options.resume_invoices
            ]
            require_live_condition(
                options.refund_invoice in options.resume_invoices, "resume must include the overpaid invoice"
            )
            wait_for_paid_and_settled_invoices(invoices)
            verify_final_invoices(invoices)
            return_payer_leftovers()
        else:
            sys.exit(run_live_pay_through())
    finally:
        # Deleting a test account removes its credentials and sessions; invoice rows and chain evidence stay.
        if os.environ.get("PAY_LIVE_ACCOUNT_USERNAME"):
            read_database_rows(
                "DELETE FROM accounts WHERE username=%s RETURNING id", (os.environ["PAY_LIVE_ACCOUNT_USERNAME"],)
            )
        http_client.close()
