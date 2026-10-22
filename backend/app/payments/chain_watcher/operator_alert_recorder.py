"""OperatorAlertRecorder: findings become ``operator.alert`` account events on the operator's account (2026-10-12).

The operator's account is the first ``PAY_OPERATOR_ACCOUNT_IDS`` names. A problem
(``problem_key``) alerts when it opens and, while it lasts, again every ``operator_alert_repeat_hours`` of the operator
account's Discord webhook (1 by default); never again when that is 0 or no webhook is set, since only Discord nags and
/activity keeps the first. Hourly is the default; an operator who finds it too often raises the setting or sets 0.
Its ``operator_alert_problems`` row says when it opened (``created_at``), was last seen and last alerted; one not seen
for the recorder's ``problem_cleared_after`` cleared, so if it comes back it alerts anew. That window is per source,
longer than its gap between sightings of a lasting problem: 20 minutes for the watcher's and the API's minute checks,
3 hours for the host's hourly backup (a hung run blocks the next start for up to its 1 h timeout), a day for the ip-api
key (seen only while visitors are screened). The rows are read and written under a transaction-scoped advisory lock,
so a restart, or the API's workers and the watcher recording at once, does not alert twice. With no operator account
nothing is kept: every due finding is a WARNING log line, the only trace, and ``record-operator-alert`` exits 1. Used
by the watcher's ``OperatorAlertService``, by the API's ``WatcherLivenessService`` (the watcher cannot report its own
death) and by ``python -m app.cli record-operator-alert`` (the host's backup units, ``host_operator_alerts.py``), whose
alerts are about no chain (``chain_key`` None).
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.account_events.enums import AccountEventType
from app.payments.account_events.service import AccountEventService
from app.payments.account_webhooks.discord_service import find_operator_alert_repeat_hours_on_session
from app.payments.accounts.service import AccountService
from app.payments.chain_watcher.log_events import OperatorAlertRaised
from app.payments.chain_watcher.types import OperatorAlertRecording, ReconciliationFinding
from app.shared import logging as log

logger = log.get_logger(__name__)

# The watcher's and the API's checks run once a minute and write a lasting problem every 5 minutes; a deploy or a slow
# tick may skip a few.
OPERATOR_ALERT_PROBLEM_CLEARED_AFTER = timedelta(minutes=20)
# The host's backup timer fires hourly (RandomizedDelaySec=5m), and a hung run (TimeoutStartSec=1h) holds the next.
HOST_OPERATOR_ALERT_PROBLEM_CLEARED_AFTER = timedelta(hours=3)
# The ip-api key's rejection is seen only while a screened lookup left its 10-minute marker: quiet nights are gaps.
IP_API_KEY_ALERT_PROBLEM_CLEARED_AFTER = timedelta(days=1)
# Rows no source has seen this long are deleted: the longest window above.
OPERATOR_ALERT_PROBLEM_FORGOTTEN_AFTER = IP_API_KEY_ALERT_PROBLEM_CLEARED_AFTER
# A process writes a lasting problem's ``last_seen_at`` this often at most, not on every check.
OPERATOR_ALERT_PROBLEM_SEEN_REFRESH_AFTER = timedelta(minutes=5)
# A reminder is due this much early: sightings come up to a refresh, or a timer's random delay, apart, and a strict
# cut-off would push it a whole cycle.
OPERATOR_ALERT_REMINDER_SLACK = timedelta(minutes=10)

type SessionFactory = Callable[[], AsyncSession]


class OperatorAlertRecorder:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        operator_alert_account_id: int | None,
        problem_cleared_after: timedelta = OPERATOR_ALERT_PROBLEM_CLEARED_AFTER,
    ) -> None:
        self.session_factory = session_factory
        self.operator_alert_account_id = operator_alert_account_id
        self.problem_cleared_after = problem_cleared_after
        # Problems this process recorded lately, so a problem that lasts does not query Postgres every check.
        self.last_recorded_monotonic_by_problem_key: dict[str, float] = {}

    async def record_new_findings(
        self, findings: list[ReconciliationFinding], *, chain_key: str | None
    ) -> OperatorAlertRecording:
        """Records each finding with an ``alert_kind`` as seen, and alerts the ones that opened or whose reminder is
        due."""
        now_monotonic = time.monotonic()
        refresh_after_seconds = OPERATOR_ALERT_PROBLEM_SEEN_REFRESH_AFTER.total_seconds()
        self.last_recorded_monotonic_by_problem_key = {
            problem_key: recorded_monotonic
            for problem_key, recorded_monotonic in self.last_recorded_monotonic_by_problem_key.items()
            if now_monotonic - recorded_monotonic < refresh_after_seconds
        }
        due_findings = [
            finding
            for finding in findings
            if finding.alert_kind is not None and finding.problem_key not in self.last_recorded_monotonic_by_problem_key
        ]
        if not due_findings:
            return OperatorAlertRecording(alerted_problem_keys=[], operator_account_found=None)
        async with self.session_factory() as session, session.begin():
            account_event_service = AccountEventService.on_session(session)
            # The API's workers, the watcher and the CLI all record: the read of the problems and their writes must not
            # interleave between them (2026-10-13).
            await account_event_service.lock_operator_alert_recording()
            now = datetime.now(UTC)
            account_id = await AccountService.on_session(session).find_operator_account_id(
                self.operator_alert_account_id
            )
            if account_id is None:
                for finding in due_findings:
                    self.last_recorded_monotonic_by_problem_key[finding.problem_key] = now_monotonic
                    log_operator_alert(finding, chain_key=chain_key, recorded_on_account=False)
                return OperatorAlertRecording(
                    alerted_problem_keys=[finding.problem_key for finding in due_findings], operator_account_found=False
                )
            await account_event_service.delete_operator_alert_problems_last_seen_before(
                now - OPERATOR_ALERT_PROBLEM_FORGOTTEN_AFTER
            )
            repeat_hours = await find_operator_alert_repeat_hours_on_session(session, account_id=account_id) or 0
            reminder_due_after = max(timedelta(hours=repeat_hours) - OPERATOR_ALERT_REMINDER_SLACK, timedelta(0))
            problems_by_key = await account_event_service.list_operator_alert_problems(
                [finding.problem_key for finding in due_findings]
            )
            alerted_problem_keys: list[str] = []
            for finding in due_findings:
                self.last_recorded_monotonic_by_problem_key[finding.problem_key] = now_monotonic
                problem = problems_by_key.get(finding.problem_key)
                if problem is None:
                    problem = await account_event_service.insert_operator_alert_problem(
                        finding.problem_key, seen_at=now
                    )
                    problems_by_key[finding.problem_key] = problem
                    is_reminder = False
                elif now - problem.last_seen_at > self.problem_cleared_after:
                    # It cleared and came back: a new problem.
                    problem.created_at = problem.last_seen_at = problem.last_alerted_at = now
                    is_reminder = False
                else:
                    problem.last_seen_at = now
                    if repeat_hours == 0 or now - problem.last_alerted_at < reminder_due_after:
                        continue
                    problem.last_alerted_at = now
                    is_reminder = True
                await self.record_operator_alert(
                    finding,
                    first_seen_at=problem.created_at,
                    is_reminder=is_reminder,
                    chain_key=chain_key,
                    account_id=account_id,
                    service=account_event_service,
                )
                alerted_problem_keys.append(finding.problem_key)
        return OperatorAlertRecording(
            alerted_problem_keys=alerted_problem_keys, operator_account_found=account_id is not None
        )

    async def record_operator_alert(
        self,
        finding: ReconciliationFinding,
        *,
        first_seen_at: datetime,
        is_reminder: bool,
        chain_key: str | None,
        account_id: int,
        service: AccountEventService,
    ) -> None:
        log_operator_alert(finding, chain_key=chain_key, recorded_on_account=True)
        await service.record_account_event(
            None,
            account_id=account_id,
            event_type=AccountEventType.OPERATOR_ALERT,
            data={
                "kind": finding.alert_kind or "",
                "message": finding.message,
                "problem_key": finding.problem_key,
                "chain_key": chain_key,
                "invoice_id": finding.invoice_id,
                "first_seen_at": first_seen_at.isoformat(),
                "is_reminder": is_reminder,
            },
        )


def log_operator_alert(finding: ReconciliationFinding, *, chain_key: str | None, recorded_on_account: bool) -> None:
    log.emit(
        logger,
        event=OperatorAlertRaised(
            chain_key=chain_key,
            alert_kind=finding.alert_kind or "",
            problem_key=finding.problem_key,
            recorded_on_account=recorded_on_account,
        ),
    )
