"""The chain watcher's enums."""

from enum import StrEnum


class RescanTier(StrEnum):
    """The rescan's two tiers (``rescan_service.py``, 2026-10-13), each with its own pass and interval."""

    # Invoices younger than `recent_deposit_window_seconds`, or not closed yet: every `rescan_interval_seconds`.
    RECENT = "recent"
    # Every other deposit ever made: every `old_deposit_rescan_interval_seconds`.
    OLD = "old"
