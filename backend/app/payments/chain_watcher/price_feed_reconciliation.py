"""The price feed check of ``ReconciliationService`` (2026-10-14, split out to keep that module under 800 lines): each
Chainlink feed's ``latestRoundData`` ``updatedAt`` against its own ``price_feed_heartbeat_seconds`` plus a grace minute;
a feed that reverts or answers nonsense; a rollup's sequencer reported down or just back."""

import time

from app.infrastructure.chainlink_api.client import ChainlinkApiClient
from app.infrastructure.chainlink_api.errors import ChainlinkFeedError, ChainlinkSequencerUnavailableError
from app.payments.chain_watcher.types import ReconciliationCheckResult, ReconciliationFinding
from app.payments.chains.types import ChainConfiguration
from app.shared.durations import describe_seconds

# Public endpoints trail the head by a few seconds and the feeds update on their heartbeat or a deviation.
PRICE_FEED_STALE_GRACE_SECONDS = 60


async def check_price_feeds(
    chain: ChainConfiguration, *, chainlink_client: ChainlinkApiClient
) -> ReconciliationCheckResult:
    labels_by_feed: dict[str, list[str]] = {}
    # Each feed's own heartbeat and quote limit (XMR/USD is a 24 h feed); a feed two assets share keeps the larger.
    heartbeat_seconds_by_feed: dict[str, int] = {}
    maximum_price_age_seconds_by_feed: dict[str, int] = {}
    feed_limits = [
        (asset.usd_feed, f"{asset.symbol}/USD", asset.price_feed_heartbeat_seconds, asset.maximum_price_age_seconds)
        for asset in chain.assets
    ] + [(chain.eur_usd_feed, "EUR/USD", chain.price_feed_heartbeat_seconds, chain.maximum_price_age_seconds)]
    for feed_address, pair_label, heartbeat_seconds, maximum_price_age_seconds in feed_limits:
        labels_by_feed.setdefault(feed_address, []).append(pair_label)
        heartbeat_seconds_by_feed[feed_address] = max(heartbeat_seconds, heartbeat_seconds_by_feed.get(feed_address, 0))
        # Two assets on one feed: quotes refuse at each asset's own limit, so the report uses the stricter one.
        maximum_price_age_seconds_by_feed[feed_address] = min(
            maximum_price_age_seconds,
            maximum_price_age_seconds_by_feed.get(feed_address, maximum_price_age_seconds),
        )
    feed_addresses = list(labels_by_feed)
    try:
        readings = await chainlink_client.read_feeds(feed_addresses, sequencer_uptime_feed=chain.sequencer_uptime_feed)
    except ChainlinkSequencerUnavailableError as error:
        return ReconciliationCheckResult(
            check_name="price_feeds",
            summary=f"{len(feed_addresses)} feeds, refused by the sequencer uptime feed",
            findings=[
                ReconciliationFinding(
                    problem_key=f"sequencer_unavailable:{chain.key}",
                    alert_kind="price_feed_unreadable",
                    invoice_id=None,
                    message=f"The {chain.name} sequencer is down or has been back for less than an hour; no price, "
                    "no quote and no automatic refund until it has been up that long.",
                    columns={"chain": chain.key, "reason": str(error)},
                )
            ],
        )
    except ChainlinkFeedError as error:
        return ReconciliationCheckResult(
            check_name="price_feeds",
            summary=f"{len(feed_addresses)} feeds, not readable",
            findings=[
                ReconciliationFinding(
                    problem_key=f"price_feed_unreadable:{chain.key}",
                    alert_kind="price_feed_unreadable",
                    invoice_id=None,
                    message=f"The {chain.name} price feeds cannot be read; no price, no quote and no automatic "
                    "refund until they answer.",
                    columns={"chain": chain.key, "error": str(error)},
                )
            ],
        )
    now_timestamp = time.time()
    findings: list[ReconciliationFinding] = []
    ages = [max(0.0, now_timestamp - reading.updated_at_timestamp) for reading in readings]
    for reading, age_seconds in zip(readings, ages, strict=True):
        heartbeat_seconds = heartbeat_seconds_by_feed[reading.feed_address]
        if age_seconds <= heartbeat_seconds + PRICE_FEED_STALE_GRACE_SECONDS:
            continue
        pair_names = ", ".join(labels_by_feed[reading.feed_address])
        findings.append(
            ReconciliationFinding(
                problem_key=f"price_feed_stale:{chain.key}:{reading.feed_address}",
                alert_kind="price_feed_stale",
                invoice_id=None,
                message=f"The {pair_names} price feed on {chain.name} last updated {describe_seconds(age_seconds)} "
                f"ago (heartbeat {heartbeat_seconds} s); quotes refuse it past "
                f"{describe_seconds(maximum_price_age_seconds_by_feed[reading.feed_address])}.",
                columns={
                    "feed": pair_names,
                    "address": reading.feed_address,
                    "updated": f"{describe_seconds(age_seconds)} ago",
                },
            )
        )
    return ReconciliationCheckResult(
        check_name="price_feeds",
        summary=f"{len(readings)} feeds, oldest answer {describe_seconds(max(ages))} old "
        f"(each stale past its heartbeat plus {PRICE_FEED_STALE_GRACE_SECONDS} s)",
        findings=findings,
    )
