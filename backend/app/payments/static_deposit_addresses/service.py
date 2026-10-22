"""StaticDepositAddressService: an account's static deposit addresses (PLAN.md section 17), on one session.

Creating one is idempotent per (account, chain, customer reference): under the account's advisory lock the row is
looked up first, so a repeat (or two at once) answers the address already made, unchanged, and the per-account cap
holds. A new one checks the account's payout address as an invoice's destination (``EvmDepositTargets``), derives its
salt (``deposit_salts.py``) and asks the chain's factory for the address (one ``eth_call``). There is no disable: the
chain cannot be told to refuse money, so every final payment is credited and announced (PLAN.md section 17). Every
read is scoped to the account: another account's id is 404.
"""

import base64
from decimal import Decimal
from typing import Literal, Self

from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.accounts.types import AccountAccess, AccountApiTokenAccess
from app.payments.chains.service import present_chain
from app.payments.invoices.errors import FactoryNotDeployedError
from app.payments.invoices.presenters import decode_cursor
from app.payments.invoices.service import InvoiceService, is_invoice_deposit_address
from app.payments.static_deposit_addresses.configuration import read_static_deposit_address_settings
from app.payments.static_deposit_addresses.deposit_salts import derive_static_deposit_salt
from app.payments.static_deposit_addresses.enums import UncreditedReason
from app.payments.static_deposit_addresses.errors import (
    StaticDepositAddressLimitReachedError,
    StaticDepositAddressNotFoundError,
)
from app.payments.static_deposit_addresses.log_events import StaticDepositAddressCreated
from app.payments.static_deposit_addresses.models import StaticDepositAddress, StaticDepositPayment
from app.payments.static_deposit_addresses.repository import StaticDepositAddressRepository
from app.payments.static_deposit_addresses.schemas import (
    CreateStaticDepositAddressRequest,
    StaticDepositAddressListResponse,
    StaticDepositAddressPayment,
    StaticDepositAddressResponse,
)
from app.payments.static_deposit_addresses.types import StaticDepositAddressTotals
from app.shared import logging as log
from app.shared.amounts import format_base_units
from app.shared.errors import InvalidInputError
from app.shared.identifiers import generate_public_id

logger = log.get_logger(__name__)

STATIC_DEPOSIT_ADDRESSES_PER_PAGE = 50
PAYMENTS_PER_STATIC_DEPOSIT_ADDRESS = 100
NO_TOTALS = StaticDepositAddressTotals(received_usd=Decimal("0.00"), payment_count=0, last_payment_at=None)


def encode_static_deposit_address_cursor(static_deposit_address: StaticDepositAddress) -> str:
    """The same opaque (created_at, id) cursor as the invoice list's (``decode_cursor`` reads both)."""
    return base64.urlsafe_b64encode(
        f"{static_deposit_address.created_at.isoformat()}|{static_deposit_address.id}".encode()
    ).decode()


async def is_gateway_deposit_address(session: AsyncSession, address: str) -> bool:  # noqa: PLR0917  # GatewayDepositAddressCheck's positional signature
    """The gateway's deposit address check (``GatewayDepositAddressCheck``): an invoice's deposit or a static one."""
    return await is_invoice_deposit_address(session, address=address) or await StaticDepositAddressRepository(
        session=session
    ).exists_deposit_address(address)


def present_uncredited_reason(payment: StaticDepositPayment) -> Literal["wrong_coin"] | None:
    return "wrong_coin" if payment.uncredited_reason == UncreditedReason.WRONG_COIN.value else None


class StaticDepositAddressService:
    def __init__(self, *, repository: StaticDepositAddressRepository, invoice_service: InvoiceService) -> None:
        self.repository = repository
        self.invoice_service = invoice_service
        self.chains_service = invoice_service.chains_service
        self.settings = read_static_deposit_address_settings()

    @classmethod
    def on_session(cls, session: AsyncSession, *, invoice_service: InvoiceService) -> Self:
        return cls(repository=StaticDepositAddressRepository(session=session), invoice_service=invoice_service)

    # Presenting

    def present_payment(self, payment: StaticDepositPayment, *, chain_key: str) -> StaticDepositAddressPayment:
        asset = self.chains_service.get_asset(chain_key, symbol=payment.asset_symbol)
        return StaticDepositAddressPayment(
            id=payment.public_id,
            asset_symbol=payment.asset_symbol,
            amount=format_base_units(payment.amount_base_units, decimals=asset.decimals),
            amount_usd=payment.amount_usd,
            asset_price_usd=payment.asset_price_usd,
            credited=payment.credited,
            uncredited_reason=present_uncredited_reason(payment),
            tx_hash=payment.transaction_hash,
            sender=payment.sender,
            block_number=payment.block_number,
            created_at=payment.created_at,
        )

    def present(
        self,
        static_deposit_address: StaticDepositAddress,
        *,
        totals: StaticDepositAddressTotals,
        payments: list[StaticDepositPayment] | None = None,
    ) -> StaticDepositAddressResponse:
        return StaticDepositAddressResponse(
            id=static_deposit_address.id,
            customer_reference=static_deposit_address.customer_reference,
            chain=present_chain(self.chains_service.get_chain(static_deposit_address.chain_key)),
            assets=list(static_deposit_address.assets),
            deposit_address=static_deposit_address.deposit_address,
            destination_address=static_deposit_address.destination_address,
            sweep_blocked=static_deposit_address.sweep_blocked,
            received_usd=totals.received_usd,
            payment_count=totals.payment_count,
            last_payment_at=totals.last_payment_at,
            created_at=static_deposit_address.created_at,
            payments=[
                self.present_payment(payment, chain_key=static_deposit_address.chain_key) for payment in payments or []
            ],
        )

    async def present_with_totals(
        self, static_deposit_address: StaticDepositAddress, *, with_payments: bool
    ) -> StaticDepositAddressResponse:
        totals = (await self.repository.read_totals([static_deposit_address.id])).get(
            static_deposit_address.id, NO_TOTALS
        )
        payments = (
            await self.repository.list_payments(static_deposit_address.id, limit=PAYMENTS_PER_STATIC_DEPOSIT_ADDRESS)
            if with_payments
            else None
        )
        return self.present(static_deposit_address, totals=totals, payments=payments)

    # Account use cases

    async def create_static_deposit_address(
        self,
        creation_request: CreateStaticDepositAddressRequest,
        *,
        account_access: AccountAccess | AccountApiTokenAccess,
    ) -> StaticDepositAddressResponse:
        """The account's address for the customer on the chain: the one already made, or a new one."""
        chain_key = creation_request.chain or self.invoice_service.list_invoiceable_evm_chain_keys()[0]
        chain = self.chains_service.get_evm_chain(chain_key)
        if chain.key not in self.invoice_service.wiring.factory_clients:
            raise FactoryNotDeployedError(f"no deposit factory is configured on {chain.name} yet")
        account_id = account_access.account_id
        await self.repository.lock_account_creations(account_id)
        existing = await self.repository.find_by_customer_reference(
            account_id, chain_key=chain.key, customer_reference=creation_request.customer_reference
        )
        if existing is not None:
            return await self.present_with_totals(existing, with_payments=False)
        if (
            await self.repository.count_account_static_deposit_addresses(account_id)
            >= self.settings.maximum_static_deposit_addresses_per_account
        ):
            raise StaticDepositAddressLimitReachedError(
                f"this account has {self.settings.maximum_static_deposit_addresses_per_account} static deposit "
                "addresses, the most it may have"
            )
        if creation_request.assets is not None:
            for symbol in creation_request.assets:
                self.chains_service.get_asset(chain.key, symbol=symbol)
            asset_symbols = chain.order_accepted_asset_symbols(creation_request.assets)
        else:
            asset_symbols = account_access.accepted_asset_symbols_by_chain.get(
                chain.key, chain.default_accepted_asset_symbols
            )
        destination_address = await self.invoice_service.evm_deposit_targets.check_destination(
            account_access.payout_address,
            chain=chain,
            allow_contract_destination=creation_request.allow_contract_destination,
        )
        deposit_salt = derive_static_deposit_salt(
            account_id=account_id,
            chain_key=chain.key,
            customer_reference=creation_request.customer_reference,
            invoice_salt_seed=self.invoice_service.wiring.invoice_settings.invoice_salt_seed,
        )
        predicted = await self.invoice_service.evm_deposit_targets.predict_deposit(
            destination_address, chain=chain, invoice_salt=deposit_salt
        )
        static_deposit_address = StaticDepositAddress(
            id=generate_public_id("sda_"),
            account_id=account_id,
            chain_key=chain.key,
            customer_reference=creation_request.customer_reference,
            assets=asset_symbols,
            deposit_salt=deposit_salt,
            deposit_address=predicted.deposit_address,
            destination_address=destination_address,
        )
        await self.repository.insert_static_deposit_address(static_deposit_address)
        await self.repository.session.refresh(static_deposit_address)
        log.emit(
            logger,
            event=StaticDepositAddressCreated(
                static_deposit_address_id=static_deposit_address.id,
                account_id=account_id,
                chain_key=chain.key,
                deposit_address=static_deposit_address.deposit_address,
                destination_address=destination_address,
            ),
        )
        return self.present(static_deposit_address, totals=NO_TOTALS)

    async def get_account_static_deposit_address(
        self, static_deposit_address_id: str, *, account_id: int
    ) -> StaticDepositAddress:
        static_deposit_address = await self.repository.get_static_deposit_address(static_deposit_address_id)
        if static_deposit_address is None or static_deposit_address.account_id != account_id:
            raise StaticDepositAddressNotFoundError("No static deposit address with that id.")
        return static_deposit_address

    async def read_static_deposit_address(
        self, static_deposit_address_id: str, *, account_id: int
    ) -> StaticDepositAddressResponse:
        static_deposit_address = await self.get_account_static_deposit_address(
            static_deposit_address_id, account_id=account_id
        )
        return await self.present_with_totals(static_deposit_address, with_payments=True)

    async def list_static_deposit_addresses(
        self,
        account_id: int,
        *,
        chain_key: str | None,
        customer_reference: str | None,
        customer_reference_prefix: str | None,
        cursor: str | None,
    ) -> StaticDepositAddressListResponse:
        if customer_reference is not None and customer_reference_prefix is not None:
            raise InvalidInputError("customer_reference and customer_reference_prefix: give one, not both")
        if chain_key is not None:
            self.chains_service.get_chain(chain_key)
        static_deposit_addresses = await self.repository.list_account_static_deposit_addresses(
            account_id,
            chain_key=chain_key,
            customer_reference=customer_reference,
            customer_reference_prefix=customer_reference_prefix,
            after=decode_cursor(cursor) if cursor else None,
            limit=STATIC_DEPOSIT_ADDRESSES_PER_PAGE + 1,
        )
        page = static_deposit_addresses[:STATIC_DEPOSIT_ADDRESSES_PER_PAGE]
        totals_by_id = await self.repository.read_totals([row.id for row in page])
        return StaticDepositAddressListResponse(
            static_deposit_addresses=[self.present(row, totals=totals_by_id.get(row.id, NO_TOTALS)) for row in page],
            next_cursor=(
                encode_static_deposit_address_cursor(page[-1])
                if len(static_deposit_addresses) > STATIC_DEPOSIT_ADDRESSES_PER_PAGE
                else None
            ),
        )
