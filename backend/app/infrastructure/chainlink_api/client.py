"""Chainlink ``AggregatorV3Interface`` reads: ``latestRoundData`` and ``decimals`` of every requested feed in one
Multicall3 ``aggregate3`` at ``latest``.

The price endpoints may be another chain's (Amoy reads Polygon mainnet feeds over its own ``EvmRpcClient``, PLAN.md
section 1); Polygon's reads share the watcher's client (``lifespan/composition.py``). A read fails over inside itself,
since it is a request's, not a watcher tick's: it walks the endpoints from the client's current one in a local order
and moves that shared cursor only to an endpoint that answered fresh, so concurrent reads never push each other past
a good endpoint (2026-10-14).
It fails over on a stale answer too (2026-10-13): ``latest`` is the endpoint's own head ("the latest proposed block",
https://ethereum.org/en/developers/docs/apis/json-rpc/#default-block), so an endpoint lagging minutes behind the tip
serves the round of minutes ago, as its ``updatedAt`` ("Timestamp of when the round was updated",
https://docs.chain.link/data-feeds/api-reference) shows, while another endpoint may be current.

A rollup's read carries its L2 Sequencer Uptime Feed in the same Multicall3 (2026-10-14, PLAN.md section 15): answer 1
means the sequencer is down, ``startedAt`` 0 means the feed is not initialized (Arbitrum only), and for
``SEQUENCER_GRACE_PERIOD_SECONDS`` after ``startedAt`` the prices are refused too, as Chainlink's example does
(``GRACE_PERIOD_TIME = 3600``, ``if (timeSinceUp <= GRACE_PERIOD_TIME) revert``,
https://docs.chain.link/data-feeds/l2-sequencer-feeds). An observed refusal stops the read immediately, since a lagging
endpoint may still show the old up status and must not override a down or recovering sequencer.
"""

from collections.abc import Sequence
from decimal import Decimal
import time

from eth_abi import decode
from eth_abi.exceptions import DecodingError
from eth_utils import function_signature_to_4byte_selector

from app.infrastructure.chainlink_api.errors import ChainlinkFeedError, ChainlinkSequencerUnavailableError
from app.infrastructure.chainlink_api.log_events import ChainlinkEndpointStale, ChainlinkSequencerRefused
from app.infrastructure.chainlink_api.schemas import FeedReading
from app.infrastructure.evm_rpc_api.client import EvmRpcClient
from app.infrastructure.evm_rpc_api.errors import EvmExecutionRevertedError, EvmRpcUnavailableError
from app.infrastructure.evm_rpc_api.schemas import Multicall3Call, Multicall3Result
from app.shared import logging as log

logger = log.get_logger(__name__)

LATEST_ROUND_DATA_SELECTOR = function_signature_to_4byte_selector("latestRoundData()")
DECIMALS_SELECTOR = function_signature_to_4byte_selector("decimals()")
# One hour after the sequencer is back: Chainlink's own example value.
SEQUENCER_GRACE_PERIOD_SECONDS = 3600
SEQUENCER_ANSWER_UP = 0


def find_sequencer_refusal(sequencer_result: Multicall3Result, *, now_timestamp: int) -> str | None:
    """Why the sequencer feed's ``latestRoundData`` refuses prices; None when the sequencer is up and has been for more
    than the grace period. A feed that reverts or answers nonsense refuses too: nothing says the sequencer is up."""
    if not sequencer_result.succeeded:
        return "the sequencer uptime feed reverted"
    try:
        _, answer, started_at, _, _ = decode(
            ["uint80", "int256", "uint256", "uint256", "uint80"], sequencer_result.return_data
        )
    except DecodingError:
        return "the sequencer uptime feed returned undecodable data"
    if answer != SEQUENCER_ANSWER_UP:
        return "the sequencer is reported down"
    if started_at == 0:
        return "the sequencer uptime feed is not initialized"
    if now_timestamp - started_at <= SEQUENCER_GRACE_PERIOD_SECONDS:
        return f"the sequencer came back {max(0, now_timestamp - started_at)} s ago, inside its grace period"
    return None


def parse_feed_reading(
    feed_address: str, *, round_result: Multicall3Result, decimals_result: Multicall3Result
) -> FeedReading:
    if not (round_result.succeeded and decimals_result.succeeded):
        raise ChainlinkFeedError(f"feed {feed_address} reverted")
    try:
        _, answer, _, updated_at, _ = decode(
            ["uint80", "int256", "uint256", "uint256", "uint80"], round_result.return_data
        )
        (decimals,) = decode(["uint8"], decimals_result.return_data)
    except Exception as exc:  # eth_abi raises several decoding errors
        raise ChainlinkFeedError(f"feed {feed_address} returned undecodable data") from exc
    if answer <= 0:
        raise ChainlinkFeedError(f"feed {feed_address} answered {answer}")
    return FeedReading(
        feed_address=feed_address, price=Decimal(answer).scaleb(-decimals), updated_at_timestamp=updated_at
    )


def measure_most_overdue_seconds(
    readings: Sequence[FeedReading], *, maximum_age_seconds_by_feed: Sequence[int], now_timestamp: int
) -> tuple[int, int]:
    """``(seconds past its own limit, age)`` of the reading furthest past its feed's maximum age; the first is 0 or
    less when every reading is within its limit."""
    overdue_and_age = [
        (
            now_timestamp - reading.updated_at_timestamp - maximum_age_seconds,
            now_timestamp - reading.updated_at_timestamp,
        )
        for reading, maximum_age_seconds in zip(readings, maximum_age_seconds_by_feed, strict=True)
    ]
    return max(overdue_and_age)


class ChainlinkApiClient:
    def __init__(self, *, rpc_client: EvmRpcClient) -> None:
        self.rpc_client = rpc_client

    async def read_feeds(
        self,
        feed_addresses: Sequence[str],
        *,
        maximum_age_seconds_by_feed: Sequence[int] | None = None,
        sequencer_uptime_feed: str | None = None,
    ) -> list[FeedReading]:
        """The latest answer of each feed, in order. Tries every endpoint once before giving up. With
        ``maximum_age_seconds_by_feed`` (one limit per feed, in order, since a slow feed such as XMR/USD has a 24 h
        heartbeat beside 27 s ones), an endpoint with any answer older than its feed's limit is failed over like a
        down one; when no endpoint is fresh, the readings least past their limits come back and the caller judges
        them. With ``sequencer_uptime_feed`` its status is read in the same call. An observed refusal raises
        ``ChainlinkSequencerUnavailableError`` immediately: a lagging endpoint must not override a down or
        recovering sequencer. Transport failures still fail over."""
        calls = [
            call
            for feed_address in feed_addresses
            for call in (
                Multicall3Call(target=feed_address, call_data=LATEST_ROUND_DATA_SELECTOR),
                Multicall3Call(target=feed_address, call_data=DECIMALS_SELECTOR),
            )
        ]
        if sequencer_uptime_feed is not None:
            calls.append(Multicall3Call(target=sequencer_uptime_feed, call_data=LATEST_ROUND_DATA_SELECTOR))
        least_overdue_readings: list[FeedReading] | None = None
        least_overdue_seconds = 0
        endpoints = self.rpc_client.endpoints
        start_index = self.rpc_client.current_endpoint_index
        # One clock for the whole read, so the least-overdue pick compares endpoints alike.
        read_started_at = int(time.time())
        for offset in range(len(endpoints)):
            endpoint_index = (start_index + offset) % len(endpoints)
            try:
                results = await endpoints[endpoint_index].aggregate3(calls, block_hash=None)
            except (EvmRpcUnavailableError, EvmExecutionRevertedError):
                continue
            if sequencer_uptime_feed is not None:
                endpoint_refusal = find_sequencer_refusal(results[-1], now_timestamp=read_started_at)
                if endpoint_refusal is not None:
                    log.emit(
                        logger, event=ChainlinkSequencerRefused(endpoint_index=endpoint_index, reason=endpoint_refusal)
                    )
                    raise ChainlinkSequencerUnavailableError(endpoint_refusal)
            readings = [
                parse_feed_reading(
                    feed_address, round_result=results[2 * index], decimals_result=results[2 * index + 1]
                )
                for index, feed_address in enumerate(feed_addresses)
            ]
            if maximum_age_seconds_by_feed is None or not readings:
                self.rpc_client.current_endpoint_index = endpoint_index
                return readings
            overdue_seconds, age_seconds = measure_most_overdue_seconds(
                readings, maximum_age_seconds_by_feed=maximum_age_seconds_by_feed, now_timestamp=read_started_at
            )
            if overdue_seconds <= 0:
                self.rpc_client.current_endpoint_index = endpoint_index
                return readings
            log.emit(logger, event=ChainlinkEndpointStale(endpoint_index=endpoint_index, age_seconds=age_seconds))
            if least_overdue_readings is None or overdue_seconds < least_overdue_seconds:
                least_overdue_readings = readings
                least_overdue_seconds = overdue_seconds
        if least_overdue_readings is not None:
            return least_overdue_readings
        raise ChainlinkFeedError("no price endpoint answered")
