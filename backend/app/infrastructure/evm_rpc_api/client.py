"""EVM JSON-RPC over web3.py 8 (``AsyncWeb3`` on ``AsyncHTTPProvider``), knowing nothing about invoices.

``EvmRpcClient`` holds a chain's endpoint list in failover order. A watcher tick takes one ``EvmRpcEndpoint`` with
``endpoint_for_tick`` and makes every call of the tick through it; a tick that fails calls ``fail_over`` so the next
tick uses the next endpoint (PLAN.md section 5: failover switches between ticks, never inside one).

Requests go through the provider as raw JSON-RPC (single or batched) and are parsed here into ``schemas.py``
models, so web3's per-method result formatters (and their POA quirks on Polygon) stay out of the way. ``eth_getLogs``
alone goes over httpx, so a refusal sent with a non-2xx status keeps its message and is told apart: a range or
result-count refusal (the watcher narrows the range) from a history refusal (the tick fails over). Reads pinned
to a block use EIP-1898's ``{"blockHash": ..., "requireCanonical": true}`` (https://eips.ethereum.org/EIPS/eip-1898).

An endpoint that rate limits us (HTTP 429, or a rate-limit JSON-RPC error, ``RATE_LIMIT_ERROR_CODES``) is set aside
for its ``Retry-After`` or ``DEFAULT_RATE_LIMIT_PAUSE_SECONDS`` (2026-10-14): until then a call to it raises
``EvmRpcRateLimitedError`` without sending anything, and ``fail_over`` passes over it while another endpoint is free.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from itertools import batched
import time
from urllib.parse import urlsplit

import aiohttp
from eth_abi import decode, encode
from eth_utils import function_signature_to_4byte_selector, to_checksum_address
import httpx
from pydantic import JsonValue
from web3 import AsyncHTTPProvider, AsyncWeb3
from web3.types import RPCEndpoint

from app.infrastructure.evm_rpc_api.errors import (
    EvmChainMismatchError,
    EvmExecutionRevertedError,
    EvmInsufficientFundsError,
    EvmLogBeyondHeadRefusedError,
    EvmLogHistoryRefusedError,
    EvmLogRangeRefusedError,
    EvmRpcRateLimitedError,
    EvmRpcUnavailableError,
)
from app.infrastructure.evm_rpc_api.log_events import EvmRpcEndpointRateLimited
from app.infrastructure.evm_rpc_api.schemas import (
    BlockHead,
    BlockHeads,
    BlockTransactions,
    LogEntry,
    MinedTransaction,
    Multicall3Call,
    Multicall3Result,
    NativeTransfer,
    PendingTransaction,
    TransactionReceipt,
)
from app.shared import logging as log

logger = log.get_logger(__name__)

# Deployed at this address on Polygon and Amoy (checked 2026-10-09, PLAN.md section 1); a fork of Polygon has it.
MULTICALL3_ADDRESS = to_checksum_address("0xcA11bde05977b3631167028862bE2a173976CA11")
# Calls per aggregate3 (PLAN.md section 5). 500 cold balance reads estimated 4.3M gas and 112 KB of call data on
# Polygon (2026-10-09): well under geth's default 50M eth_call gas cap and 5 MiB request body limit.
MULTICALL3_CHUNK_SIZE = 500
# Mints and burns name it in a Transfer's `from` or `to` (EIP-20, https://eips.ethereum.org/EIPS/eip-20); no key signs
# for it and tokens refuse a transfer to it.
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
# EIP-7702 delegation designator: 0xef0100 || address, 23 bytes.
EIP7702_DESIGNATOR_PREFIX = "ef0100"
EIP7702_DESIGNATOR_HEX_LENGTH = 46
# Requests per JSON-RPC batch on an endpoint whose host `JSON_RPC_BATCH_SIZE_BY_HOST` does not name (publicnode,
# Tenderly and Pocket answered 50 with no error entry, 2026-10-13); public endpoints refuse very large batches.
DEFAULT_JSON_RPC_BATCH_SIZE = 50
# Smaller batches where a host caps them, matched on the URL's host or any subdomain of it, so a keyed URL of the same
# provider added later gets the same cap (2026-10-13):
# - dRPC's free plan: "Batch requests are limited to 3 items to prevent bypassing rate limits"
#   (https://drpc.org/docs/howitworks/ratelimiting); a batch of 50 got HTTP 500 with code 31 in every entry, "Batch of
#   more than 3 requests are not allowed on free plan" (10 passed once; the documented cap is kept).
# - QuickNode's public endpoint: a batch of 50 got 30 entries of -32007 "20/second request limit reached", one of 20
#   passed; its limits are not documented in the sources checked, so 10 leaves room for a second batch in a second.
JSON_RPC_BATCH_SIZE_BY_HOST = {"drpc.org": 3, "quiknode.pro": 10}
AGGREGATE3_SELECTOR = function_signature_to_4byte_selector("aggregate3((address,bool,bytes)[])")
GET_ETH_BALANCE_SELECTOR = function_signature_to_4byte_selector("getEthBalance(address)")
GET_BLOCK_NUMBER_SELECTOR = function_signature_to_4byte_selector("getBlockNumber()")
BALANCE_OF_SELECTOR = function_signature_to_4byte_selector("balanceOf(address)")
# Node answers to a send that mean "this exact transaction, or one at its nonce, is already in": not a failure, the
# receipt reconciliation decides what mined.
ALREADY_SUBMITTED_MARKERS = ("already known", "known transaction", "nonce too low", "already imported")
REVERT_MARKERS = ("revert", "execution reverted", "invalid opcode")
INSUFFICIENT_FUNDS_MARKER = "insufficient funds"
# eth_getLogs refusals, matched in the lowercased JSON-RPC error message (measured 2026-10-12 on the endpoints
# chains.toml lists; the execution-apis spec leaves limits and their errors to each node). History first:
# publicnode's archive refusal names no range, and a narrower range never passes it.
LOG_HISTORY_REFUSAL_MARKERS = (
    "archive requests require",  # publicnode: logs past its history, 98 blocks to over 100,000 back (HTTP 403)
    "historical state is not available",  # Pocket's portal, from a pruned supplier
    # geth's PrunedHistoryError, code 4444 (go-ethereum master, core/history/historymode.go, read 2026-10-12)
    "pruned history unavailable",
)
LOG_RANGE_REFUSAL_MARKERS = (
    # dRPC's free plan, "ranges over 10000 blocks are not supported", for any range over 101 blocks (HTTP 400)
    "ranges over",
    "max allowed range",  # tenderly.rpc.polygon.community over 10,000 blocks (HTTP 413)
    "is limited to a",  # QuickNode's public endpoint, "eth_getLogs is limited to a 10,000 range" (HTTP 413)
    "exceeds the limit",  # "block span 49999 of range [...] exceeds the limit 10000" (seen from rpc.sentio.xyz)
    "range is over limit",  # "Requested range is over limit of 100" (seen from Tatum)
    "exceed maximum block range",  # publicnode, -32701 "exceed maximum block range: 10000" (2026-10-14)
    # Bor's own cap, "block range exceeds configured limit" (maticnetwork/bor develop, eth/filters/api.go, read
    # 2026-10-12), and other nodes' "block range too large". Not Bor's "invalid block range params": that is a
    # `toBlock` past the answering node's head (LOG_BEYOND_HEAD_REFUSAL_MARKERS), which no narrower range fixes.
    "block range exceeds",
    "block range too",
    "block range is too",
    "too many results",  # "too many results: the result count exceeds the limit"
    # Tenderly, "Query returned more than 3000 results" (https://docs.tenderly.co/node-rpc/overview)
    "returned more than",
    "more than 10000 results",  # dRPC caps a reply at 10,000 logs (https://drpc.org/docs/howitworks/ratelimiting)
    # dRPC's free nodes stop a request at 2 s (same page) and Pocket answered "relay timeout exceeded" (HTTP 504) on
    # 3 of 40 deep 2,000-block reads: a narrower range is quicker
    "timeout",
    "timed out",
)
# eth_getLogs refusals of a `toBlock` past the answering node's head. The spec makes that -32602 and forbids clamping
# or a partial result (https://github.com/ethereum/execution-apis, src/eth/filter.yaml, eth_getLogs, read
# 2026-10-13); the endpoints answer it in their own words, measured 2026-10-13 with `toBlock` 5 blocks past
# `eth_blockNumber`. A load-balanced endpoint answers the heads and the log read from different backends, so a read up
# to the `latest` it just reported can get one (about a third of ticks on publicnode, 2026-10-13).
LOG_BEYOND_HEAD_REFUSAL_MARKERS = (
    # Bor, -32000, for `lastBlock > head` (0xPolygon/bor develop, eth/filters/filter.go `updateChainView`): publicnode
    # and QuickNode's public endpoint
    "invalid block range params",
    # geth's errBlockRangeIntoFuture, -32602 (ethereum/go-ethereum master, eth/filters/api.go)
    "beyond current head block",
    # Tenderly's community endpoint and Pocket's portal, -32603, "block not found for eth_getLogs, requested toBlock N
    # is not available on the upstream node (latestBlock: ..., finalizedBlock: ...)"
    "is not available on the upstream node",
)
# A 413 with no JSON-RPC body is a range refusal too (Tenderly and QuickNode answer 413 to one).
HTTP_STATUS_PAYLOAD_TOO_LARGE = 413
# web3's provider uses aiohttp's 30 s default; the log read keeps the same bound.
LOG_READ_TIMEOUT_SECONDS = 30.0
# Rate limiting (2026-10-14). HTTP 429 Too Many Requests (https://www.rfc-editor.org/rfc/rfc6585#section-4) may carry
# Retry-After as seconds or an HTTP-date (https://www.rfc-editor.org/rfc/rfc9110#section-10.2.3), and so may a 503.
# JSON-RPC codes: QuickNode's -32007 per-second, -32008 per-minute, -32011 method and -32029 rate limits
# (https://www.quicknode.com/docs/ethereum/error-references, which also answers HTTP 429 and asks callers to honour
# Retry-After). Not -32005: EIP-1474 calls it only "Limit exceeded", and QuickNode's references list it for a range,
# result count, batch or timeout too large, which the watcher narrows (LOG_RANGE_REFUSAL_MARKERS). dRPC's, publicnode's, Tenderly's and
# Pocket's rate-limit replies are not documented in the sources checked; a 429 from any of them is caught by its status.
HTTP_STATUS_TOO_MANY_REQUESTS = 429
HTTP_STATUS_SERVICE_UNAVAILABLE = 503
RATE_LIMIT_ERROR_CODES = frozenset({-32007, -32008, -32011, -32029})
# A JSON-RPC rate-limit reply carries no Retry-After (web3 hands back only the body): a per-minute limit waits a
# minute; the others the default. Inside an HTTP 429 the body is lost (aiohttp raises on the status), so the default.
RATE_LIMIT_PAUSE_SECONDS_BY_ERROR_CODE = {-32008: 60}
# Without Retry-After: long enough for a per-second limit to clear and the tick to move on, short enough that a
# per-minute limit costs at most a few failed-over ticks.
DEFAULT_RATE_LIMIT_PAUSE_SECONDS = 10
# A Retry-After further out than this is capped: the endpoint is tried again then and can set itself aside anew.
MAXIMUM_RATE_LIMIT_PAUSE_SECONDS = 600


def encode_native_balance_call(holder_address: str) -> Multicall3Call:
    """Multicall3's own ``getEthBalance(holder)``, so native balances ride in the same aggregate3."""
    return Multicall3Call(
        target=MULTICALL3_ADDRESS, call_data=GET_ETH_BALANCE_SELECTOR + encode(["address"], [holder_address])
    )


def encode_block_number_call() -> Multicall3Call:
    """Multicall3's own ``getBlockNumber()``: the block an ``aggregate3`` at ``latest`` was read at."""
    return Multicall3Call(target=MULTICALL3_ADDRESS, call_data=GET_BLOCK_NUMBER_SELECTOR)


def encode_token_balance_call(token_address: str, *, holder_address: str) -> Multicall3Call:
    return Multicall3Call(target=token_address, call_data=BALANCE_OF_SELECTOR + encode(["address"], [holder_address]))


def is_contract_code(code_hex: str) -> bool:
    """Non-empty code other than an EIP-7702 delegation designator, ``0xef0100`` then a 20-byte address
    (https://eips.ethereum.org/EIPS/eip-7702): that marks an externally owned account, not a contract."""
    code = code_hex.lower().removeprefix("0x")
    if code in {"", "0"}:
        return False
    return not (len(code) == EIP7702_DESIGNATOR_HEX_LENGTH and code.startswith(EIP7702_DESIGNATOR_PREFIX))


def rate_limit_pause_seconds_of(
    retry_after: str | None, *, now: datetime, default_pause_seconds: int | None = None
) -> int:
    """How long to set an endpoint aside: its ``Retry-After`` (delay seconds or an HTTP-date) within
    ``MAXIMUM_RATE_LIMIT_PAUSE_SECONDS``, else ``default_pause_seconds`` or ``DEFAULT_RATE_LIMIT_PAUSE_SECONDS``."""
    if retry_after:
        text = retry_after.strip()
        seconds: float | None = None
        # delay-seconds = 1*DIGIT, ASCII digits only (RFC 9110); nine digits are already years, past the cap.
        if text.isascii() and text.isdigit() and len(text) <= 9:
            seconds = int(text)
        else:
            try:
                seconds = (parsedate_to_datetime(text) - now).total_seconds()
            except (TypeError, ValueError):
                seconds = None
        if seconds is not None:
            return max(1, min(int(seconds), MAXIMUM_RATE_LIMIT_PAUSE_SECONDS))
    return default_pause_seconds or DEFAULT_RATE_LIMIT_PAUSE_SECONDS


def retry_after_of(headers: Mapping[str, str] | None) -> str | None:
    if headers is None:
        return None
    return next((value for name, value in headers.items() if name.lower() == "retry-after"), None)


def json_rpc_batch_size_for(rpc_url: str) -> int:
    """The batch size of ``rpc_url``: ``JSON_RPC_BATCH_SIZE_BY_HOST`` for its host or a subdomain of one, else
    ``DEFAULT_JSON_RPC_BATCH_SIZE``."""
    host = (urlsplit(rpc_url).hostname or "").lower()
    for capped_host, batch_size in JSON_RPC_BATCH_SIZE_BY_HOST.items():
        if host == capped_host or host.endswith("." + capped_host):
            return batch_size
    return DEFAULT_JSON_RPC_BATCH_SIZE


def decode_uint256(return_data: bytes) -> int:
    if len(return_data) < 32:
        raise EvmRpcUnavailableError("a uint256 return is shorter than 32 bytes")
    return int.from_bytes(return_data[:32], "big")


def hex_to_int(value: JsonValue) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise EvmRpcUnavailableError(f"expected a hex quantity, got {type(value).__name__}")
    return int(value, 16)


def text_of(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise EvmRpcUnavailableError(f"expected a string, got {type(value).__name__}")
    return value


def object_of(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise EvmRpcUnavailableError(f"expected an object, got {type(value).__name__}")
    return value


def list_of(value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, list):
        raise EvmRpcUnavailableError(f"expected a list, got {type(value).__name__}")
    return value


def parse_block_head(value: JsonValue) -> BlockHead:
    block = object_of(value)
    base_fee = block.get("baseFeePerGas")
    return BlockHead(
        number=hex_to_int(block.get("number")),
        hash=text_of(block.get("hash")),
        timestamp=hex_to_int(block.get("timestamp")),
        base_fee_per_gas=hex_to_int(base_fee) if base_fee is not None else 0,
    )


def parse_log_entry(value: JsonValue) -> LogEntry:
    entry = object_of(value)
    return LogEntry(
        address=to_checksum_address(text_of(entry.get("address"))),
        topics=[text_of(topic).lower() for topic in list_of(entry.get("topics"))],
        data=text_of(entry.get("data")),
        block_number=hex_to_int(entry.get("blockNumber")),
        block_hash=text_of(entry.get("blockHash")),
        transaction_hash=text_of(entry.get("transactionHash")),
        transaction_index=hex_to_int(entry.get("transactionIndex")),
        log_index=hex_to_int(entry.get("logIndex")),
    )


def parse_receipt(value: JsonValue) -> TransactionReceipt:
    receipt = object_of(value)
    return TransactionReceipt(
        transaction_hash=text_of(receipt.get("transactionHash")),
        succeeded=hex_to_int(receipt.get("status")) == 1,
        block_number=hex_to_int(receipt.get("blockNumber")),
        block_hash=text_of(receipt.get("blockHash")),
        transaction_index=hex_to_int(receipt.get("transactionIndex")),
        gas_used=hex_to_int(receipt.get("gasUsed")),
        effective_gas_price=hex_to_int(receipt.get("effectiveGasPrice")),
        logs=[parse_log_entry(entry) for entry in list_of(receipt.get("logs"))],
    )


def parse_block_transactions(value: JsonValue) -> BlockTransactions:
    head = parse_block_head(value)
    transfers: list[NativeTransfer] = []
    for transaction_value in list_of(object_of(value).get("transactions")):
        transaction = object_of(transaction_value)
        amount = hex_to_int(transaction.get("value"))
        if amount == 0:
            continue
        recipient = transaction.get("to")
        transfers.append(
            NativeTransfer(
                transaction_hash=text_of(transaction.get("hash")),
                sender=to_checksum_address(text_of(transaction.get("from"))),
                recipient=to_checksum_address(text_of(recipient)) if recipient is not None else None,
                value=amount,
                block_number=head.number,
                block_hash=head.hash,
                transaction_index=hex_to_int(transaction.get("transactionIndex")),
            )
        )
    return BlockTransactions(head=head, transfers=transfers)


def parse_mined_transaction(value: JsonValue) -> MinedTransaction:
    """``eth_getTransactionByHash`` of a mined transaction; its value may be 0."""
    transaction = object_of(value)
    recipient = transaction.get("to")
    return MinedTransaction(
        transaction_hash=text_of(transaction.get("hash")),
        sender=to_checksum_address(text_of(transaction.get("from"))),
        recipient=to_checksum_address(text_of(recipient)) if recipient is not None else None,
        value=hex_to_int(transaction.get("value")),
        block_number=hex_to_int(transaction.get("blockNumber")),
        block_hash=text_of(transaction.get("blockHash")),
        transaction_index=hex_to_int(transaction.get("transactionIndex")),
        nonce=hex_to_int(transaction.get("nonce")),
    )


def result_of(method: str, *, reply: object) -> JsonValue:
    """The ``result`` of one JSON-RPC reply; an ``error`` member raises, a revert as ``EvmExecutionRevertedError``."""
    if not isinstance(reply, dict):
        raise EvmRpcUnavailableError(f"{method}: reply is not an object")
    error = reply.get("error")
    if error is not None:
        error_object: dict[str, object] = error if isinstance(error, dict) else {}
        error_message = str(error_object.get("message", "")).lower()
        error_code = error_object.get("code")
        if error_code in RATE_LIMIT_ERROR_CODES:
            raise EvmRpcRateLimitedError(
                f"{method}: JSON-RPC error {error_code}: {error_message[:200]}",
                pause_seconds=RATE_LIMIT_PAUSE_SECONDS_BY_ERROR_CODE.get(
                    error_code if isinstance(error_code, int) else 0, DEFAULT_RATE_LIMIT_PAUSE_SECONDS
                ),
            )
        if error_object.get("code") == 3 or any(marker in error_message for marker in REVERT_MARKERS):
            raise EvmExecutionRevertedError(f"{method}: {error_message[:200]}")
        if INSUFFICIENT_FUNDS_MARKER in error_message:
            raise EvmInsufficientFundsError(f"{method}: {error_message[:200]}")
        raise EvmRpcUnavailableError(f"{method}: JSON-RPC error {error_object.get('code')}: {error_message[:200]}")
    result: JsonValue = reply.get("result")
    return result


def raise_log_query_refusal(reply: object, *, http_status_code: int) -> None:
    """Raise when an ``eth_getLogs`` reply is a refusal: ``EvmLogHistoryRefusedError`` for blocks the endpoint does
    not serve, ``EvmLogBeyondHeadRefusedError`` for a ``toBlock`` past the answering node's head,
    ``EvmLogRangeRefusedError`` for a range or result count a narrower range may pass. Any other error raises as
    ``result_of`` does; a plain result returns."""
    error = reply.get("error") if isinstance(reply, dict) else None
    if error is None:
        if http_status_code == HTTP_STATUS_PAYLOAD_TOO_LARGE:
            raise EvmLogRangeRefusedError(f"eth_getLogs: HTTP {http_status_code}")
        if not isinstance(reply, dict):
            raise EvmRpcUnavailableError(f"eth_getLogs: HTTP {http_status_code}, reply is not an object")
        return
    error_object: dict[str, object] = error if isinstance(error, dict) else {}
    error_message = str(error_object.get("message", error)).lower()
    # A rate limit first: its words ("request limit reached") must never read as a range a narrower one may pass.
    if error_object.get("code") in RATE_LIMIT_ERROR_CODES:
        result_of("eth_getLogs", reply=reply)
    if any(marker in error_message for marker in LOG_HISTORY_REFUSAL_MARKERS):
        raise EvmLogHistoryRefusedError(f"eth_getLogs: {error_message[:200]}")
    if any(marker in error_message for marker in LOG_BEYOND_HEAD_REFUSAL_MARKERS):
        raise EvmLogBeyondHeadRefusedError(f"eth_getLogs: {error_message[:200]}")
    if http_status_code == HTTP_STATUS_PAYLOAD_TOO_LARGE or any(
        marker in error_message for marker in LOG_RANGE_REFUSAL_MARKERS
    ):
        raise EvmLogRangeRefusedError(f"eth_getLogs: {error_message[:200]}")
    result_of("eth_getLogs", reply=reply)


def block_reference(block_hash: str | None) -> JsonValue:
    """EIP-1898 block parameter: pinned to ``block_hash`` (canonical only), or ``latest``."""
    if block_hash is None:
        return "latest"
    return {"blockHash": block_hash, "requireCanonical": True}


class EvmRpcEndpoint:
    """One JSON-RPC endpoint. Every method is one request or one batch request."""

    def __init__(self, rpc_url: str) -> None:
        self.rpc_url = rpc_url
        self.json_rpc_batch_size = json_rpc_batch_size_for(rpc_url)
        # No provider retries: a failed tick fails over to the next endpoint instead.
        self.web3 = AsyncWeb3(AsyncHTTPProvider(rpc_url, exception_retry_configuration=None))
        # eth_getLogs only: web3's provider raises on a non-2xx status and drops the body, and dRPC (400), publicnode
        # (403), Tenderly and QuickNode (413) put their log refusals in exactly such a body (measured 2026-10-12).
        self.log_http_client = httpx.AsyncClient(timeout=LOG_READ_TIMEOUT_SECONDS)
        # time.monotonic() before which this endpoint gets no request (module docstring).
        self.rate_limited_until = 0.0

    def is_rate_limited(self) -> bool:
        return time.monotonic() < self.rate_limited_until

    def raise_if_rate_limited(self, method: str) -> None:
        if self.is_rate_limited():
            raise EvmRpcRateLimitedError(f"{method}: endpoint set aside after a rate limit")

    def set_aside_for_rate_limit(self, *, retry_after: str | None, default_pause_seconds: int | None = None) -> None:
        pause_seconds = rate_limit_pause_seconds_of(
            retry_after, now=datetime.now(UTC), default_pause_seconds=default_pause_seconds
        )
        self.rate_limited_until = max(self.rate_limited_until, time.monotonic() + pause_seconds)
        log.emit(
            logger,
            event=EvmRpcEndpointRateLimited(
                endpoint_host=urlsplit(self.rpc_url).hostname or "", pause_seconds=pause_seconds
            ),
        )

    def raise_transport_error(self, description: str, *, exc: Exception) -> None:
        """A transport failure as ``EvmRpcUnavailableError``; a 429, or a 503 with Retry-After, sets the endpoint
        aside first. web3's aiohttp session raises ``ClientResponseError`` on any non-2xx status
        (``raise_for_status=True``), headers included."""
        if isinstance(exc, aiohttp.ClientResponseError):
            retry_after = retry_after_of(exc.headers)
            if exc.status == HTTP_STATUS_TOO_MANY_REQUESTS or (
                exc.status == HTTP_STATUS_SERVICE_UNAVAILABLE and retry_after is not None
            ):
                self.set_aside_for_rate_limit(retry_after=retry_after)
                raise EvmRpcRateLimitedError(f"{description}: HTTP {exc.status}") from exc
        raise EvmRpcUnavailableError(f"{description}: {type(exc).__name__}") from exc

    def result_or_set_aside(self, method: str, *, reply: object) -> JsonValue:
        try:
            return result_of(method, reply=reply)
        except EvmRpcRateLimitedError as exc:
            self.set_aside_for_rate_limit(retry_after=None, default_pause_seconds=exc.pause_seconds)
            raise

    async def call_method(self, method: str, *, parameters: list[JsonValue]) -> JsonValue:
        self.raise_if_rate_limited(method)
        try:
            reply = await self.web3.provider.make_request(RPCEndpoint(method), parameters)
        except Exception as exc:  # aiohttp and web3 raise their own transport errors
            self.raise_transport_error(method, exc=exc)
            raise
        return self.result_or_set_aside(method, reply=reply)

    async def call_method_batch(self, method: str, *, parameter_lists: Sequence[list[JsonValue]]) -> list[JsonValue]:
        """One JSON-RPC batch per ``json_rpc_batch_size`` requests of the same method; results in request order.
        Any entry's error fails the whole call (the tick retries on the next endpoint): a batch answers each entry on
        its own (Tenderly: "If a particular request fails, the batch request still returns a 200 OK HTTP status",
        https://docs.tenderly.co/node-rpc/overview), so a rate-limited entry next to good ones (QuickNode's -32007,
        2026-10-13) is never taken as data."""
        results: list[JsonValue] = []
        for chunk in batched(parameter_lists, self.json_rpc_batch_size, strict=False):
            self.raise_if_rate_limited(f"{method} batch")
            requests = [(RPCEndpoint(method), parameters) for parameters in chunk]
            try:
                replies = await self.web3.provider.make_batch_request(requests)
            except Exception as exc:  # aiohttp and web3 raise their own transport errors
                self.raise_transport_error(f"{method} batch", exc=exc)
                raise
            if not isinstance(replies, list):
                self.result_or_set_aside(method, reply=replies)
                raise EvmRpcUnavailableError(f"{method} batch: reply is not a list")
            if len(replies) != len(requests):
                raise EvmRpcUnavailableError(f"{method} batch: {len(replies)} replies for {len(requests)} requests")
            results.extend(self.result_or_set_aside(method, reply=reply) for reply in replies)
        return results

    async def read_chain_id(self) -> int:
        return hex_to_int(await self.call_method("eth_chainId", parameters=[]))

    async def read_block_heads(self) -> BlockHeads:
        latest, finalized = await self.call_method_batch(
            "eth_getBlockByNumber", parameter_lists=[["latest", False], ["finalized", False]]
        )
        return BlockHeads(latest=parse_block_head(latest), finalized=parse_block_head(finalized))

    async def read_block_heads_by_number(self, block_numbers: Sequence[int]) -> list[BlockHead]:
        replies = await self.call_method_batch(
            "eth_getBlockByNumber", parameter_lists=[[hex(number), False] for number in block_numbers]
        )
        return [parse_block_head(reply) for reply in replies]

    async def read_blocks_with_transactions(self, block_numbers: Sequence[int]) -> list[BlockTransactions]:
        replies = await self.call_method_batch(
            "eth_getBlockByNumber", parameter_lists=[[hex(number), True] for number in block_numbers]
        )
        return [parse_block_transactions(reply) for reply in replies]

    async def call_contract(self, target_address: str, *, call_data: bytes, block_hash: str | None = None) -> bytes:
        result = await self.call_method(
            "eth_call",
            parameters=[{"to": target_address, "data": "0x" + call_data.hex()}, block_reference(block_hash)],
        )
        return bytes.fromhex(text_of(result).removeprefix("0x"))

    async def aggregate3(self, calls: Sequence[Multicall3Call], *, block_hash: str | None) -> list[Multicall3Result]:
        """Multicall3 ``aggregate3`` in chunks of ``MULTICALL3_CHUNK_SIZE``, each pinned to ``block_hash`` (or
        ``latest``). Every call allows failure; a failed call comes back with ``succeeded`` False."""
        results: list[Multicall3Result] = []
        for chunk in batched(calls, MULTICALL3_CHUNK_SIZE, strict=False):
            call_data = AGGREGATE3_SELECTOR + encode(
                ["(address,bool,bytes)[]"], [[(call.target, True, call.call_data) for call in chunk]]
            )
            return_data = await self.call_contract(MULTICALL3_ADDRESS, call_data=call_data, block_hash=block_hash)
            try:
                (decoded,) = decode(["(bool,bytes)[]"], return_data)
            except Exception as exc:  # eth_abi raises several decoding errors
                raise EvmRpcUnavailableError("aggregate3: undecodable return") from exc
            if len(decoded) != len(chunk):
                raise EvmRpcUnavailableError("aggregate3: result count differs from call count")
            results.extend(Multicall3Result(succeeded=succeeded, return_data=data) for succeeded, data in decoded)
        return results

    async def read_logs(
        self, *, addresses: Sequence[str], topics: list[JsonValue], from_block: int, to_block: int
    ) -> list[LogEntry]:
        """``eth_getLogs``. A refusal raises ``EvmLogRangeRefusedError`` (a narrower range may pass) or
        ``EvmLogHistoryRefusedError`` (this endpoint serves no logs that far back); see ``raise_log_query_refusal``."""
        self.raise_if_rate_limited("eth_getLogs")
        log_filter: JsonValue = {
            "address": list(addresses),
            "topics": topics,
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
        }
        try:
            response = await self.log_http_client.post(
                self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs", "params": [log_filter]}
            )
        except httpx.HTTPError as exc:
            raise EvmRpcUnavailableError(f"eth_getLogs: {type(exc).__name__}") from exc
        if response.status_code == HTTP_STATUS_TOO_MANY_REQUESTS or (
            response.status_code == HTTP_STATUS_SERVICE_UNAVAILABLE and "retry-after" in response.headers
        ):
            self.set_aside_for_rate_limit(retry_after=response.headers.get("retry-after"))
            raise EvmRpcRateLimitedError(f"eth_getLogs: HTTP {response.status_code}")
        try:
            reply: object = response.json()
        except ValueError:
            reply = None
        if reply is None and response.status_code != HTTP_STATUS_PAYLOAD_TOO_LARGE:
            raise EvmRpcUnavailableError(f"eth_getLogs: HTTP {response.status_code}, no JSON-RPC reply")
        try:
            raise_log_query_refusal(reply, http_status_code=response.status_code)
        except EvmRpcRateLimitedError as exc:
            self.set_aside_for_rate_limit(
                retry_after=response.headers.get("retry-after"), default_pause_seconds=exc.pause_seconds
            )
            raise
        if not response.is_success:
            raise EvmRpcUnavailableError(f"eth_getLogs: HTTP {response.status_code}")
        return [parse_log_entry(entry) for entry in list_of(self.result_or_set_aside("eth_getLogs", reply=reply))]

    async def read_transaction_receipts(self, transaction_hashes: Sequence[str]) -> list[TransactionReceipt | None]:
        """Receipts in request order; None for a transaction not mined (or not known) at this endpoint."""
        replies = await self.call_method_batch(
            "eth_getTransactionReceipt", parameter_lists=[[transaction_hash] for transaction_hash in transaction_hashes]
        )
        return [parse_receipt(reply) if reply is not None else None for reply in replies]

    async def read_transaction_by_hash(self, transaction_hash: str) -> MinedTransaction | PendingTransaction | None:
        """``eth_getTransactionByHash``: null when the endpoint knows no such transaction, ``blockNumber`` null while
        it is pending (https://ethereum.org/en/developers/docs/apis/json-rpc/#eth_gettransactionbyhash)."""
        reply = await self.call_method("eth_getTransactionByHash", parameters=[transaction_hash])
        if reply is None:
            return None
        if object_of(reply).get("blockNumber") is None:
            return PendingTransaction(transaction_hash=text_of(object_of(reply).get("hash")))
        return parse_mined_transaction(reply)

    async def read_mined_transaction(self, transaction_hash: str) -> MinedTransaction | None:
        """The top-level transaction; None when this endpoint does not know it or it is not mined yet."""
        transaction = await self.read_transaction_by_hash(transaction_hash)
        return transaction if isinstance(transaction, MinedTransaction) else None

    async def read_addresses_with_code(self, addresses: Sequence[str]) -> set[str]:
        """The subset of ``addresses`` that are contracts at ``latest``. An EIP-7702 delegated account is not one:
        its key still signs (``personal_sign`` included). The zero address counts as one without a read: it has no
        code, but it is a mint's ``from``, not a sender anyone can be paid back at, so its money is the owner's to
        settle like a contract's (2026-10-13)."""
        zero_addresses = {address for address in addresses if address.lower() == ZERO_ADDRESS}
        addresses_to_read = [address for address in addresses if address not in zero_addresses]
        if not addresses_to_read:
            return zero_addresses
        replies = await self.call_method_batch(
            "eth_getCode", parameter_lists=[[address, "latest"] for address in addresses_to_read]
        )
        return zero_addresses | {
            address for address, code in zip(addresses_to_read, replies, strict=True) if is_contract_code(text_of(code))
        }

    async def read_transaction_count(self, address: str, *, block_tag: str) -> int:
        return hex_to_int(await self.call_method("eth_getTransactionCount", parameters=[address, block_tag]))

    async def read_transaction_counts(self, addresses: Sequence[str], *, block_tag: str) -> list[int]:
        """Each address's ``eth_getTransactionCount`` at ``block_tag``, in one batch, in request order."""
        replies = await self.call_method_batch(
            "eth_getTransactionCount", parameter_lists=[[address, block_tag] for address in addresses]
        )
        return [hex_to_int(reply) for reply in replies]

    async def read_native_balance(self, address: str) -> int:
        return hex_to_int(await self.call_method("eth_getBalance", parameters=[address, "latest"]))

    async def read_max_priority_fee_per_gas(self) -> int:
        return hex_to_int(await self.call_method("eth_maxPriorityFeePerGas", parameters=[]))

    async def estimate_gas(self, *, sender_address: str, target_address: str, call_data: bytes, value: int) -> int:
        """Raises ``EvmExecutionRevertedError`` when the node says the transaction would revert."""
        return hex_to_int(
            await self.call_method(
                "eth_estimateGas",
                parameters=[
                    {"from": sender_address, "to": target_address, "data": "0x" + call_data.hex(), "value": hex(value)}
                ],
            )
        )

    async def send_raw_transaction(self, raw_transaction: bytes) -> None:
        """Broadcast; an answer meaning the transaction (or its nonce) is already in is not an error."""
        try:
            await self.call_method("eth_sendRawTransaction", parameters=["0x" + raw_transaction.hex()])
        except EvmRpcUnavailableError as exc:
            if any(marker in str(exc).lower() for marker in ALREADY_SUBMITTED_MARKERS):
                return
            raise

    async def close(self) -> None:
        await self.web3.provider.disconnect()
        await self.log_http_client.aclose()


class EvmRpcClient:
    """A chain's endpoints in failover order; the current one serves whole ticks."""

    def __init__(self, *, chain_id: int, rpc_urls: Sequence[str]) -> None:
        if not rpc_urls:
            raise ValueError("an EVM chain needs at least one RPC URL")
        self.chain_id = chain_id
        self.endpoints = [EvmRpcEndpoint(rpc_url) for rpc_url in rpc_urls]
        self.current_endpoint_index = 0

    def endpoint_for_tick(self) -> EvmRpcEndpoint:
        return self.endpoints[self.current_endpoint_index]

    def fail_over(self) -> None:
        """The next endpoint in order that is not set aside for a rate limit. When every other one is: stay, unless
        this one is set aside too, then the one free soonest."""
        endpoint_count = len(self.endpoints)
        for step in range(1, endpoint_count):
            candidate_index = (self.current_endpoint_index + step) % endpoint_count
            if not self.endpoints[candidate_index].is_rate_limited():
                self.current_endpoint_index = candidate_index
                return
        if self.endpoints[self.current_endpoint_index].is_rate_limited():
            self.current_endpoint_index = min(
                range(endpoint_count), key=lambda index: self.endpoints[index].rate_limited_until
            )

    async def call_with_failover[T](self, call: Callable[[EvmRpcEndpoint], Awaitable[T]]) -> T:
        """For request-side reads (not watcher ticks): try each endpoint once, failing over on an unavailable one."""
        for _ in self.endpoints:
            try:
                return await call(self.endpoint_for_tick())
            except EvmRpcUnavailableError:
                self.fail_over()
        raise EvmRpcUnavailableError("no RPC endpoint of the chain answered")

    async def call_on_each_endpoint_until_found[T](
        self, call: Callable[[EvmRpcEndpoint], Awaitable[T | None]]
    ) -> T | None:
        """For lookups one endpoint may not know (a transaction another node has not seen): ask each endpoint in
        turn and return the first answer that is not None. None when every endpoint that answered knew nothing; an
        unavailable endpoint is skipped, and only when none answered at all is that an error."""
        any_endpoint_answered = False
        for endpoint in self.endpoints:
            try:
                found = await call(endpoint)
            except EvmRpcUnavailableError:
                continue
            any_endpoint_answered = True
            if found is not None:
                return found
        if not any_endpoint_answered:
            raise EvmRpcUnavailableError("no RPC endpoint of the chain answered")
        return None

    async def read_highest_transaction_counts(self, addresses: Sequence[str], *, block_tag: str) -> list[int]:
        """Each address's highest ``eth_getTransactionCount`` at ``block_tag`` among the endpoints that answer (one
        batch each), in request order: a lagging node's lower count would look like nonces never used. An
        unavailable endpoint is skipped; only when none answered is that an error."""
        highest_counts: list[int] | None = None
        for endpoint in self.endpoints:
            try:
                counts = await endpoint.read_transaction_counts(addresses, block_tag=block_tag)
            except EvmRpcUnavailableError:
                continue
            highest_counts = (
                counts
                if highest_counts is None
                else [max(highest, count) for highest, count in zip(highest_counts, counts, strict=True)]
            )
        if highest_counts is None:
            raise EvmRpcUnavailableError("no RPC endpoint of the chain answered eth_getTransactionCount")
        return highest_counts

    async def verify_chain_id(self) -> None:
        """Refuse an endpoint that serves another chain; at least one endpoint must answer. An unreachable endpoint
        is skipped (it may come back), and the first one that answers becomes current."""
        first_answering_index: int | None = None
        for endpoint_index, endpoint in enumerate(self.endpoints):
            try:
                served_chain_id = await endpoint.read_chain_id()
            except EvmRpcUnavailableError:
                continue
            if served_chain_id != self.chain_id:
                raise EvmChainMismatchError(
                    f"{endpoint.rpc_url} serves chain {served_chain_id}, configured {self.chain_id}"
                )
            if first_answering_index is None:
                first_answering_index = endpoint_index
        if first_answering_index is None:
            raise EvmRpcUnavailableError("no RPC endpoint of the chain answered eth_chainId")
        self.current_endpoint_index = first_answering_index

    async def close(self) -> None:
        for endpoint in self.endpoints:
            await endpoint.close()
