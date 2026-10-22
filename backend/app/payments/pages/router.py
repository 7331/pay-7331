"""The pages from the frontend's Vite build, frontend/dist (PLAN.md sections 7 and 10): every page route (``/``,
``/i/{anything}``, ``/l/{anything}``, ``/new``, ``/activity``, ``/settings``, ``/docs``, ``/enroll-passkey``) answers the one ``index.html`` and the app routes on the
client; any other path outside ``/api`` and ``/static`` answers it too, with 404, so the app's own not-found page shows
instead of JSON. ``/static/`` serves the build's assets and public files (Vite ``base: "/static/"``). The pages fetch
the JSON API."""

from functools import lru_cache
import os
from os import PathLike
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.convertors import Convertor, register_url_convertor
from starlette.types import Scope

from app.config import read_application_settings
from app.shared.errors import NotFoundError

# backend/app/payments/pages -> the repository root; the image keeps the same layout. `pnpm build` in frontend/ (or the
# image's node stage) writes it.
FRONTEND_BUILD_DIRECTORY = Path(__file__).resolve().parents[4] / "frontend" / "dist"
# Scripts only from this origin (no inline script, so no javascript: URL runs), no framing (2026-10-09).
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
PAGE_SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
PAGE_HEADERS = {"Cache-Control": "no-cache", **PAGE_SECURITY_HEADERS}

# HEAD as well as GET: uptime checkers and link previews send HEAD. FastAPI's APIRoute does not add HEAD to a GET
# route the way Starlette's Route does; FileResponse then sends the same headers and no body (2026-10-09).
PAGE_METHODS = ["GET", "HEAD"]

pages_router = APIRouter(tags=["pages"], include_in_schema=False)


class UnknownPagePathConvertor(Convertor[str]):
    """Any path except ``/api`` and ``/static`` and what is under them, so an unknown API route keeps its JSON 404 and
    the static mount keeps its files (https://www.starlette.io/routing/#path-parameters)."""

    regex = r"(?!(?:api|static)(?:/|$)).*"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("unknown_page_path", UnknownPagePathConvertor())


def app_page_response(*, status_code: int = 200) -> Response:
    page_path = FRONTEND_BUILD_DIRECTORY / "index.html"
    if not page_path.is_file():
        raise NotFoundError("the frontend is not built into this deployment")
    if read_application_settings().sandbox:
        return Response(
            read_sandbox_page_html(page_path),
            status_code=status_code,
            media_type="text/html",
            headers=PAGE_HEADERS,
        )
    return FileResponse(page_path, status_code=status_code, media_type="text/html", headers=PAGE_HEADERS)


# The sandbox (PLAN.md section 16) marks its page with a meta tag the app reads before it renders
# (frontend/src/lib/deployment_mode.ts), so the banner needs no request and no inline script (the CSP allows none).
SANDBOX_PAGE_MARKER = '<meta name="pay-deployment" content="sandbox" />'


@lru_cache(maxsize=1)
def read_sandbox_page_html(page_path: Path) -> bytes:
    page_html = page_path.read_text()
    if "</head>" not in page_html:
        raise RuntimeError(f"{page_path} has no </head> to put the sandbox marker before")
    return page_html.replace("</head>", f"    {SANDBOX_PAGE_MARKER}\n  </head>", 1).encode()


@pages_router.api_route("/", methods=PAGE_METHODS)
async def serve_quick_invoice_page() -> Response:
    return app_page_response()


# Any id, valid or not: the app asks the API and says "Can't show this invoice" for a truncated or unknown one.
@pages_router.api_route("/i/{invoice_reference}", methods=PAGE_METHODS)
async def serve_invoice_page(invoice_reference: str) -> Response:  # noqa: ARG001  # the app reads it
    return app_page_response()


# A quantity link (payments/payment_links), any id like /i/: the app says it can't show an unknown one.
@pages_router.api_route("/l/{payment_link_reference}", methods=PAGE_METHODS)
async def serve_payment_link_page(payment_link_reference: str) -> Response:  # noqa: ARG001  # the app reads it
    return app_page_response()


@pages_router.api_route("/new", methods=PAGE_METHODS)
async def serve_new_invoice_page() -> Response:
    return app_page_response()


@pages_router.api_route("/activity", methods=PAGE_METHODS)
async def serve_account_activity_page() -> Response:
    return app_page_response()


@pages_router.api_route("/settings", methods=PAGE_METHODS)
async def serve_account_settings_page() -> Response:
    return app_page_response()


# The passkey enrollment link (accounts/passkey_enrollment_link_service.py); its token is in the fragment.
@pages_router.api_route("/enroll-passkey", methods=PAGE_METHODS)
async def serve_passkey_enrollment_page() -> Response:
    return app_page_response()


@pages_router.api_route("/docs", methods=PAGE_METHODS)
async def serve_developer_docs_page() -> Response:
    return app_page_response()


# Last on the last router included: the app's not-found page, with a 404 status for crawlers and link previews.
@pages_router.api_route("/{unknown_page_path:unknown_page_path}", methods=PAGE_METHODS)
async def serve_unknown_page(unknown_page_path: str) -> Response:  # noqa: ARG001  # the app shows the path
    return app_page_response(status_code=404)


# Vite names every built script, style and font after a hash of its content under `assets/` (its default
# `build.assetsDir`, https://vite.dev/config/build-options#build-assetsdir), so such a name never changes content.
HASHED_BUILD_ASSET_DIRECTORY = FRONTEND_BUILD_DIRECTORY / "assets"
# A year without revalidating (https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Cache-Control#immutable).
HASHED_BUILD_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"


class RevalidatedStaticFiles(StaticFiles):
    """``/static`` with ``Cache-Control: no-cache`` (revalidate by ETag), so a deploy's pages and scripts change together,
    except the hashed build assets under ``/static/assets/``, which a browser keeps for a year: a new build names new
    files and the revalidated ``index.html`` points at them (2026-10-12). A Cloudflare cache rule bypasses the edge for
    ``pay.7331.org/static/``; the browser still follows this header.

    Every file here also carries the pages' security headers: ``/static/index.html`` is the app itself and must not be
    framed or run under a looser policy than ``/``, and an SVG opened directly is a document too. A response's CSP
    governs only the document or worker it creates (CSP Level 3, sections 4.1 and 4.2, "Integration with Fetch" and
    "Integration with HTML", https://www.w3.org/TR/CSP3/), not the page that loads a script, style or image, so the
    headers cost the assets nothing."""

    def file_response(  # noqa: PLR0917  # Starlette's signature
        self, full_path: PathLike[str] | str, stat_result: os.stat_result, scope: Scope, status_code: int = 200
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        is_hashed_build_asset = Path(full_path).resolve().is_relative_to(HASHED_BUILD_ASSET_DIRECTORY)
        response.headers["Cache-Control"] = HASHED_BUILD_ASSET_CACHE_CONTROL if is_hashed_build_asset else "no-cache"
        response.headers.update(PAGE_SECURITY_HEADERS)
        return response


# check_dir=False: a checkout without a build still starts; /static/ then answers 404.
static_files = RevalidatedStaticFiles(directory=FRONTEND_BUILD_DIRECTORY, check_dir=False)
