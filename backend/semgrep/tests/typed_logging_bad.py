"""Known-bad logging, scanned explicitly by CI's self-test though it lives outside app/."""

import hmac
import logging

from PIL import Image
import structlog
from structlog.contextvars import bind_contextvars

logger = structlog.get_logger(__name__)
standard_logger = logging.getLogger(__name__)
logger.info("Untyped fields", untyped_field="value")
bind_contextvars(invoice_id="inv_x")
logger.new(untyped_field="value")

# Native constructors must not be mistaken for logger context.
Image.new("RGB", (1, 1))
hmac.new(b"synthetic-key", b"synthetic-message", "sha256")
