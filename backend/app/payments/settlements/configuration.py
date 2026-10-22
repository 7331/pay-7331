"""The operator keys per chain (PLAN.md section 5, operator keys; 2026-10-13). Never logged.

- ``PAY_OPERATOR_PRIVATE_KEY_<CHAIN>`` (e.g. ``PAY_OPERATOR_PRIVATE_KEY_POLYGON``): the refund key. It pays refunds
  from its own small float and their gas, and every sweep's gas when no sweeper key is set.
- ``PAY_SWEEPER_PRIVATE_KEYS_<CHAIN>``: comma-separated sweeper keys, gas only; each sweep is signed by one of them.

No deposit can be moved anywhere but its destination, so a leaked key loses only what it holds (PLAN.md section 3).
"""

import os

from pydantic import SecretStr


def read_operator_private_key(chain_key: str) -> SecretStr:
    """The refund key. Empty when unset: the watcher then refuses to start for that chain, the API never signs."""
    return SecretStr(os.environ.get(f"PAY_OPERATOR_PRIVATE_KEY_{chain_key.upper()}", ""))


def read_sweeper_private_keys(chain_key: str) -> list[SecretStr]:
    """The sweeper keys, each its own ``SecretStr``; empty when unset (sweeps then use the refund key)."""
    joined = os.environ.get(f"PAY_SWEEPER_PRIVATE_KEYS_{chain_key.upper()}", "")
    return [SecretStr(private_key.strip()) for private_key in joined.split(",") if private_key.strip()]


OPERATOR_KEY_VARIABLE_PREFIXES = ("PAY_OPERATOR_PRIVATE_KEY_", "PAY_SWEEPER_PRIVATE_KEYS_")


def find_set_operator_key_variable_names() -> list[str]:
    """The names (never the values) of every operator key variable set to something: the API refuses to start with
    any (lifespan/composition.py)."""
    return sorted(
        name for name, value in os.environ.items() if name.startswith(OPERATOR_KEY_VARIABLE_PREFIXES) and value.strip()
    )


# The anvil forks' chain keys (Polygon's, and Arbitrum One's since 2026-10-14): the only chains a test-only hook is read for.
LOCAL_FORK_CHAIN_KEYS = frozenset({"local_fork", "local_arbitrum_fork"})


def read_e2e_fail_after_operation_resolve(chain_key: str) -> frozenset[tuple[str, str]]:
    """Test-only (e2e/polygon_fork_flow.py): ``PAY_E2E_FAIL_AFTER_OPERATION_RESOLVE`` names ``kind:status`` pairs,
    comma-separated (``refund:final,sweep:failed``), whose resolution raises after the operation's new status is
    written and before its invoice effect, so the fork run proves the two commit together. Read for the fork chains
    only; empty on every real chain."""
    if chain_key not in LOCAL_FORK_CHAIN_KEYS:
        return frozenset()
    pairs = os.environ.get("PAY_E2E_FAIL_AFTER_OPERATION_RESOLVE", "")
    return frozenset(
        (kind.strip(), status.strip())
        for pair in pairs.split(",")
        if ":" in pair
        for kind, status in [pair.split(":", 1)]
    )
