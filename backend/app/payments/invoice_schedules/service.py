"""InvoiceScheduleService: an account's invoice schedules (PLAN.md section 18), on one session.

A schedule keeps ``POST /invoices``' terms, resolved and checked once when it is made, and an interval. As each period
comes due the watcher (``runner_service.py``) makes that period's invoice through ``InvoiceService.create_invoice`` in
the same transaction that advances the schedule, under a ``FOR UPDATE`` lock on the schedule row, and records
``schedule.invoice_created``; the unique ``(invoice_schedule_id, invoice_schedule_period_start)`` index on invoices is
the backstop, so a period is billed at most once however many runners try. Lock order is schedule row, then the
invoice creation's per-account advisory lock; the account's pause, resume and cancel take only the schedule row.

Push only: every invoice is an ordinary one its payer pays by sending; the account's server hands
the payer its link (pay-7331 sends no email).
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.accounts.types import AccountAccess, AccountApiTokenAccess
from app.payments.chains.errors import ChainKindNotSupportedError
from app.payments.chains.types import AssetConfiguration, EvmChainConfiguration
from app.payments.invoice_schedules.configuration import InvoiceScheduleSettings, read_invoice_schedule_settings
from app.payments.invoice_schedules.enums import InvoiceScheduleInterval, InvoiceScheduleStatus
from app.payments.invoice_schedules.errors import (
    InvoiceScheduleNotFoundError,
    InvoiceScheduleStateError,
    OrderIdPrefixTakenError,
    ScheduledOrderIdTakenError,
    TooManyInvoiceSchedulesError,
)
from app.payments.invoice_schedules.log_events import (
    InvoiceScheduleCompleted,
    InvoiceScheduleCreated,
    InvoiceScheduleStatusChanged,
    ScheduledInvoiceCreated,
)
from app.payments.invoice_schedules.models import InvoiceSchedule
from app.payments.invoice_schedules.periods import (
    compute_period_start,
    find_first_period_index_from,
    find_latest_started_period_index,
    format_period_day,
)
from app.payments.invoice_schedules.repository import InvoiceScheduleRepository
from app.payments.invoice_schedules.schemas import (
    CreateInvoiceScheduleRequest,
    InvoiceScheduleInvoiceListResponse,
    InvoiceScheduleListResponse,
    InvoiceScheduleResponse,
)
from app.payments.invoices.enums import InvoiceCreationChannel, InvoiceEventType
from app.payments.invoices.errors import FactoryNotDeployedError
from app.payments.invoices.invoice_creation import MINIMUM_FIAT_INVOICE_AMOUNT, refuse_below_minimum_sweep
from app.payments.invoices.offered_chains import (
    FIAT_CURRENCIES,
    resolve_account_invoice_offer,
    resolve_offered_assets,
)
from app.payments.invoices.service import InvoiceService
from app.payments.invoices.types import NewInvoice
from app.shared import logging as log
from app.shared.amounts import parse_decimal_amount, to_base_units
from app.shared.errors import InvalidInputError
from app.shared.identifiers import generate_public_id

logger = log.get_logger(__name__)

INVOICE_SCHEDULES_PER_ACCOUNT_PAGE = 100
INVOICES_PER_INVOICE_SCHEDULE_PAGE = 200


def as_utc(moment: datetime) -> datetime:
    """A time without a zone is UTC, as the CSV export reads one."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def interval_of(invoice_schedule: InvoiceSchedule) -> InvoiceScheduleInterval:
    return InvoiceScheduleInterval(invoice_schedule.billing_interval)


def period_start_of(invoice_schedule: InvoiceSchedule, *, period_index: int) -> datetime:
    return compute_period_start(
        invoice_schedule.starts_at, interval=interval_of(invoice_schedule), period_index=period_index
    )


def order_id_prefix_of(invoice_schedule: InvoiceSchedule) -> str:
    return invoice_schedule.order_id_prefix or invoice_schedule.id


def has_reached_its_end(invoice_schedule: InvoiceSchedule, *, next_period_index: int) -> bool:
    """No more invoices: its count is made, or its next period would start at or after its end date."""
    if invoice_schedule.ends_after_invoice_count is not None:
        return invoice_schedule.created_invoice_count >= invoice_schedule.ends_after_invoice_count
    if invoice_schedule.ends_at is not None:
        return period_start_of(invoice_schedule, period_index=next_period_index) >= invoice_schedule.ends_at
    return False


def find_next_billed_period_start(invoice_schedule: InvoiceSchedule, *, now: datetime) -> datetime:
    """The start of the period the next invoice bills, as the runner picks it: the newest started period once it is at
    or past ``next_period_index`` (so a fresh schedule shows period 0, due now, and a skipped or failing period never
    shows), else ``next_period_index``'s start, still to come."""
    latest_started = find_latest_started_period_index(
        invoice_schedule.starts_at, interval=interval_of(invoice_schedule), now=now
    )
    next_billed_index = max(invoice_schedule.next_period_index, latest_started if latest_started is not None else 0)
    return period_start_of(invoice_schedule, period_index=next_billed_index)


def present_invoice_schedule(invoice_schedule: InvoiceSchedule) -> InvoiceScheduleResponse:
    status = InvoiceScheduleStatus(invoice_schedule.status)
    return InvoiceScheduleResponse(
        id=invoice_schedule.id,
        status=status,
        interval=interval_of(invoice_schedule),
        starts_at=invoice_schedule.starts_at,
        ends_after_invoice_count=invoice_schedule.ends_after_invoice_count,
        ends_at=invoice_schedule.ends_at,
        next_period_start=find_next_billed_period_start(invoice_schedule, now=datetime.now(UTC))
        if status is InvoiceScheduleStatus.ACTIVE
        else None,
        created_invoice_count=invoice_schedule.created_invoice_count,
        last_invoice_created_at=invoice_schedule.last_invoice_created_at,
        last_failure_code=invoice_schedule.last_failure_code,
        last_failure_at=invoice_schedule.last_failure_at,
        amount=invoice_schedule.amount,
        currency=invoice_schedule.currency,
        chains=invoice_schedule.chain_keys,
        assets=invoice_schedule.assets,
        assets_by_chain=invoice_schedule.assets_by_chain,
        destination_address=invoice_schedule.destination_address,
        note=invoice_schedule.note,
        metadata=invoice_schedule.private_metadata,
        order_id_prefix=order_id_prefix_of(invoice_schedule),
        lifetime_seconds=invoice_schedule.lifetime_seconds,
        accuracy_percent=format(invoice_schedule.accuracy_percent.normalize(), "f"),
        return_url=invoice_schedule.return_url,
        success_url=invoice_schedule.success_url,
        callback_url=invoice_schedule.callback_url,
        paused_at=invoice_schedule.paused_at,
        ended_at=invoice_schedule.ended_at,
        created_at=invoice_schedule.created_at,
        updated_at=invoice_schedule.updated_at,
    )


def refuse_invalid_schedule_amount(
    amount: Decimal, *, currency: str, offered: dict[str, list[AssetConfiguration]]
) -> None:
    """A fiat amount has at most 2 decimals and is at least the smallest invoice; a crypto amount names exactly one
    chain and that one asset, and is at least its minimum sweep."""
    if currency in FIAT_CURRENCIES:
        if amount != amount.quantize(Decimal("0.01")):
            raise InvalidInputError("amount: at most 2 decimals for a fiat currency")
        if amount < MINIMUM_FIAT_INVOICE_AMOUNT:
            raise InvalidInputError(f"amount: the smallest invoice is {MINIMUM_FIAT_INVOICE_AMOUNT} {currency}")
    else:
        only_chain_assets = next(iter(offered.values()))
        if (
            len(offered) != 1
            or len(only_chain_assets) != 1
            or only_chain_assets[0].symbol.casefold() != currency.casefold()
        ):
            raise InvalidInputError("currency: a crypto amount needs exactly one chain and that one asset")
        asset = only_chain_assets[0]
        refuse_below_minimum_sweep(to_base_units(amount, decimals=asset.decimals, field_name="amount"), asset=asset)


def resolve_schedule_window(schedule_request: CreateInvoiceScheduleRequest) -> tuple[datetime, datetime | None]:
    """When the schedule starts (now when not given) and ends; ends_at is refused next to ends_after_invoice_count or
    at or before starts_at."""
    starts_at = as_utc(schedule_request.starts_at) if schedule_request.starts_at else datetime.now(UTC)
    ends_at = as_utc(schedule_request.ends_at) if schedule_request.ends_at else None
    if ends_at is not None and schedule_request.ends_after_invoice_count is not None:
        raise InvalidInputError("ends_at: send either ends_at or ends_after_invoice_count, not both")
    if ends_at is not None and ends_at <= starts_at:
        raise InvalidInputError("ends_at: must be after starts_at")
    return starts_at, ends_at


class InvoiceScheduleService:
    def __init__(
        self,
        *,
        repository: InvoiceScheduleRepository,
        invoice_service: InvoiceService,
        invoice_schedule_settings: InvoiceScheduleSettings,
    ) -> None:
        self.repository = repository
        self.invoice_service = invoice_service
        self.chains_service = invoice_service.chains_service
        self.invoice_schedule_settings = invoice_schedule_settings

    @classmethod
    def on_session(cls, session: AsyncSession, *, invoice_service: InvoiceService) -> Self:
        return cls(
            repository=InvoiceScheduleRepository(session=session),
            invoice_service=invoice_service,
            invoice_schedule_settings=read_invoice_schedule_settings(),
        )

    async def get_account_invoice_schedule(
        self, invoice_schedule_id: str, *, account_id: int, lock: bool = False
    ) -> InvoiceSchedule:
        """The account's own schedule; another's is not found, as if it did not exist."""
        invoice_schedule = await self.repository.find_invoice_schedule(invoice_schedule_id, lock=lock)
        if invoice_schedule is None or invoice_schedule.account_id != account_id:
            raise InvoiceScheduleNotFoundError("No invoice schedule with that id.")
        return invoice_schedule

    # The account

    async def create_invoice_schedule(
        self, schedule_request: CreateInvoiceScheduleRequest, *, account_access: AccountAccess | AccountApiTokenAccess
    ) -> tuple[InvoiceScheduleResponse, dict[str, list[str]]]:
        """Checked as its first invoice would be, short of the price (each invoice is quoted when its period comes):
        the chains and coins, the amount, the destination on every offered chain (a chain whose RPC is down is
        refused, not skipped: the runner skips the code read) and the callback URL. EVM chains only. Refused past
        ``PAY_MAXIMUM_INVOICE_SCHEDULES_PER_ACCOUNT`` live schedules (409 ``too_many_invoice_schedules``) and for an
        order id prefix a live schedule of the account uses, or whose first invoice's order id an invoice of the
        account already has (409 ``order_id_prefix_taken``). Answers the schedule and each offered chain's coins."""
        account_id = account_access.account_id
        currency = schedule_request.currency.upper()
        offer = resolve_account_invoice_offer(
            invoiceable_evm_chain_keys=self.invoice_service.list_invoiceable_evm_chain_keys(),
            chain=schedule_request.chain,
            chains=schedule_request.chains,
            assets=schedule_request.assets,
            assets_by_chain=schedule_request.assets_by_chain,
            currency=currency,
            accepted_asset_symbols_by_chain=account_access.accepted_asset_symbols_by_chain,
        )
        offered = resolve_offered_assets(
            self.chains_service,
            chain_keys=offer.chain_keys,
            asset_symbols=offer.assets,
            assets_by_chain=offer.assets_by_chain,
            defaulted_chains=offer.defaulted_chains,
        )
        offered_chains = self.list_schedule_offered_evm_chains(list(offered))
        amount = parse_decimal_amount(schedule_request.amount, field_name="amount")
        refuse_invalid_schedule_amount(amount, currency=currency, offered=offered)
        starts_at, ends_at = resolve_schedule_window(schedule_request)
        destination_address, _ = await self.invoice_service.evm_deposit_targets.check_destination_on_offered_chains(
            schedule_request.destination_address or account_access.payout_address,
            chains=offered_chains,
            allow_contract_destination=schedule_request.allow_contract_destination,
            defaulted_chains=False,
        )
        if schedule_request.callback_url is not None:
            await self.invoice_service.invoice_creation.check_callback_url(
                schedule_request.callback_url, account_id=account_id
            )
        await self.repository.lock_account_invoice_schedule_creation(account_id)
        maximum_schedules = self.invoice_schedule_settings.maximum_invoice_schedules_per_account
        if await self.repository.count_live_account_invoice_schedules(account_id) >= maximum_schedules:
            raise TooManyInvoiceSchedulesError(
                f"this account already has {maximum_schedules} active or paused invoice schedules; cancel one first"
            )
        await self.refuse_taken_order_id_prefix(schedule_request, account_id=account_id, starts_at=starts_at)
        invoice_schedule = await self.repository.insert_invoice_schedule(
            InvoiceSchedule(
                id=generate_public_id("sch_"),
                account_id=account_id,
                status=InvoiceScheduleStatus.ACTIVE.value,
                amount=format(amount, "f"),
                currency=currency,
                chain_keys=offer.chain_keys,
                defaulted_chains=offer.defaulted_chains,
                assets=offer.assets,
                assets_by_chain=offer.assets_by_chain,
                destination_address=destination_address,
                note=schedule_request.note,
                private_metadata=schedule_request.metadata,
                order_id_prefix=schedule_request.order_id_prefix,
                lifetime_seconds=schedule_request.lifetime_seconds,
                accuracy_percent=schedule_request.accuracy_percent,
                return_url=schedule_request.return_url,
                success_url=schedule_request.success_url,
                callback_url=schedule_request.callback_url,
                billing_interval=schedule_request.interval.value,
                starts_at=starts_at,
                ends_after_invoice_count=schedule_request.ends_after_invoice_count,
                ends_at=ends_at,
                next_period_index=0,
                next_attempt_at=starts_at,
                created_invoice_count=0,
                consecutive_failure_count=0,
            )
        )
        log.emit(
            logger,
            event=InvoiceScheduleCreated(
                invoice_schedule_id=invoice_schedule.id, interval=schedule_request.interval.value
            ),
        )
        offered_asset_symbols_by_chain = {
            chain_key: [asset.symbol for asset in chain_assets] for chain_key, chain_assets in offered.items()
        }
        return present_invoice_schedule(invoice_schedule), offered_asset_symbols_by_chain

    def list_schedule_offered_evm_chains(self, offered_chain_keys: list[str]) -> list[EvmChainConfiguration]:
        """Each offered chain, refused unless it is an EVM chain with a deposit factory configured."""
        offered_chains: list[EvmChainConfiguration] = []
        for chain_key in offered_chain_keys:
            chain = self.chains_service.get_chain(chain_key)
            if not isinstance(chain, EvmChainConfiguration):
                raise ChainKindNotSupportedError(f"{chain.name}: invoice schedules are made on EVM chains only")
            if chain_key not in self.invoice_service.wiring.factory_clients:
                raise FactoryNotDeployedError(f"no deposit factory is configured on {chain_key} yet")
            offered_chains.append(chain)
        return offered_chains

    async def refuse_taken_order_id_prefix(
        self, schedule_request: CreateInvoiceScheduleRequest, *, account_id: int, starts_at: datetime
    ) -> None:
        """Refuses an order id prefix a live schedule of the account uses, or whose first invoice's order id an invoice
        of the account already has (409 ``order_id_prefix_taken``)."""
        if schedule_request.order_id_prefix is not None and (
            await self.repository.live_account_invoice_schedule_uses_prefix(
                account_id, order_id_prefix=schedule_request.order_id_prefix
            )
        ):
            raise OrderIdPrefixTakenError(
                "order_id_prefix: another active or paused schedule of this account uses it; pick another"
            )
        if schedule_request.order_id_prefix is not None:
            # A cancelled schedule's (or the account's own) invoice may already hold the first period's order id; the
            # runner would refuse that period as order_id_taken and the first invoice would be lost.
            first_period_index = (
                find_latest_started_period_index(starts_at, interval=schedule_request.interval, now=datetime.now(UTC))
                or 0
            )
            first_period_start = compute_period_start(
                starts_at, interval=schedule_request.interval, period_index=first_period_index
            )
            first_order_id = f"{schedule_request.order_id_prefix}-{format_period_day(first_period_start)}"
            if await self.invoice_service.account_order_id_is_used(first_order_id, account_id=account_id):
                raise OrderIdPrefixTakenError(
                    f"order_id_prefix: invoice order id {first_order_id} is already used; pick another prefix"
                )

    async def list_account_invoice_schedules(self, account_id: int) -> InvoiceScheduleListResponse:
        invoice_schedules = await self.repository.list_account_invoice_schedules(
            account_id, limit=INVOICE_SCHEDULES_PER_ACCOUNT_PAGE
        )
        return InvoiceScheduleListResponse(
            invoice_schedules=[present_invoice_schedule(invoice_schedule) for invoice_schedule in invoice_schedules]
        )

    async def read_account_invoice_schedule(
        self, invoice_schedule_id: str, *, account_id: int
    ) -> InvoiceScheduleResponse:
        return present_invoice_schedule(
            await self.get_account_invoice_schedule(invoice_schedule_id, account_id=account_id)
        )

    async def list_invoice_schedule_invoices(
        self, invoice_schedule_id: str, *, account_id: int
    ) -> InvoiceScheduleInvoiceListResponse:
        invoice_schedule = await self.get_account_invoice_schedule(invoice_schedule_id, account_id=account_id)
        return InvoiceScheduleInvoiceListResponse(
            invoices=await self.invoice_service.list_invoice_schedule_invoices(
                invoice_schedule.id, account_id=account_id, limit=INVOICES_PER_INVOICE_SCHEDULE_PAGE
            )
        )

    def change_status(self, invoice_schedule: InvoiceSchedule, *, status: InvoiceScheduleStatus) -> None:
        log.emit(
            logger,
            event=InvoiceScheduleStatusChanged(
                invoice_schedule_id=invoice_schedule.id, previous_status=invoice_schedule.status, status=status.value
            ),
        )
        invoice_schedule.status = status.value

    async def pause_invoice_schedule(self, invoice_schedule_id: str, *, account_id: int) -> InvoiceScheduleResponse:
        """No invoice while paused; pausing a paused one changes nothing. 409 on an ended one."""
        invoice_schedule = await self.get_account_invoice_schedule(
            invoice_schedule_id, account_id=account_id, lock=True
        )
        status = InvoiceScheduleStatus(invoice_schedule.status)
        if status is InvoiceScheduleStatus.ACTIVE:
            self.change_status(invoice_schedule, status=InvoiceScheduleStatus.PAUSED)
            invoice_schedule.paused_at = datetime.now(UTC)
            await self.repository.save_invoice_schedule(invoice_schedule)
        elif status is not InvoiceScheduleStatus.PAUSED:
            raise InvoiceScheduleStateError(f"this schedule is {status.value}; only an active one can be paused")
        return present_invoice_schedule(invoice_schedule)

    async def resume_invoice_schedule(self, invoice_schedule_id: str, *, account_id: int) -> InvoiceScheduleResponse:
        """Bills again from the first period starting now or later: a period that started while paused is skipped,
        never billed late. Resuming an active one changes nothing; 409 on an ended one."""
        invoice_schedule = await self.get_account_invoice_schedule(
            invoice_schedule_id, account_id=account_id, lock=True
        )
        status = InvoiceScheduleStatus(invoice_schedule.status)
        if status is InvoiceScheduleStatus.ACTIVE:
            return present_invoice_schedule(invoice_schedule)
        if status is not InvoiceScheduleStatus.PAUSED:
            raise InvoiceScheduleStateError(f"this schedule is {status.value}; only a paused one can be resumed")
        now = datetime.now(UTC)
        next_period_index = max(
            invoice_schedule.next_period_index,
            find_first_period_index_from(
                invoice_schedule.starts_at, interval=interval_of(invoice_schedule), moment=now
            ),
        )
        invoice_schedule.paused_at = None
        invoice_schedule.last_failure_code = None
        invoice_schedule.next_period_index = next_period_index
        if has_reached_its_end(invoice_schedule, next_period_index=next_period_index):
            self.change_status(invoice_schedule, status=InvoiceScheduleStatus.COMPLETED)
            invoice_schedule.ended_at = now
        else:
            self.change_status(invoice_schedule, status=InvoiceScheduleStatus.ACTIVE)
            invoice_schedule.next_attempt_at = period_start_of(invoice_schedule, period_index=next_period_index)
            invoice_schedule.consecutive_failure_count = 0
        await self.repository.save_invoice_schedule(invoice_schedule)
        return present_invoice_schedule(invoice_schedule)

    async def cancel_invoice_schedule(self, invoice_schedule_id: str, *, account_id: int) -> InvoiceScheduleResponse:
        """No more invoices, for good; those made run their course. Cancelling an ended one changes nothing."""
        invoice_schedule = await self.get_account_invoice_schedule(
            invoice_schedule_id, account_id=account_id, lock=True
        )
        if not InvoiceScheduleStatus(invoice_schedule.status).is_ended:
            self.change_status(invoice_schedule, status=InvoiceScheduleStatus.CANCELLED)
            invoice_schedule.ended_at = datetime.now(UTC)
            await self.repository.save_invoice_schedule(invoice_schedule)
        return present_invoice_schedule(invoice_schedule)

    # The watcher (runner_service.py)

    async def list_due_invoice_schedule_ids(self, *, now: datetime, limit: int) -> list[str]:
        return await self.repository.list_due_invoice_schedule_ids(now=now, limit=limit)

    async def create_due_scheduled_invoice(self, invoice_schedule_id: str, *, now: datetime) -> str | None:
        """The newest started period's invoice, once: under the schedule's row lock (``SKIP LOCKED``: another runner's
        turn is left alone) the invoice is made, ``schedule.invoice_created`` recorded and the schedule advanced, all
        in the caller's transaction. Older periods nobody billed in time are skipped (logged), so a watcher that was
        down for days makes one invoice, not a backlog. Answers the invoice id, or None when there was nothing to do.
        Raises the creation's refusal (``AppError``) for the caller to record; the transaction then rolls back."""
        invoice_schedule = await self.repository.lock_due_invoice_schedule(invoice_schedule_id, now=now)
        if invoice_schedule is None:
            return None
        latest_started = find_latest_started_period_index(
            invoice_schedule.starts_at, interval=interval_of(invoice_schedule), now=now
        )
        if latest_started is None or latest_started < invoice_schedule.next_period_index:
            invoice_schedule.next_attempt_at = period_start_of(
                invoice_schedule, period_index=invoice_schedule.next_period_index
            )
            return None
        if has_reached_its_end(invoice_schedule, next_period_index=latest_started):
            self.complete_invoice_schedule(invoice_schedule, now=now)
            return None
        period_start = period_start_of(invoice_schedule, period_index=latest_started)
        if await self.invoice_service.invoice_schedule_period_is_billed(invoice_schedule.id, period_start=period_start):
            # Billed already (the schedule's own row says otherwise only if it was moved back by hand): move on.
            invoice_schedule.next_period_index = latest_started + 1
            invoice_schedule.next_attempt_at = period_start_of(invoice_schedule, period_index=latest_started + 1)
            return None
        invoice = await self.invoice_service.create_invoice(
            NewInvoice(
                chain_keys=invoice_schedule.chain_keys,
                defaulted_chains=invoice_schedule.defaulted_chains,
                amount=parse_decimal_amount(invoice_schedule.amount, field_name="amount"),
                currency=invoice_schedule.currency,
                assets=invoice_schedule.assets,
                assets_by_chain=invoice_schedule.assets_by_chain,
                destination_address=invoice_schedule.destination_address,
                # Never None: lock_due_invoice_schedule takes only a schedule with an account.
                account_id=invoice_schedule.account_id,
                # Checked when the schedule was made; skipping the code read keeps the row lock short.
                allow_contract_destination=True,
                note=invoice_schedule.note,
                order_id=f"{order_id_prefix_of(invoice_schedule)}-{format_period_day(period_start)}",
                lifetime_seconds=invoice_schedule.lifetime_seconds,
                accuracy_percent=invoice_schedule.accuracy_percent,
                return_url=invoice_schedule.return_url,
                success_url=invoice_schedule.success_url,
                callback_url=invoice_schedule.callback_url,
                metadata=invoice_schedule.private_metadata,
                invoice_schedule_id=invoice_schedule.id,
                invoice_schedule_period_start=period_start,
            ),
            client_ip_address=None,
            creation_channel=InvoiceCreationChannel.SCHEDULE,
        )
        # A repeated order id answers the invoice made with it before instead of a new one.
        if invoice.invoice_schedule_id != invoice_schedule.id:
            # One the account made itself.
            raise ScheduledOrderIdTakenError(
                f"order id {invoice.order_id} is already used by another invoice of this account"
            )
        if invoice.invoice_schedule_period_start != period_start:
            raise ScheduledOrderIdTakenError(f"order id {invoice.order_id} already names another period's invoice")
        await self.invoice_service.record_invoice_event(invoice, event_type=InvoiceEventType.SCHEDULED_INVOICE_CREATED)
        invoice_schedule.created_invoice_count += 1
        invoice_schedule.last_invoice_created_at = now
        invoice_schedule.last_failure_code = None
        invoice_schedule.consecutive_failure_count = 0
        skipped_period_count = latest_started - invoice_schedule.next_period_index
        invoice_schedule.next_period_index = latest_started + 1
        log.emit(
            logger,
            event=ScheduledInvoiceCreated(
                invoice_schedule_id=invoice_schedule.id,
                invoice_id=invoice.id,
                period_index=latest_started,
                skipped_period_count=skipped_period_count,
            ),
        )
        if has_reached_its_end(invoice_schedule, next_period_index=latest_started + 1):
            self.complete_invoice_schedule(invoice_schedule, now=now)
        else:
            invoice_schedule.next_attempt_at = period_start_of(invoice_schedule, period_index=latest_started + 1)
        await self.repository.session.flush()
        return invoice.id

    def complete_invoice_schedule(self, invoice_schedule: InvoiceSchedule, *, now: datetime) -> None:
        invoice_schedule.status = InvoiceScheduleStatus.COMPLETED.value
        invoice_schedule.ended_at = now
        log.emit(
            logger,
            event=InvoiceScheduleCompleted(
                invoice_schedule_id=invoice_schedule.id,
                created_invoice_count=invoice_schedule.created_invoice_count,
            ),
        )

    async def record_scheduled_invoice_failure(
        self, invoice_schedule_id: str, *, failure_code: str, now: datetime, retry_in_seconds_by_failures: list[int]
    ) -> tuple[int, int, int] | None:
        """Notes the refusal on the schedule and puts its next attempt off, longer for each failure in a row (the
        last step repeats); answers (period index, failures in a row, seconds until the retry), or None when the
        schedule is no longer active."""
        invoice_schedule = await self.repository.find_invoice_schedule(invoice_schedule_id, lock=True)
        if invoice_schedule is None or invoice_schedule.status != InvoiceScheduleStatus.ACTIVE.value:
            return None
        invoice_schedule.consecutive_failure_count += 1
        invoice_schedule.last_failure_code = failure_code
        invoice_schedule.last_failure_at = now
        retry_in_seconds = retry_in_seconds_by_failures[
            min(invoice_schedule.consecutive_failure_count, len(retry_in_seconds_by_failures)) - 1
        ]
        invoice_schedule.next_attempt_at = datetime.fromtimestamp(now.timestamp() + retry_in_seconds, tz=UTC)
        await self.repository.session.flush()
        return invoice_schedule.next_period_index, invoice_schedule.consecutive_failure_count, retry_in_seconds
