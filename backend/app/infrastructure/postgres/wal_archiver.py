"""Read the server's WAL archiver state over an ordinary session: no host or container access (2026-10-12).

``pg_stat_archiver`` is readable by any role. The waiting segments come from ``pg_ls_archive_statusdir()``, "restricted
to superusers and members of the ``pg_monitor`` role by default"
(https://www.postgresql.org/docs/18/functions-admin.html#FUNCTIONS-ADMIN-GENFILE-TABLE); the compose stacks connect as
the image's superuser, and another role gets ``None`` instead of an error that would abort the caller's transaction.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.postgres.schemas import WalArchiverStatus

WAL_ARCHIVER_STATUS_QUERY = text(
    "SELECT current_setting('archive_mode') AS archive_mode, archived_count, last_archived_wal, last_archived_time, "
    "failed_count, last_failed_wal, last_failed_time, "
    "pg_size_bytes(current_setting('wal_segment_size')) AS wal_segment_size_bytes, "
    "pg_postmaster_start_time() AS server_started_time, clock_timestamp() AS database_time "
    "FROM pg_stat_archiver"
)
CAN_LIST_ARCHIVE_STATUS_QUERY = text("SELECT has_function_privilege('pg_ls_archive_statusdir()', 'EXECUTE')")
# A segment waiting for the archiver has `<segment>.ready` there and the archiver scans for that suffix (not in the
# manual; https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/postmaster/pgarch.c, pgarch_readyXlog).
READY_SEGMENT_COUNT_QUERY = text("SELECT count(*) FROM pg_ls_archive_statusdir() WHERE name LIKE '%.ready'")


async def read_wal_archiver_status(session: AsyncSession) -> WalArchiverStatus:
    row = (await session.execute(WAL_ARCHIVER_STATUS_QUERY)).mappings().one()
    ready_segment_count: int | None = None
    if await session.scalar(CAN_LIST_ARCHIVE_STATUS_QUERY):
        ready_segment_count = int(await session.scalar(READY_SEGMENT_COUNT_QUERY) or 0)
    return WalArchiverStatus(**row, ready_segment_count=ready_segment_count)
