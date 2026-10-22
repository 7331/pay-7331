"""The namespaced key builder.

A namespace has either children or a variable tail, never both: every shape puts a constant leaf namespace
before its variable tail, so a caller-controlled tail cannot address another shape's keys.
"""

KEY_PREFIX = "pay"
KEY_SEPARATOR = ":"


def redis_key(*parts: str | int) -> str:
    """A namespaced Redis key, for example ``redis_key("leaderboard", "v1", "top")``."""
    return KEY_SEPARATOR.join((KEY_PREFIX, *(str(part) for part in parts)))
