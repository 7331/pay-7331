"""Typed log records of the invoice_schedules domain. Ids, codes and counts only; never a note, an address or a URL."""

import logging

from app.shared.logging import LogEvent


class InvoiceScheduleCreated(LogEvent):
    message = "Invoice schedule created"
    operation = "invoice_schedule_create"
    invoice_schedule_id: str
    interval: str


class InvoiceScheduleStatusChanged(LogEvent):
    message = "Invoice schedule status changed by its account"
    operation = "invoice_schedule_status"
    invoice_schedule_id: str
    previous_status: str
    status: str


class ScheduledInvoiceCreated(LogEvent):
    message = "A schedule's period came due and its invoice was made"
    operation = "scheduled_invoice_create"
    invoice_schedule_id: str
    invoice_id: str
    period_index: int
    # Periods that came due while nothing could bill them (the watcher was down, or attempts failed past the next
    # period's start): never billed afterwards.
    skipped_period_count: int


class ScheduledInvoiceFailed(LogEvent):
    level = logging.WARNING
    message = "A schedule's due invoice could not be made; retried later"
    operation = "scheduled_invoice_create"
    invoice_schedule_id: str
    period_index: int
    failure_code: str
    consecutive_failure_count: int
    retry_in_seconds: int


class ScheduledInvoiceFailedUnexpectedly(LogEvent):
    level = logging.ERROR
    message = "A schedule's due invoice failed with an unexpected error; retried later"
    operation = "scheduled_invoice_create"
    invoice_schedule_id: str


class ScheduledInvoicePeriodAlreadyBilled(LogEvent):
    level = logging.WARNING
    message = "A schedule's due invoice hit the one-invoice-per-period index; another runner billed it, retried later"
    operation = "scheduled_invoice_create"
    invoice_schedule_id: str


class InvoiceScheduleCompleted(LogEvent):
    message = "Invoice schedule reached its end"
    operation = "scheduled_invoice_create"
    invoice_schedule_id: str
    created_invoice_count: int
