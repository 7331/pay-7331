"""Period arithmetic of an invoice schedule (PLAN.md section 18), pure and in UTC.

Period n starts n intervals after the schedule's start, always counted from the start rather than from the period
before, so a monthly schedule on the 31st bills on 28 February and on 31 March again, never drifting to the 28th. A
monthly period keeps the start's time of day.
"""

import calendar
from datetime import UTC, datetime, timedelta

from app.payments.invoice_schedules.enums import InvoiceScheduleInterval

DAYS_PER_INTERVAL = {InvoiceScheduleInterval.DAILY: 1, InvoiceScheduleInterval.WEEKLY: 7}
MONTHS_PER_YEAR = 12


def compute_period_start(starts_at: datetime, *, interval: InvoiceScheduleInterval, period_index: int) -> datetime:
    """When period ``period_index`` (0 is the first) starts."""
    starts_at = starts_at.astimezone(UTC)
    if interval is not InvoiceScheduleInterval.MONTHLY:
        return starts_at + timedelta(days=DAYS_PER_INTERVAL[interval] * period_index)
    month_number = starts_at.year * MONTHS_PER_YEAR + (starts_at.month - 1) + period_index
    year, month_zero_based = divmod(month_number, MONTHS_PER_YEAR)
    month = month_zero_based + 1
    day = min(starts_at.day, calendar.monthrange(year, month)[1])
    return starts_at.replace(year=year, month=month, day=day)


def find_latest_started_period_index(
    starts_at: datetime, *, interval: InvoiceScheduleInterval, now: datetime
) -> int | None:
    """The newest period whose start is at or before ``now``; None before the first one starts. Computed, not counted,
    so a schedule paused for years costs nothing."""
    starts_at = starts_at.astimezone(UTC)
    if now < starts_at:
        return None
    if interval is not InvoiceScheduleInterval.MONTHLY:
        return (now - starts_at) // timedelta(days=DAYS_PER_INTERVAL[interval])
    now = now.astimezone(UTC)
    period_index = (now.year - starts_at.year) * MONTHS_PER_YEAR + (now.month - starts_at.month)
    # The clamped day (or the time of day) of this month's period may still be ahead.
    while period_index > 0 and compute_period_start(starts_at, interval=interval, period_index=period_index) > now:
        period_index -= 1
    return period_index


def find_first_period_index_from(starts_at: datetime, *, interval: InvoiceScheduleInterval, moment: datetime) -> int:
    """The first period starting at or after ``moment`` (a resumed schedule bills from there)."""
    latest_started = find_latest_started_period_index(starts_at, interval=interval, now=moment)
    if latest_started is None:
        return 0
    if compute_period_start(starts_at, interval=interval, period_index=latest_started) == moment:
        return latest_started
    return latest_started + 1


def format_period_day(period_start: datetime) -> str:
    """``20261021``: the order id's suffix, the period start's UTC day."""
    return period_start.astimezone(UTC).strftime("%Y%m%d")
