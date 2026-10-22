"""The failures the EVM JSON-RPC client raises."""


class EvmRpcUnavailableError(Exception):
    """The endpoint was unreachable, answered a JSON-RPC error or sent a reply that is not what the method returns."""


class EvmInsufficientFundsError(EvmRpcUnavailableError):
    """The sender cannot pay the gas and value (``insufficient funds for gas * price + value``); every handler of an
    unavailable endpoint still catches it, the settlement runner tells it apart (2026-10-09)."""


class EvmLogRangeRefusedError(EvmRpcUnavailableError):
    """``eth_getLogs`` refused the block range or the number of results (or timed out on them): a narrower range
    may pass on the same endpoint (2026-10-12). Every handler of an unavailable endpoint still catches it."""


class EvmLogHistoryRefusedError(EvmRpcUnavailableError):
    """``eth_getLogs`` refused blocks this endpoint does not serve to a keyless caller (pruned, or archive behind a
    paid token): no range passes here, so the watcher tick fails and the next one fails over (2026-10-12)."""


class EvmLogBeyondHeadRefusedError(EvmRpcUnavailableError):
    """``eth_getLogs`` refused a ``toBlock`` past the head of the node that answered: a load-balanced endpoint's log
    backend can trail the one that answered the heads read by a block or two (2026-10-13). The watcher stops a read
    above the finalized block there for the tick; anywhere else it fails the tick over like any unavailable endpoint."""


class EvmExecutionRevertedError(Exception):
    """The node says the call or transaction reverts (``eth_estimateGas`` or ``eth_call``)."""


class EvmChainMismatchError(Exception):
    """An endpoint serves another chain id than the configured one; startup refuses it."""


class EvmRpcRateLimitedError(EvmRpcUnavailableError):
    """The endpoint rate limited us (HTTP 429 or a rate-limit JSON-RPC error) or is still set aside for an earlier one:
    no request goes to it until then, and every handler of an unavailable endpoint still catches it (2026-10-14).
    ``pause_seconds``: how long the reply's kind of limit asks for when it names no Retry-After."""

    def __init__(self, message: str, *, pause_seconds: int | None = None) -> None:
        super().__init__(message)
        self.pause_seconds = pause_seconds
