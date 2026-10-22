"""InvoiceCreationLimits: who may make one more invoice (PLAN.md section 9), split out of ``service.py``.

A web creation (a browser, signed in or not) is counted against its client network's open-invoice cap
(``PAY_MAXIMUM_OPEN_INVOICES_PER_IP``) under an advisory lock on the network's hash, so a burst cannot pass it; from
IPv6, also against its /48's (``PAY_MAXIMUM_OPEN_INVOICES_PER_IPV6_48``) under a lock on the /48's hash, always taken
after the /64's. A public one (signed out, or a quantity link's payer) is also counted against each offered chain's
``PAY_MAXIMUM_OPEN_PUBLIC_INVOICES_PER_CHAIN``, over the public invoices still waiting for a coin or a payment (each
closes on its own clock; an underpaid one may stay open for good, so it is left out), without a lock: a lock there would queue every public creation on the
chain behind one another, and a burst can pass that cap by at most the creations running at once. An API
token's creation (an account's own server, 2026-10-12) is counted against its account's cap
(``PAY_MAXIMUM_OPEN_INVOICES_PER_ACCOUNT``); the caller holds the account's advisory lock
(``InvoiceRepository.lock_account_invoice_creation``) for it. A schedule's invoice (2026-10-21) is counted the
same way. Neither is screened: only sign-up is.
"""

from datetime import UTC, datetime
import math

from pydantic import BaseModel, ConfigDict, SecretStr

from app.payments.invoices.configuration import InvoiceSettings
from app.payments.invoices.creation_limit_repository import InvoiceCreationLimitRepository
from app.payments.invoices.enums import InvoiceCreationChannel
from app.payments.invoices.errors import PublicInvoiceCapacityReachedError, TooManyOpenInvoicesError
from app.payments.invoices.log_events import PublicInvoiceCapacityRefused
from app.payments.invoices.types import NewInvoice
from app.shared import logging as log
from app.shared.client_network import hash_client_ipv6_48_network, hash_client_network
from app.shared.errors import InvalidInputError

# The watcher closes a lapsed invoice on its next tick (chains.toml `poll_interval_seconds`, 1-2 s): a little room.
WATCHER_CLOSE_MARGIN_SECONDS = 5
logger = log.get_logger(__name__)

# A full chain frees up as public invoices are paid or run out of time; no one invoice's clock says when.
PUBLIC_INVOICE_CAPACITY_RETRY_AFTER_SECONDS = 300


class AdmittedClientNetwork(BaseModel):
    """The client network hashes a web invoice keeps: its /64 (or IPv4 address) and, from IPv6, its /48."""

    model_config = ConfigDict(frozen=True)

    client_ip_hash: str
    client_ipv6_48_hash: str | None


def build_too_many_open_invoices_error(
    soonest_clock_close: datetime | None, *, maximum_open_invoices: int
) -> TooManyOpenInvoicesError:
    """The cap's refusal, with ``Retry-After`` at the moment the first open invoice closes by the clock plus one
    watcher tick, so a signed-out maker (who cannot cancel) is told when a slot frees up (UX-026, 2026-10-11).
    Without such a clock there is no honest time, so no ``Retry-After`` (RFC 6585 section 4 makes it optional)."""
    if soonest_clock_close is None:
        return TooManyOpenInvoicesError(
            f"this network already has {maximum_open_invoices} open invoices; one frees up once it is paid, "
            "cancelled or runs out of time",
        )
    seconds_until_close = math.ceil((soonest_clock_close - datetime.now(UTC)).total_seconds())
    retry_after_seconds = max(0, seconds_until_close) + WATCHER_CLOSE_MARGIN_SECONDS
    return TooManyOpenInvoicesError(
        f"this network already has {maximum_open_invoices} open invoices; the next one runs out of time in "
        f"about {math.ceil(retry_after_seconds / 60)} min",
        retry_after_seconds=retry_after_seconds,
    )


class InvoiceCreationLimits:
    def __init__(
        self,
        *,
        repository: InvoiceCreationLimitRepository,
        invoice_settings: InvoiceSettings,
        ip_hash_salt: SecretStr,
    ) -> None:
        self.repository = repository
        self.invoice_settings = invoice_settings
        self.ip_hash_salt = ip_hash_salt

    async def admit_invoice_creation(
        self, account_id: int | None, *, client_ip_address: str | None, creation_channel: InvoiceCreationChannel
    ) -> AdmittedClientNetwork | None:
        """Refuses past the cap that applies; answers the client network hashes a web invoice keeps (None for an API
        token's or a schedule's: no payer's network made it)."""
        if creation_channel.is_capped_per_account and account_id is not None:
            maximum_open_invoices = self.invoice_settings.maximum_open_invoices_per_account
            if await self.repository.count_open_invoices_for_account(account_id) >= maximum_open_invoices:
                raise TooManyOpenInvoicesError(
                    f"this account already has {maximum_open_invoices} open invoices; one frees up once it is paid, "
                    "cancelled or runs out of time"
                )
            return None
        if client_ip_address is None:
            raise InvalidInputError("a web invoice is counted against the network that made it")
        ip_hash_salt = self.ip_hash_salt.get_secret_value()
        client_ip_hash = hash_client_network(client_ip_address, salt=ip_hash_salt)
        await self.repository.lock_client_ip_hash(client_ip_hash)
        maximum_open_invoices = self.invoice_settings.maximum_open_invoices_per_ip
        if await self.repository.count_open_invoices_for_ip_hash(client_ip_hash) >= maximum_open_invoices:
            raise build_too_many_open_invoices_error(
                await self.repository.find_soonest_clock_close_for_ip_hash(client_ip_hash),
                maximum_open_invoices=maximum_open_invoices,
            )
        client_ipv6_48_hash = hash_client_ipv6_48_network(client_ip_address, salt=ip_hash_salt)
        if client_ipv6_48_hash is not None:
            await self.repository.lock_client_ip_hash(client_ipv6_48_hash)
            maximum_open_invoices = self.invoice_settings.maximum_open_invoices_per_ipv6_48
            if await self.repository.count_open_invoices_for_ipv6_48_hash(client_ipv6_48_hash) >= maximum_open_invoices:
                raise build_too_many_open_invoices_error(
                    await self.repository.find_soonest_clock_close_for_ipv6_48_hash(client_ipv6_48_hash),
                    maximum_open_invoices=maximum_open_invoices,
                )
        return AdmittedClientNetwork(client_ip_hash=client_ip_hash, client_ipv6_48_hash=client_ipv6_48_hash)

    async def admit_public_invoice_on_chains(
        self, new_invoice: NewInvoice, *, chain_keys: list[str], creation_channel: InvoiceCreationChannel
    ) -> None:
        """Refuses a public invoice (signed out, or a quantity link's payer) while any chain it offers already has
        ``PAY_MAXIMUM_OPEN_PUBLIC_INVOICES_PER_CHAIN`` open public invoices; any other invoice passes."""
        is_public_invoice = new_invoice.account_id is None or new_invoice.payment_link_id is not None
        if creation_channel is not InvoiceCreationChannel.WEB or not is_public_invoice:
            return
        maximum_open_public_invoices = self.invoice_settings.maximum_open_public_invoices_per_chain
        open_public_invoices_by_chain = await self.repository.count_open_public_invoices_by_chain(chain_keys)
        for chain_key, waiting_public_invoice_count in open_public_invoices_by_chain.items():
            if waiting_public_invoice_count < maximum_open_public_invoices:
                continue
            log.emit(
                logger,
                event=PublicInvoiceCapacityRefused(
                    chain_key=chain_key, waiting_public_invoice_count=waiting_public_invoice_count
                ),
            )
            raise PublicInvoiceCapacityReachedError(
                "this gateway has as many open public invoices as it takes right now; try again in a few minutes",
                retry_after_seconds=PUBLIC_INVOICE_CAPACITY_RETRY_AFTER_SECONDS,
            )
