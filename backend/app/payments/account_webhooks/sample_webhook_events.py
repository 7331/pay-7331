"""Sample invoice events for "Send test event" (2026-10-21): a made-up invoice of the account, shaped exactly like
``GET /api/v1/invoices/{id}`` (``InvoiceAccountResponse``), in the state each event type leaves an invoice in, so a
receiver can run its real handler against it.

Cryptomus' test webhook does the same for a status the merchant picks ("If these parameters are not provided, the
webhook will be sent with a test invoice", https://doc.cryptomus.com/merchant-api/payments/testing-webhook). Here a
sample is marked three ways so no receiver fulfils it: the body's top-level ``"test": true`` (absent from real events),
a ``webhook-id`` starting ``test_``, and ``"test": true`` in the invoice's ``metadata``. Its id is a fixed, well-formed
one the API has no invoice for (404), so a receiver that rereads it, as the docs ask, finds nothing to fulfil. It is
signed with the account's secret like every delivery. Addresses (EIP-55 checksummed) and hashes are fixed made-up
values (``0x7e57...`` is "test"), never a real deposit; the destination is the account's own payout address, as an
invoice it made would be.

Every static deposit address event type (``static_address.*``, PLAN.md section 17) has one too: a made-up customer
address (``sda_TestSampLe7e57SampLe7e``, reading 404 as well) shaped like ``GET /api/v1/static-deposit-addresses/{id}``
without its payments, beside the payment or sweep the event is about, as the real event's ``data`` holds them.

Every ``InvoiceEventType`` an EVM invoice of an account can get has a sample, ``schedule.invoice_created`` (a schedule
made the invoice, PLAN.md section 18) included. Left out: the Monero-only ``seen`` and
``payment_held``, and the refund events (``invoice.refunded``, ``refund.*``): only invoices paying the operator's own
address get refunds, so an account's receiver never sees them.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import JsonValue

from app.payments.account_webhooks.enums import WebhookTestEventType
from app.payments.chains.service import ChainsService, present_asset, present_chain
from app.payments.chains.types import AssetConfiguration, EvmChainConfiguration
from app.payments.invoice_schedules.schemas import DEFAULT_SCHEDULED_INVOICE_LIFETIME_SECONDS
from app.payments.invoices.enums import (
    InvoiceEventType,
    InvoiceStatus,
    InvoiceSweepStatus,
    RefundUnavailableReason,
    StrayRefundHandling,
)
from app.payments.invoices.payment_uris import build_payment_uri
from app.payments.invoices.schemas import (
    InvoiceAccountResponse,
    InvoiceFiat,
    InvoicePublicPayment,
    InvoicePublicStrayPayment,
    InvoiceQuote,
    InvoiceRefundOffer,
    InvoiceSweep,
)
from app.payments.static_deposit_addresses.enums import StaticDepositAddressEventType
from app.payments.static_deposit_addresses.schemas import (
    StaticDepositAddressPayment,
    StaticDepositAddressResponse,
    StaticDepositAddressSweep,
)
from app.shared.errors import InvalidInputError

SAMPLE_INVOICE_EVENT_TYPES = frozenset(InvoiceEventType).intersection(WebhookTestEventType)
SAMPLE_STATIC_DEPOSIT_ADDRESS_EVENT_TYPES = frozenset(StaticDepositAddressEventType).intersection(WebhookTestEventType)
SAMPLE_EVENT_TYPES = tuple(WebhookTestEventType)

# Well-formed like every invoice id (``inv_`` and 22 base58 characters), fixed so a test is recognisable.
SAMPLE_INVOICE_ID = "inv_TestSampLe7e57SampLe7e"
SAMPLE_DEPOSIT_ADDRESS = "0x7E577e577e577E577e577E577E577E577e577E57"
SAMPLE_PAYER_ADDRESS = "0xB0B0b0B0B0B0B0b0B0B0B0b0b0b0b0B0b0b0B0B0"
SAMPLE_PAYMENT_HASH = "0x" + "7e57" * 16
SAMPLE_SWEEP_HASH = "0x" + "5eef" * 16
SAMPLE_STRAY_HASH = "0x" + "57a7" * 16
SAMPLE_BLOCK_NUMBER = 78_000_000
SAMPLE_FIAT_AMOUNT = Decimal(5)
SAMPLE_EUR_PER_USD = Decimal("0.86")
SAMPLE_ORDER_ID = "test-order-1042"
SAMPLE_STATIC_DEPOSIT_ADDRESS_ID = "sda_TestSampLe7e57SampLe7e"
SAMPLE_CUSTOMER_REFERENCE = "test-customer-77"
SAMPLE_STATIC_DEPOSIT_PAYMENT_ID = 1


def choose_sample_chain(chains_service: ChainsService) -> EvmChainConfiguration:
    """The first EVM chain taking invoices, else the first EVM chain."""
    evm_chains = [chain for chain in chains_service.evm_chains if chain.takes_invoices] or chains_service.evm_chains
    if not evm_chains:
        raise InvalidInputError("event_type: no EVM chain is enabled here to make a sample on")
    return evm_chains[0]


# A scheduled invoice's sample (2026-10-21): its schedule id is as well-formed and as unknown as the invoice's, and its
# order id is the schedule's prefix and the period's day, as a real one's (payments/invoice_schedules).
SAMPLE_INVOICE_SCHEDULE_ID = "sch_TestSampLe7e57SampLe7e"
SAMPLE_SCHEDULED_ORDER_ID_PREFIX = "test-plan-77"


def choose_sample_asset(chain: EvmChainConfiguration) -> AssetConfiguration:
    """A USD-pegged stablecoin when the chain has one, so the sample's amounts read as its 5 USD."""
    return next((asset for asset in chain.assets if asset.usd_peg), chain.assets[0])


def format_sample_amount(amount: Decimal) -> str:
    text = format(amount, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def build_sample_invoice(
    event_type: str, *, chains_service: ChainsService, public_base_url: str, destination_address: str, now: datetime
) -> InvoiceAccountResponse:
    """The account view of the sample invoice as it stands right after ``event_type``."""
    if event_type not in SAMPLE_INVOICE_EVENT_TYPES:
        raise InvalidInputError(f"event_type: one of {', '.join(SAMPLE_EVENT_TYPES)}")
    chain = choose_sample_chain(chains_service)
    asset = choose_sample_asset(chain)
    native_asset = next((coin for coin in chain.assets if coin.is_native), chain.assets[0])
    amount_due = SAMPLE_FIAT_AMOUNT
    received = {
        "invoice.payment_detected": Decimal(0),
        "invoice.underpaid": Decimal("2.5"),
        "invoice.overpaid": Decimal(6),
        "invoice.expired": Decimal(0),
        "invoice.cancelled": Decimal(0),
        "invoice.late_payment": Decimal(0),
        "invoice.stray_payment": Decimal(0),
        "schedule.invoice_created": Decimal(0),
    }.get(event_type, amount_due)
    status = {
        "invoice.payment_detected": InvoiceStatus.CONFIRMING,
        "invoice.underpaid": InvoiceStatus.UNDERPAID,
        "invoice.overpaid": InvoiceStatus.OVERPAID,
        "invoice.expired": InvoiceStatus.EXPIRED,
        "invoice.cancelled": InvoiceStatus.CANCELLED,
        "invoice.late_payment": InvoiceStatus.EXPIRED,
        "invoice.stray_payment": InvoiceStatus.AWAITING_PAYMENT,
        "schedule.invoice_created": InvoiceStatus.AWAITING_PAYMENT,
    }.get(event_type, InvoiceStatus.PAID)
    is_paid = status.is_settled_by_payment
    # As remaining_base_units: 0 once paid, else the rest of the 5 USD (an expired or cancelled one's too).
    remaining = Decimal(0) if is_paid else max(amount_due - received, Decimal(0))
    created_at = now - timedelta(minutes=12)
    # The payment row the invoice lists: a seen, not yet final one while confirming; none when nothing was credited.
    # A late payment adds no row: the rescan finds money on the closed deposit's balance, flags the invoice and asks
    # for a sweep (ChainReadingsService.apply_rescan), so that sample shows the sweep under way.
    if event_type == "invoice.payment_detected":
        payment_is_final, payment_amount = False, amount_due
    else:
        payment_is_final, payment_amount = True, received
    payments = (
        [
            InvoicePublicPayment(
                tx_hash=SAMPLE_PAYMENT_HASH,
                sender=SAMPLE_PAYER_ADDRESS,
                amount=format_sample_amount(payment_amount),
                block_number=SAMPLE_BLOCK_NUMBER,
                final=payment_is_final,
            )
        ]
        if payment_amount > 0
        else []
    )
    sweeps = {
        "invoice.swept": [
            InvoiceSweep(
                tx_hash=SAMPLE_SWEEP_HASH,
                asset_symbol=asset.symbol,
                amount=format_sample_amount(amount_due),
                status=InvoiceSweepStatus.FINAL,
            )
        ],
        "invoice.sweep_failed": [
            InvoiceSweep(tx_hash=SAMPLE_SWEEP_HASH, asset_symbol=None, amount=None, status=InvoiceSweepStatus.FAILED)
        ],
        "invoice.late_payment": [
            InvoiceSweep(tx_hash=None, asset_symbol=None, amount=None, status=InvoiceSweepStatus.PENDING)
        ],
    }.get(event_type, [])
    refund_offer_reason = (
        RefundUnavailableReason.RECEIVER_SETTLES if received != amount_due else RefundUnavailableReason.NOT_OVERPAID
    )
    stray_payments = (
        [
            InvoicePublicStrayPayment(
                asset_symbol=native_asset.symbol,
                amount="12.5",
                sender=SAMPLE_PAYER_ADDRESS,
                tx_hash=SAMPLE_STRAY_HASH,
                block_number=SAMPLE_BLOCK_NUMBER,
                final=True,
                swept=False,
                refund_handling=StrayRefundHandling.RECEIVER_SETTLES,
            )
        ]
        if event_type == "invoice.stray_payment"
        else []
    )
    note = "Test event: nothing was paid"
    is_scheduled = event_type == InvoiceEventType.SCHEDULED_INVOICE_CREATED
    # A scheduled invoice waits a schedule's default lifetime, the longest an invoice may.
    expires_at = created_at + (
        timedelta(seconds=DEFAULT_SCHEDULED_INVOICE_LIFETIME_SECONDS) if is_scheduled else timedelta(minutes=30)
    )
    sample_metadata: dict[str, JsonValue] = {"test": True, "plan": "premium"}
    # A scheduled invoice is made at its period's start.
    schedule_period_start = created_at.replace(microsecond=0) if is_scheduled else None
    order_id = f"{SAMPLE_SCHEDULED_ORDER_ID_PREFIX}-{created_at:%Y%m%d}" if is_scheduled else SAMPLE_ORDER_ID
    return InvoiceAccountResponse(
        id=SAMPLE_INVOICE_ID,
        url=f"{public_base_url}/i/{SAMPLE_INVOICE_ID}",
        status=status,
        partial=False,
        late_payment=event_type == "invoice.late_payment",
        sweep_blocked=event_type == "invoice.sweep_failed",
        chain=present_chain(chain),
        asset=present_asset(asset),
        assets=[asset.symbol],
        payment_options=[],
        deposit_address=SAMPLE_DEPOSIT_ADDRESS,
        amount_due=format_sample_amount(amount_due),
        amount_received=format_sample_amount(received),
        amount_remaining=format_sample_amount(remaining),
        fiat=InvoiceFiat(
            currency="USD",
            amount=f"{SAMPLE_FIAT_AMOUNT:.2f}",
            usd=f"{SAMPLE_FIAT_AMOUNT:.2f}",
            eur=f"{SAMPLE_FIAT_AMOUNT * SAMPLE_EUR_PER_USD:.2f}",
        ),
        quote=InvoiceQuote(asset_price_usd="1", expires_at=expires_at),
        note=note,
        return_url=None,
        success_url=None,
        expires_at=expires_at,
        underpaid_open_until=None,
        payment_uri=build_payment_uri(
            chain=chain,
            asset=asset,
            deposit_address=SAMPLE_DEPOSIT_ADDRESS,
            amount=int(remaining.scaleb(asset.decimals)),
            note=note,
        ),
        payments=payments,
        sweeps=sweeps,
        refunds=[],
        refund_offer=InvoiceRefundOffer(available=False, reason=refund_offer_reason, senders=[]),
        stray_payments=stray_payments,
        seen=False,
        confirmations=None,
        confirmations_final=None,
        seller_support_contact=None,
        created_at=created_at,
        updated_at=now,
        destination_address=destination_address,
        order_id=order_id,
        metadata=sample_metadata,
        accuracy_percent="1",
        payment_link_id=None,
        invoice_schedule_id=SAMPLE_INVOICE_SCHEDULE_ID if is_scheduled else None,
        invoice_schedule_period_start=schedule_period_start,
        paid_at=now - timedelta(minutes=2) if is_paid else None,
        callback_url=None,
        refreshed_from_invoice_id=None,
        refreshed_into_invoice_id=None,
        monero=None,
    )


def build_sample_static_deposit_address_event_data(
    event_type: str, *, chains_service: ChainsService, destination_address: str, now: datetime
) -> dict[str, JsonValue]:
    """The ``data`` of a static deposit address event: the sample address as it stands after ``event_type`` (one
    5 USD payment counted; a stray coin is a second, uncounted row), and its ``payment`` or ``sweep``. The stray sample
    is native coin, which no Transfer log explains, so it has no sender or transaction hash."""
    chain = choose_sample_chain(chains_service)
    asset = choose_sample_asset(chain)
    native_asset = next((coin for coin in chain.assets if coin.is_native), chain.assets[0])
    is_stray = event_type == StaticDepositAddressEventType.STRAY_PAYMENT
    paid_at = now - timedelta(minutes=2)
    static_deposit_address_response = StaticDepositAddressResponse(
        id=SAMPLE_STATIC_DEPOSIT_ADDRESS_ID,
        customer_reference=SAMPLE_CUSTOMER_REFERENCE,
        chain=present_chain(chain),
        assets=[asset.symbol],
        deposit_address=SAMPLE_DEPOSIT_ADDRESS,
        destination_address=destination_address,
        sweep_blocked=event_type == StaticDepositAddressEventType.SWEEP_FAILED,
        received_usd=SAMPLE_FIAT_AMOUNT,
        payment_count=2 if is_stray else 1,
        last_payment_at=paid_at,
        created_at=now - timedelta(days=3),
        payments=[],
    )
    detail: dict[str, JsonValue]
    match event_type:
        case StaticDepositAddressEventType.PAYMENT_RECEIVED | StaticDepositAddressEventType.STRAY_PAYMENT:
            payment = StaticDepositAddressPayment(
                id=SAMPLE_STATIC_DEPOSIT_PAYMENT_ID + 1 if is_stray else SAMPLE_STATIC_DEPOSIT_PAYMENT_ID,
                asset_symbol=native_asset.symbol if is_stray else asset.symbol,
                amount="12.5" if is_stray else format_sample_amount(SAMPLE_FIAT_AMOUNT),
                amount_usd=None if is_stray else SAMPLE_FIAT_AMOUNT,
                asset_price_usd=None if is_stray else Decimal(1),
                credited=not is_stray,
                uncredited_reason="wrong_coin" if is_stray else None,
                tx_hash=None if is_stray else SAMPLE_PAYMENT_HASH,
                sender=None if is_stray else SAMPLE_PAYER_ADDRESS,
                block_number=SAMPLE_BLOCK_NUMBER,
                created_at=paid_at,
            )
            detail = {"payment": payment.model_dump(mode="json")}
        case StaticDepositAddressEventType.SWEPT:
            detail = {
                "sweep": StaticDepositAddressSweep(tx_hash=SAMPLE_SWEEP_HASH, status="final").model_dump(mode="json")
            }
        case StaticDepositAddressEventType.SWEEP_FAILED:
            detail = {
                "sweep": StaticDepositAddressSweep(tx_hash=SAMPLE_SWEEP_HASH, status="failed").model_dump(mode="json")
            }
        case _:
            raise InvalidInputError(f"event_type: one of {', '.join(SAMPLE_EVENT_TYPES)}")
    return {"static_deposit_address": static_deposit_address_response.model_dump(mode="json"), **detail}


def build_sample_event_data(
    event_type: str, *, chains_service: ChainsService, public_base_url: str, destination_address: str, now: datetime
) -> JsonValue:
    """The ``data`` of a test event: a message for ``webhook.test``, else the sample its event type is about."""
    if event_type == WebhookTestEventType.WEBHOOK_TEST:
        return {"message": "A test event from pay-7331."}
    if event_type in SAMPLE_STATIC_DEPOSIT_ADDRESS_EVENT_TYPES:
        return build_sample_static_deposit_address_event_data(
            event_type, chains_service=chains_service, destination_address=destination_address, now=now
        )
    return build_sample_invoice(
        event_type,
        chains_service=chains_service,
        public_base_url=public_base_url,
        destination_address=destination_address,
        now=now,
    ).model_dump(mode="json")
