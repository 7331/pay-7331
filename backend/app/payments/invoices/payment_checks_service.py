"""PaymentChecksService: the payer's "I paid" (PLAN.md section 7.1).

Without a hash it rereads the invoice's deposit at once, in every configured asset, at ``latest`` and at the
finalized block (one Multicall3 ``aggregate3`` each, as the watcher reads balances), and says what shows: a rise in the
invoice's coin (confirming), money already counted, another coin not final yet, another coin already recorded, or
nothing. Another coin that is final but not recorded answers nothing: the payer is then asked for the hash, and the
hash records it.

With a hash it reads the transaction over the chain's endpoints (``transaction_readings``) and says what the chain
shows of it for this deposit. The check never credits an invoice: crediting stays the watcher's, from finalized
balances. The one write is stray money in a listed coin, final, into this deposit, recorded exactly as the operator's
``record-stray-payment-from-transaction`` does, after which the automatic refund rules apply. No nudge to the watcher:
it reads every open deposit every tick (``poll_interval_seconds``, 2 s on Polygon), so a confirming payment counts on
the tick after it is final anyway.

An XMR invoice is checked from its ``monero_payments`` rows and, given a txid, by the scanner sidecar
(``monero_payment_checks.py``; PLAN.md section 14).

The route is a public write, so the API's per-IP budget for ``POST /api/v1/public/*`` (10 a minute) limits how often
one client fans out these RPC reads.
"""

from typing import Self

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.evm_rpc_api.client import (
    EvmRpcEndpoint,
    decode_uint256,
    encode_native_balance_call,
    encode_token_balance_call,
)
from app.infrastructure.evm_rpc_api.errors import EvmRpcUnavailableError
from app.infrastructure.evm_rpc_api.schemas import Multicall3Result
from app.payments.chains.types import AssetConfiguration, EvmChainConfiguration, MoneroChainConfiguration
from app.payments.invoices.chain_readings_service import ChainReadingsService
from app.payments.invoices.enums import ChainTransactionState, PaymentCheckOutcome
from app.payments.invoices.errors import InvoiceStateError
from app.payments.invoices.log_events import InvoicePaymentChecked
from app.payments.invoices.models import Invoice, require_deposit_address
from app.payments.invoices.monero_payment_checks import check_monero_invoice_payment
from app.payments.invoices.repository import InvoiceRepository
from app.payments.invoices.schemas import PaymentCheckResponse
from app.payments.invoices.transaction_readings import normalize_transaction_hash, read_chain_transaction
from app.payments.invoices.types import ChainTransactionReading, PaymentCheckResult
from app.payments.invoices.wiring import InvoiceWiring
from app.shared import logging as log
from app.shared.amounts import format_base_units
from app.shared.errors import ServiceUnavailableError

logger = log.get_logger(__name__)

TRANSACTION_STATE_OUTCOMES = {
    ChainTransactionState.UNKNOWN: PaymentCheckOutcome.TRANSACTION_UNKNOWN,
    ChainTransactionState.NOT_MINED: PaymentCheckOutcome.TRANSACTION_NOT_MINED,
    ChainTransactionState.FAILED: PaymentCheckOutcome.TRANSACTION_FAILED,
}


def read_balance(result: Multicall3Result) -> int:
    """A balance call's answer; a failed call (a token without code on this fork, say) reads as nothing held."""
    return decode_uint256(result.return_data) if result.succeeded else 0


class PaymentChecksService:
    def __init__(self, *, repository: InvoiceRepository, chain_readings_service: ChainReadingsService) -> None:
        self.repository = repository
        self.chain_readings_service = chain_readings_service
        self.invoice_service = chain_readings_service.invoice_service

    @classmethod
    def on_session(cls, session: AsyncSession, *, redis: Redis, wiring: InvoiceWiring) -> Self:
        return cls(
            repository=InvoiceRepository(session=session),
            chain_readings_service=ChainReadingsService.on_session(session, redis=redis, wiring=wiring),
        )

    async def check_invoice_payment(
        self, invoice_id: str, *, transaction_hash_text: str | None
    ) -> PaymentCheckResponse:
        invoice = await self.invoice_service.get_invoice(invoice_id)
        if invoice.asset_symbol is None:
            raise InvoiceStateError("the invoice has no payment address yet; pick a coin first")
        chain = self.invoice_service.chains_service.get_chain(invoice.chain_key)
        if isinstance(chain, MoneroChainConfiguration):
            result = await check_monero_invoice_payment(
                invoice,
                chain=chain,
                transaction_hash_text=transaction_hash_text,
                repository=self.repository,
                scanner_reader=self.invoice_service.wiring.monero_sidecar,
            )
        else:
            result = await self.check_evm_invoice_payment(
                invoice,
                chain=self.invoice_service.chains_service.get_evm_chain(chain.key),
                transaction_hash_text=transaction_hash_text,
            )
        log.emit(
            logger,
            event=InvoicePaymentChecked(
                invoice_id=invoice.id,
                outcome=result.outcome.value,
                with_transaction_hash=transaction_hash_text is not None,
            ),
        )
        asset = chain.find_asset(result.asset_symbol) if result.asset_symbol is not None else None
        return PaymentCheckResponse(
            outcome=result.outcome.value,
            asset_symbol=result.asset_symbol,
            amount=format_base_units(result.amount_base_units, decimals=asset.decimals)
            if asset is not None and result.amount_base_units is not None
            else None,
            invoice=await self.invoice_service.read_public_invoice(invoice.id),
        )

    async def check_evm_invoice_payment(
        self, invoice: Invoice, *, chain: EvmChainConfiguration, transaction_hash_text: str | None
    ) -> PaymentCheckResult:
        try:
            if transaction_hash_text is None:
                return await self.reread_deposit(invoice, chain=chain)
            return await self.check_transaction(invoice, chain=chain, transaction_hash_text=transaction_hash_text)
        except EvmRpcUnavailableError as exc:
            raise ServiceUnavailableError("the chain could not be reached; try again shortly") from exc

    # The deposit, reread at once

    async def reread_deposit(self, invoice: Invoice, *, chain: EvmChainConfiguration) -> PaymentCheckResult:
        """Fresh first: a rise in the invoice's coin, then another coin not final yet, then what is counted, then
        another coin already recorded. Each asset's gross is its balance plus what finalized sweeps moved out, and
        another coin's is net of what its stray rows explain."""
        deposit_address = require_deposit_address(invoice)
        balance_calls = [
            encode_native_balance_call(deposit_address)
            if asset.token_address is None
            else encode_token_balance_call(asset.token_address, holder_address=deposit_address)
            for asset in chain.assets
        ]

        async def read_balances(endpoint: EvmRpcEndpoint) -> tuple[list[Multicall3Result], list[Multicall3Result]]:
            block_heads = await endpoint.read_block_heads()
            latest = await endpoint.aggregate3(balance_calls, block_hash=block_heads.latest.hash)
            finalized = await endpoint.aggregate3(balance_calls, block_hash=block_heads.finalized.hash)
            return latest, finalized

        latest_results, finalized_results = await self.invoice_service.wiring.rpc_clients[chain.key].call_with_failover(
            read_balances
        )
        swept_by_token: dict[str | None, int] = {}
        for event in await self.repository.list_sweep_events(chain.key, deposit_addresses=[deposit_address]):
            swept_by_token[event.token_address] = swept_by_token.get(event.token_address, 0) + event.amount_base_units
        stray_payments = await self.repository.list_stray_payments([invoice.id])
        stray_by_symbol: dict[str, int] = {}
        for stray_payment in stray_payments:
            stray_by_symbol[stray_payment.asset_symbol] = (
                stray_by_symbol.get(stray_payment.asset_symbol, 0) + stray_payment.amount_base_units
            )
        wrong_coin_confirming: PaymentCheckResult | None = None
        for asset, latest_result, finalized_result in zip(chain.assets, latest_results, finalized_results, strict=True):
            swept = swept_by_token.get(asset.token_address, 0)
            gross_latest = read_balance(latest_result) + swept
            if asset.symbol == invoice.asset_symbol:
                if gross_latest > invoice.received_base_units:
                    return PaymentCheckResult(
                        outcome=PaymentCheckOutcome.PAYMENT_CONFIRMING,
                        asset_symbol=asset.symbol,
                        amount_base_units=gross_latest - invoice.received_base_units,
                    )
                continue
            unexplained_latest = gross_latest - stray_by_symbol.get(asset.symbol, 0)
            unexplained_finalized = read_balance(finalized_result) + swept - stray_by_symbol.get(asset.symbol, 0)
            if wrong_coin_confirming is None and unexplained_latest > 0 and unexplained_finalized <= 0:
                wrong_coin_confirming = PaymentCheckResult(
                    outcome=PaymentCheckOutcome.WRONG_COIN_CONFIRMING,
                    asset_symbol=asset.symbol,
                    amount_base_units=unexplained_latest,
                )
        if wrong_coin_confirming is not None:
            return wrong_coin_confirming
        if invoice.received_base_units > 0:
            return PaymentCheckResult(
                outcome=PaymentCheckOutcome.PAYMENT_COUNTED,
                asset_symbol=invoice.asset_symbol,
                amount_base_units=invoice.received_base_units,
            )
        if stray_payments:
            newest = stray_payments[-1]
            return PaymentCheckResult(
                outcome=PaymentCheckOutcome.WRONG_COIN_RECORDED,
                asset_symbol=newest.asset_symbol,
                amount_base_units=newest.amount_base_units,
            )
        return PaymentCheckResult(outcome=PaymentCheckOutcome.NOTHING_FOUND)

    # A transaction hash

    async def check_transaction(
        self, invoice: Invoice, *, chain: EvmChainConfiguration, transaction_hash_text: str
    ) -> PaymentCheckResult:
        transaction_hash = normalize_transaction_hash(transaction_hash_text)
        if transaction_hash is None:
            return PaymentCheckResult(outcome=PaymentCheckOutcome.HASH_MALFORMED)
        reading = await read_chain_transaction(
            transaction_hash, rpc_client=self.invoice_service.wiring.rpc_clients[chain.key], chain=chain
        )
        if reading.state != ChainTransactionState.SUCCEEDED:
            return PaymentCheckResult(outcome=TRANSACTION_STATE_OUTCOMES[reading.state])
        deposit_address = require_deposit_address(invoice).lower()
        into_deposit = [transfer for transfer in reading.transfers if transfer.recipient.lower() == deposit_address]
        in_invoice_coin = [transfer for transfer in into_deposit if transfer.asset_symbol == invoice.asset_symbol]
        if in_invoice_coin:
            return await self.judge_invoice_coin_payment(
                invoice,
                reading=reading,
                amount_base_units=sum(transfer.amount_base_units for transfer in in_invoice_coin),
            )
        in_other_listed_coin = [transfer for transfer in into_deposit if transfer.asset_symbol is not None]
        if in_other_listed_coin:
            symbol = in_other_listed_coin[0].asset_symbol
            amount = sum(
                transfer.amount_base_units for transfer in in_other_listed_coin if transfer.asset_symbol == symbol
            )
            if not reading.final:
                return PaymentCheckResult(
                    outcome=PaymentCheckOutcome.WRONG_COIN_CONFIRMING, asset_symbol=symbol, amount_base_units=amount
                )
            # Only this invoice's deposit: what the transaction did elsewhere is none of this payer's business.
            await self.chain_readings_service.record_stray_transfers(
                reading, chain_key=chain.key, invoice_ids=[invoice.id]
            )
            return PaymentCheckResult(
                outcome=PaymentCheckOutcome.WRONG_COIN_RECORDED, asset_symbol=symbol, amount_base_units=amount
            )
        if into_deposit:
            return PaymentCheckResult(outcome=PaymentCheckOutcome.UNLISTED_TOKEN)
        return PaymentCheckResult(outcome=PaymentCheckOutcome.OTHER_ADDRESS)

    async def judge_invoice_coin_payment(
        self, invoice: Invoice, *, reading: ChainTransactionReading, amount_base_units: int
    ) -> PaymentCheckResult:
        """Counted once the watcher has its final row and a credit; money after the invoice closed is late and never
        counted; anything else counts on the watcher's tick after it is final."""
        asset: AssetConfiguration | None = self.invoice_service.chains_service.get_chain(invoice.chain_key).find_asset(
            invoice.asset_symbol or ""
        )
        symbol = asset.symbol if asset is not None else invoice.asset_symbol
        payment_rows = [
            payment
            for payment in await self.repository.list_payments([invoice.id])
            if (payment.transaction_hash or "").lower() == reading.transaction_hash.lower()
        ]
        if reading.final and invoice.received_base_units > 0 and any(payment.final for payment in payment_rows):
            outcome = PaymentCheckOutcome.PAYMENT_COUNTED
        elif reading.final and invoice.closed_at is not None and not payment_rows:
            outcome = PaymentCheckOutcome.PAYMENT_AFTER_CLOSE
        else:
            outcome = PaymentCheckOutcome.PAYMENT_CONFIRMING
        return PaymentCheckResult(outcome=outcome, asset_symbol=symbol, amount_base_units=amount_base_units)
