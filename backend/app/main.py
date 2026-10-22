"""The API process: ``uvicorn app.main:app``. Importing it reads only the log level; the lifespan opens everything
else, so a missing setting fails the start, not a request."""

from fastapi import FastAPI

from app.config import read_logging_settings
from app.lifespan.lifespan import lifespan
from app.openapi_contract import install_openapi_contract, name_operation_after_handler
from app.payments.account_events.router import account_events_router
from app.payments.account_webhooks.router import account_webhooks_router
from app.payments.accounts.router import accounts_router
from app.payments.chains.router import chains_router
from app.payments.invoice_schedules.router import invoice_schedules_router
from app.payments.invoices.monero_wallet_router import monero_wallet_router
from app.payments.invoices.router import invoices_router
from app.payments.pages.router import pages_router, static_files
from app.payments.payment_links.router import payment_links_router
from app.payments.price_quotes.router import price_quotes_router
from app.payments.static_deposit_addresses.router import static_deposit_addresses_router
from app.rate_limit_middleware import RateLimitRequestsMiddleware
from app.request_log_middleware import RequestLogMiddleware
from app.shared.correlation import ServerMintedCorrelationIdMiddleware
from app.shared.exception_handlers import register_exception_handlers
from app.shared.logging import configure_logging
from app.shared.path_params import API_PREFIX
from app.unhandled_error_middleware import UnhandledErrorMiddleware


async def read_health() -> dict[str, str]:
    """200 while the process runs; checks nothing else."""
    return {"status": "ok"}


def create_app() -> FastAPI:
    configure_logging(read_logging_settings().log_level)
    # The OpenAPI document is not served; tools/dump_openapi.py writes it out.
    application = FastAPI(
        title="pay-7331",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        generate_unique_id_function=name_operation_after_handler,
    )
    # HEAD for uptime checkers; the server sends no body for it (2026-10-09). Left out of the OpenAPI document: one
    # handler on two methods would give two operations one id.
    application.add_api_route(f"{API_PREFIX}/health", read_health, methods=["GET", "HEAD"], include_in_schema=False)
    for domain_router in (
        invoices_router,
        monero_wallet_router,
        payment_links_router,
        static_deposit_addresses_router,
        invoice_schedules_router,
        account_events_router,
        accounts_router,
        chains_router,
        price_quotes_router,
        account_webhooks_router,
        pages_router,
    ):
        application.include_router(domain_router)
    application.mount("/static", static_files, name="static")
    # Added last runs first: the correlation id is set before the request line, the unhandled-error answer and the
    # rate limiter run.
    application.add_middleware(RateLimitRequestsMiddleware)
    application.add_middleware(UnhandledErrorMiddleware)
    application.add_middleware(RequestLogMiddleware)
    application.add_middleware(ServerMintedCorrelationIdMiddleware)
    register_exception_handlers(application)
    install_openapi_contract(application)
    return application


app = create_app()
