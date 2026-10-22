"""The prefix every API route sits under and the path parameter types more than one router takes."""

from typing import Annotated

from fastapi import Path

# On each domain router rather than on an enclosing router, so a matched route's ``path`` is the full template the
# request log writes (``app.shared.request.route_template``).
API_PREFIX = "/api/v1"
# Public ids: a prefix and 22 base58 characters (app/shared/identifiers.py).
InvoiceIdPath = Annotated[str, Path(pattern=r"^inv_[1-9A-HJ-NP-Za-km-z]{22}$")]
# The passkey routes sit on two routers (accounts, and account_events for the ones that record an event).
AccountPasskeyIdPath = Annotated[str, Path(pattern=r"^pk_[1-9A-HJ-NP-Za-km-z]{22}$")]
