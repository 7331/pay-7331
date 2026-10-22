"""``PayClient``: the blocking client, on ``httpx.Client`` (https://www.python-httpx.org/advanced/clients/).

One client per process is enough: it keeps connections alive. Close it with ``close()`` or use it as a context
manager. Nothing is retried: reads are safe to repeat, and a create is safe to repeat only with an ``order_id`` (an
invoice) or a ``customer_reference`` (a customer address), which make the API answer the one already made.
"""

from collections.abc import Generator, Iterator, Mapping, Sequence
from contextlib import closing
from datetime import datetime
import sys
from types import TracebackType
from typing import IO

import httpx
from pydantic import JsonValue

from pay_7331._api_requests import (
    DEFAULT_TIMEOUT_SECONDS,
    PRODUCTION_BASE_URL,
    AmountValue,
    ChainListResponse,
    ModelType,
    PaymentLinkListResponse,
    RateListResponse,
    build_api_error,
    build_client_settings,
    build_invoice_body,
    build_payment_link_body,
    build_query,
    build_resource_path,
    build_static_deposit_address_body,
    build_transport_error,
    parse_response,
)

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self
from pay_7331.models import (
    AccountEvent,
    AccountEventPage,
    Chain,
    ExchangeRate,
    Invoice,
    InvoicePage,
    PaymentLink,
    PaymentLinkWithInvoices,
    StaticDepositAddress,
    StaticDepositAddressPage,
)


class PayClient:
    """The pay-7331 API with an account's API token.

    ``api_token`` is the ``pay_tok_...`` token made in Settings; read it from your own secret store and pass it in (the
    SDK reads no environment variable). ``base_url`` is production by default; the sandbox is
    ``https://pay-sandbox.7331.org`` (``pay_7331.SANDBOX_BASE_URL``), with its own accounts and tokens. ``timeout`` is
    seconds for connecting, each read and each write (httpx's ``Timeout``)."""

    def __init__(
        self,
        api_token: str,
        *,
        base_url: str = PRODUCTION_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        settings = build_client_settings(api_token=api_token, base_url=base_url, timeout=timeout)
        # Only to redact it from a transport error's message.
        self._api_token = settings.api_token
        self._http_client = httpx.Client(base_url=settings.base_url, headers=settings.headers, timeout=settings.timeout)

    def __enter__(self) -> Self:
        return self

    def __exit__(  # noqa: PLR0917  # the context manager protocol fixes it
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http_client.close()

    def _request(
        self,
        *,
        method: str,
        path: str,
        model: type[ModelType],
        params: Mapping[str, str | int] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> ModelType:
        try:
            response = self._http_client.request(method, path, params=params, json=json_body)
        except httpx.RequestError as error:
            transport_error = build_transport_error(
                error, request_description=f"{method} {path}", api_token=self._api_token
            )
        else:
            return parse_response(response, model=model)
        # Raised outside the except block, so the httpx error is neither its __cause__ nor its __context__.
        raise transport_error

    # ---- Invoices ----

    def create_invoice(
        self,
        *,
        amount: AmountValue,
        currency: str,
        chain: str | None = None,
        chains: Sequence[str] | None = None,
        assets: Sequence[str] | None = None,
        assets_by_chain: Mapping[str, Sequence[str]] | None = None,
        destination_address: str | None = None,
        allow_contract_destination: bool = False,
        note: str | None = None,
        order_id: str | None = None,
        refresh_expired: bool = False,
        lifetime_seconds: int | None = None,
        accuracy_percent: AmountValue | None = None,
        underpaid_open_until: datetime | None = None,
        return_url: str | None = None,
        success_url: str | None = None,
        callback_url: str | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> Invoice:
        """``POST /invoices``. ``amount`` and ``currency`` (USD, EUR or a coin symbol) alone make an invoice on every
        live network in your saved coins. Reusing an ``order_id`` returns that order's invoice unchanged; with
        ``refresh_expired=True`` an expired, unpaid one is replaced by a new invoice. ``callback_url`` needs the
        account webhook (409 ``webhook_not_set`` otherwise). ``metadata`` is private JSON, back in reads and
        webhooks."""
        body = build_invoice_body(
            amount=amount,
            currency=currency,
            chain=chain,
            chains=chains,
            assets=assets,
            assets_by_chain=assets_by_chain,
            destination_address=destination_address,
            allow_contract_destination=allow_contract_destination,
            note=note,
            order_id=order_id,
            refresh_expired=refresh_expired,
            lifetime_seconds=lifetime_seconds,
            accuracy_percent=accuracy_percent,
            underpaid_open_until=underpaid_open_until,
            return_url=return_url,
            success_url=success_url,
            callback_url=callback_url,
            metadata=metadata,
        )
        return self._request(method="POST", path="/invoices", model=Invoice, json_body=body)

    def read_invoice(self, invoice_id: str) -> Invoice:
        """``GET /invoices/{id}``; another account's invoice is ``PayNotFoundError``."""
        return self._request(method="GET", path=build_resource_path("/invoices", resource_id=invoice_id), model=Invoice)

    def list_invoices(
        self, *, status: str | None = None, order_id_prefix: str | None = None, cursor: str | None = None
    ) -> InvoicePage:
        """``GET /invoices``: one page, newest first. ``iterate_invoices`` follows the cursor for you."""
        params = build_query(status=status, order_id_prefix=order_id_prefix, cursor=cursor)
        return self._request(method="GET", path="/invoices", model=InvoicePage, params=params)

    def iterate_invoices(self, *, status: str | None = None, order_id_prefix: str | None = None) -> Iterator[Invoice]:
        """Every invoice matching the filters, newest first, one page request at a time."""
        cursor: str | None = None
        while True:
            page = self.list_invoices(status=status, order_id_prefix=order_id_prefix, cursor=cursor)
            yield from page.invoices
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

    def find_invoice_by_order_id(self, order_id: str) -> Invoice | None:
        """``GET /invoices?order_id=``: the invoice made with that order id, or None. After a create whose answer was
        lost, this tells whether it was made."""
        page = self._request(method="GET", path="/invoices", model=InvoicePage, params=build_query(order_id=order_id))
        return page.invoices[0] if page.invoices else None

    def cancel_invoice(self, invoice_id: str) -> Invoice:
        """``POST /invoices/{id}/cancel``: only with nothing received, else ``PayConflictError``."""
        return self._request(
            method="POST", path=build_resource_path("/invoices", resource_id=invoice_id, action="cancel"), model=Invoice
        )

    def accept_partial_payment(self, invoice_id: str) -> Invoice:
        """``POST /invoices/{id}/accept-partial``: an ``underpaid`` invoice becomes paid with ``partial`` true."""
        return self._request(
            method="POST",
            path=build_resource_path("/invoices", resource_id=invoice_id, action="accept-partial"),
            model=Invoice,
        )

    def iterate_invoice_export_csv(
        self,
        *,
        status: str | None = None,
        created_from: datetime | None = None,
        created_before: datetime | None = None,
    ) -> Generator[bytes, None, None]:
        """``GET /invoices/export.csv`` as it streams, in byte chunks, so a large export never sits in memory.
        ``created_from`` is inclusive, ``created_before`` exclusive; a naive time is UTC. Read it to the end (or
        close the iterator) to free the connection."""
        params = build_query(status=status, created_from=created_from, created_before=created_before)
        try:
            with self._http_client.stream("GET", "/invoices/export.csv", params=params) as response:
                if not response.is_success:
                    response.read()
                    raise build_api_error(response)
                yield from response.iter_bytes()
        except httpx.RequestError as error:
            transport_error = build_transport_error(
                error, request_description="GET /invoices/export.csv", api_token=self._api_token
            )
        else:
            return
        raise transport_error

    def export_invoices_csv(
        self,
        destination: IO[bytes],
        *,
        status: str | None = None,
        created_from: datetime | None = None,
        created_before: datetime | None = None,
    ) -> int:
        """Writes the CSV export into ``destination`` (a file opened ``"wb"``); returns the bytes written."""
        written_byte_count = 0
        chunks = self.iterate_invoice_export_csv(
            status=status, created_from=created_from, created_before=created_before
        )
        with closing(chunks):
            for chunk in chunks:
                destination.write(chunk)
                written_byte_count += len(chunk)
        return written_byte_count

    # ---- Quantity links ----

    def create_payment_link(
        self,
        *,
        amount: AmountValue,
        currency: str,
        quantity: int,
        chain: str | None = None,
        chains: Sequence[str] | None = None,
        assets: Sequence[str] | None = None,
        assets_by_chain: Mapping[str, Sequence[str]] | None = None,
        destination_address: str | None = None,
        allow_contract_destination: bool = False,
        note: str | None = None,
        lifetime_seconds: int | None = None,
        accuracy_percent: AmountValue | None = None,
        return_url: str | None = None,
    ) -> PaymentLink:
        """``POST /payment-links``: one link up to ``quantity`` (1-1000) payers pay, in USD or EUR."""
        body = build_payment_link_body(
            amount=amount,
            currency=currency,
            quantity=quantity,
            chain=chain,
            chains=chains,
            assets=assets,
            assets_by_chain=assets_by_chain,
            destination_address=destination_address,
            allow_contract_destination=allow_contract_destination,
            note=note,
            lifetime_seconds=lifetime_seconds,
            accuracy_percent=accuracy_percent,
            return_url=return_url,
        )
        return self._request(method="POST", path="/payment-links", model=PaymentLink, json_body=body)

    def list_payment_links(self) -> list[PaymentLink]:
        """``GET /payment-links``: your newest 100."""
        return self._request(method="GET", path="/payment-links", model=PaymentLinkListResponse).payment_links

    def read_payment_link(self, payment_link_id: str) -> PaymentLinkWithInvoices:
        """``GET /payment-links/{id}`` with its newest 200 invoices."""
        path = build_resource_path("/payment-links", resource_id=payment_link_id)
        return self._request(method="GET", path=path, model=PaymentLinkWithInvoices)

    def close_payment_link(self, payment_link_id: str) -> PaymentLink:
        """``POST /payment-links/{id}/close``: no new payers; invoices already made run their course."""
        path = build_resource_path("/payment-links", resource_id=payment_link_id, action="close")
        return self._request(method="POST", path=path, model=PaymentLink)

    # ---- Customer (static deposit) addresses ----

    def create_static_deposit_address(
        self,
        *,
        customer_reference: str,
        chain: str | None = None,
        assets: Sequence[str] | None = None,
        allow_contract_destination: bool = False,
    ) -> StaticDepositAddress:
        """``POST /static-deposit-addresses``: the customer's permanent address on ``chain`` (default the first
        network ``list_chains`` lists). The same reference on the same chain returns the same address, so a retry is
        safe."""
        body = build_static_deposit_address_body(
            customer_reference=customer_reference,
            chain=chain,
            assets=assets,
            allow_contract_destination=allow_contract_destination,
        )
        return self._request(
            method="POST", path="/static-deposit-addresses", model=StaticDepositAddress, json_body=body
        )

    def read_static_deposit_address(self, static_deposit_address_id: str) -> StaticDepositAddress:
        """``GET /static-deposit-addresses/{id}`` with its newest 100 payments."""
        path = build_resource_path("/static-deposit-addresses", resource_id=static_deposit_address_id)
        return self._request(method="GET", path=path, model=StaticDepositAddress)

    def list_static_deposit_addresses(
        self,
        *,
        customer_reference: str | None = None,
        customer_reference_prefix: str | None = None,
        chain: str | None = None,
        cursor: str | None = None,
    ) -> StaticDepositAddressPage:
        """``GET /static-deposit-addresses``: one page of 50, newest first, ``payments`` empty."""
        params = build_query(
            customer_reference=customer_reference,
            customer_reference_prefix=customer_reference_prefix,
            chain=chain,
            cursor=cursor,
        )
        return self._request(
            method="GET", path="/static-deposit-addresses", model=StaticDepositAddressPage, params=params
        )

    def iterate_static_deposit_addresses(
        self,
        *,
        customer_reference: str | None = None,
        customer_reference_prefix: str | None = None,
        chain: str | None = None,
    ) -> Iterator[StaticDepositAddress]:
        cursor: str | None = None
        while True:
            page = self.list_static_deposit_addresses(
                customer_reference=customer_reference,
                customer_reference_prefix=customer_reference_prefix,
                chain=chain,
                cursor=cursor,
            )
            yield from page.static_deposit_addresses
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

    # ---- Events ----

    def list_events(self, *, limit: int | None = None, cursor: str | None = None) -> AccountEventPage:
        """``GET /events``: newest first, ``limit`` 1-200 (the API's default 50); exactly the events your webhook
        gets."""
        return self._request(
            method="GET", path="/events", model=AccountEventPage, params=build_query(limit=limit, cursor=cursor)
        )

    def iterate_events(self, *, page_size: int | None = None) -> Iterator[AccountEvent]:
        """Every event, newest first, back to the first."""
        cursor: str | None = None
        while True:
            page = self.list_events(limit=page_size, cursor=cursor)
            yield from page.events
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

    # ---- Public reads ----

    def list_chains(self) -> list[Chain]:
        """``GET /chains``: the networks a new invoice can use, with their coins."""
        return self._request(method="GET", path="/chains", model=ChainListResponse).chains

    def list_exchange_rates(self) -> list[ExchangeRate]:
        """``GET /rates``: each coin's USD and EUR price as a new quote would use it now. A guide, not a quote."""
        return self._request(method="GET", path="/rates", model=RateListResponse).rates
