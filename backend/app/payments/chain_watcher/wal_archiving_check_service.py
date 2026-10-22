"""WalArchivingCheckService: is Postgres still archiving its WAL off the box (2026-10-12)?

Production archives every WAL segment to R2 with WAL-G (``archive_command``, ``archive_timeout`` 60 s;
deploy/docker-compose.production.yml). When the archive cannot be reached, "the ``pg_wal/`` directory will continue to
fill with WAL segment files until the situation is resolved", and a full disk is a PANIC shutdown
(https://www.postgresql.org/docs/18/continuous-archiving.html#BACKUP-ARCHIVING-WAL): about 23 GB a day here. The check
reads only the server's own report (``app/infrastructure/postgres/wal_archiver.py``), so it needs no host access:

- failing: a failed attempt newer than the last success, and no success for ``WAL_ARCHIVING_FAILING_AFTER``. The
  archiver tries a segment 3 times, then again at least every 60 s
  (https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/postmaster/pgarch.c, ``NUM_ARCHIVE_RETRIES``,
  ``PGARCH_AUTOWAKE_INTERVAL``), so a lasting failure keeps ``last_failed_time`` fresh and a blip the next try fixes
  never alerts. With no success on record (a fresh cluster, or statistics lost to a crash), the time since the
  server started stands in for the last success, so a restart's first failed try does not alert either.
- stalled: no failure recorded, but a segment waits and nothing was archived for ``WAL_ARCHIVING_STALLED_AFTER`` (an
  ``archive_command`` hanging on the network records no failure). ``archive_timeout`` switches segments only when
  there was activity (https://www.postgresql.org/docs/18/runtime-config-wal.html#GUC-ARCHIVE-TIMEOUT), so an idle
  server with nothing waiting is not stalled; where the waiting segments cannot be listed, the age alone decides.

``archive_mode`` off (local and e2e stacks) is reported and checks nothing. The watcher runs it with its alert checks
once a minute and ``reconcile`` with its own; both through ``ReconciliationService``.
"""

from datetime import datetime, timedelta
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.postgres.schemas import WalArchiverStatus
from app.infrastructure.postgres.wal_archiver import read_wal_archiver_status
from app.payments.chain_watcher.types import ReconciliationCheckResult, ReconciliationFinding
from app.shared.durations import describe_seconds

WAL_ARCHIVING_FAILING_AFTER = timedelta(minutes=5)
# Fifteen archive_timeout periods: about 15 segments (240 MB) waiting.
WAL_ARCHIVING_STALLED_AFTER = timedelta(minutes=15)
BYTES_PER_MEGABYTE = 1024 * 1024


def describe_age(moment: datetime | None, *, now: datetime) -> str:
    return "never" if moment is None else f"{describe_seconds((now - moment).total_seconds())} ago"


class WalArchivingCheckService:
    def __init__(self, *, session: AsyncSession) -> None:
        self.session = session

    @classmethod
    def on_session(cls, session: AsyncSession) -> Self:
        return cls(session=session)

    async def check_wal_archiving(self) -> ReconciliationCheckResult:
        status = await read_wal_archiver_status(self.session)
        if status.archive_mode == "off":
            return ReconciliationCheckResult(
                check_name="wal_archiving", summary="archive_mode off: WAL is not archived here", findings=[]
            )
        now = status.database_time
        waiting_text = self.describe_waiting_segments(status)
        summary = (
            f"archive_mode {status.archive_mode}; {status.archived_count} archived, last "
            f"{describe_age(status.last_archived_time, now=now)}; {status.failed_count} failed, last "
            f"{describe_age(status.last_failed_time, now=now)}; {waiting_text}"
        )
        since_last_archived = now - (status.last_archived_time or status.server_started_time)
        failing = (
            status.last_failed_time is not None
            and (status.last_archived_time is None or status.last_failed_time > status.last_archived_time)
            and since_last_archived > WAL_ARCHIVING_FAILING_AFTER
        )
        stalled = not failing and status.ready_segment_count != 0 and since_last_archived > WAL_ARCHIVING_STALLED_AFTER
        columns = {
            "last archived": describe_age(status.last_archived_time, now=now),
            "last failure": describe_age(status.last_failed_time, now=now),
            "failed in total": str(status.failed_count),
            "waiting": waiting_text,
        }
        consequence = (
            f"{waiting_text} in pg_wal, which grows until archiving works again (a full disk stops Postgres); the "
            "newest WAL is not off the box. Read the Postgres container's log (wal-g wal-push)."
        )
        findings: list[ReconciliationFinding] = []
        if failing:
            findings.append(
                ReconciliationFinding(
                    problem_key="wal_archiving_failing",
                    alert_kind="wal_archiving_failing",
                    invoice_id=None,
                    message=f"WAL archiving is failing: last success {describe_age(status.last_archived_time, now=now)}"
                    f", last failure {describe_age(status.last_failed_time, now=now)}. {consequence}",
                    columns=columns,
                )
            )
        elif stalled:
            findings.append(
                ReconciliationFinding(
                    problem_key="wal_archiving_stalled",
                    alert_kind="wal_archiving_stalled",
                    invoice_id=None,
                    message="WAL archiving has stalled with no failure recorded (archive_command hanging?): last "
                    f"success {describe_age(status.last_archived_time, now=now)}. {consequence}",
                    columns=columns,
                )
            )
        return ReconciliationCheckResult(check_name="wal_archiving", summary=summary, findings=findings)

    def describe_waiting_segments(self, status: WalArchiverStatus) -> str:
        if status.ready_segment_count is None:
            return "waiting segments not listable by this role"
        megabytes = status.ready_segment_count * status.wal_segment_size_bytes // BYTES_PER_MEGABYTE
        return f"{status.ready_segment_count} segments ({megabytes} MB) waiting"
