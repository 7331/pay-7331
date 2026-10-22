"""IpApiKeyAlertService: the API alerts the operator once ip-api rejects the sign-up screening key (2026-10-13).

The pro key is prepaid and "has to be manually renewed" (https://members.ip-api.com/faq); once it expires, every
lookup fails and screening admits every sign-up unchecked (or, with ``PAY_SCREENING_FAIL_CLOSED``, refuses every
sign-up). Screening leaves a Redis marker on each rejection (``sign_up_screening/ip_api_key_rejection_service.py``); once a
minute (``IP_API_KEY_CHECK_INTERVAL_SECONDS``) the API reads it and records an ``operator.alert`` of kind
``ip_api_key_rejected`` through ``OperatorAlertRecorder`` (once while it lasts, then at the reminder cadence), which the operator's /activity and
Discord show. Redis not answering is logged and checks nothing.
"""

from collections.abc import Callable
import re

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.chain_watcher.log_events import IpApiKeyRejectionUnreadable
from app.payments.chain_watcher.operator_alert_recorder import (
    IP_API_KEY_ALERT_PROBLEM_CLEARED_AFTER,
    OperatorAlertRecorder,
)
from app.payments.chain_watcher.types import ReconciliationFinding
from app.payments.sign_up_screening.ip_api_key_rejection_service import IpApiKeyRejectionService
from app.shared import logging as log

logger = log.get_logger(__name__)

IP_API_MESSAGE_MARKUP_PATTERN = re.compile(r"[\[\]<>`]")

IP_API_KEY_CHECK_INTERVAL_SECONDS = 60
IP_API_KEY_REJECTED_ALERT_KIND = "ip_api_key_rejected"

type SessionFactory = Callable[[], AsyncSession]


class IpApiKeyAlertService:
    def __init__(self, *, redis: Redis, session_factory: SessionFactory, operator_alert_account_id: int | None) -> None:
        self.ip_api_key_rejection_service = IpApiKeyRejectionService.on_redis(redis)
        self.operator_alert_recorder = OperatorAlertRecorder(
            session_factory=session_factory,
            operator_alert_account_id=operator_alert_account_id,
            problem_cleared_after=IP_API_KEY_ALERT_PROBLEM_CLEARED_AFTER,
        )

    async def check_ip_api_key_rejection(self) -> None:
        """One pass: an alert when ip-api rejected the screening key within the marker's TTL."""
        try:
            ip_api_message = await self.ip_api_key_rejection_service.find_recent_ip_api_key_rejection()
        except RedisError:
            log.emit(logger, event=IpApiKeyRejectionUnreadable())
            return
        if ip_api_message is None:
            return
        # ip-api's own words, not ours: without brackets and backticks no masked link, tag or code span renders in
        # Discord, where an operator alert's text is not escaped (discord_messages.py).
        ip_api_text = IP_API_MESSAGE_MARKUP_PATTERN.sub("", ip_api_message)
        ip_api_says = f' (ip-api says "{ip_api_text}")' if ip_api_text else ""
        await self.operator_alert_recorder.record_new_findings(
            [
                ReconciliationFinding(
                    problem_key=IP_API_KEY_REJECTED_ALERT_KIND,
                    alert_kind=IP_API_KEY_REJECTED_ALERT_KIND,
                    invoice_id=None,
                    message=f"ip-api rejected the sign-up screening key{ip_api_says}: until PAY_IP_API_KEY is renewed "
                    "or replaced in the deploy .env, screening admits every sign-up unchecked (or refuses every "
                    "sign-up if PAY_SCREENING_FAIL_CLOSED is on).",
                    columns={"kind": IP_API_KEY_REJECTED_ALERT_KIND},
                )
            ],
            chain_key=None,
        )
