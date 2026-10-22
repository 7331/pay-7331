"""Times on the wire: webhook bodies and the CSV export write them the same way."""

from datetime import UTC, datetime


def format_utc_timestamp(moment: datetime) -> str:
    """ISO 8601 in UTC with a ``Z``, as the API's JSON times and the Standard Webhooks spec's examples write it."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")
