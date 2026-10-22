"""Invoice schedule settings (env ``PAY_MAXIMUM_INVOICE_SCHEDULES_PER_ACCOUNT``)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class InvoiceScheduleSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAY_", extra="ignore")

    # Schedules one account may have that have not ended (active or paused), 2026-10-21. Each makes at most one invoice
    # a day, so 100 daily schedules are 100 invoices a day, well under the 500 open invoices an account may have.
    maximum_invoice_schedules_per_account: int = Field(default=100, ge=1)


@lru_cache
def read_invoice_schedule_settings() -> InvoiceScheduleSettings:
    return InvoiceScheduleSettings()
