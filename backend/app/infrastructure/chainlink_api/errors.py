"""The failure Chainlink reads raise."""


class ChainlinkFeedError(Exception):
    """A feed could not be read, answered zero or less, or no endpoint answered."""


class ChainlinkSequencerUnavailableError(ChainlinkFeedError):
    """The rollup's sequencer is down, was down less than the grace period ago, or its uptime feed cannot say."""
