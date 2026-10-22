"""What the server itself reports about its WAL archiving (``wal_archiver.py``)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class WalArchiverStatus(BaseModel):
    """``archive_mode`` and the single row of ``pg_stat_archiver``
    (https://www.postgresql.org/docs/18/monitoring-stats.html#MONITORING-PG-STAT-ARCHIVER-VIEW), read with the server's
    own clock so the ages need no clock of this process."""

    model_config = ConfigDict(frozen=True)

    # off, on or always (https://www.postgresql.org/docs/18/runtime-config-wal.html#GUC-ARCHIVE-MODE).
    archive_mode: str
    archived_count: int
    last_archived_wal: str | None
    last_archived_time: datetime | None
    failed_count: int
    last_failed_wal: str | None
    last_failed_time: datetime | None
    # Completed segments the archiver has not archived yet (`.ready` files in pg_wal/archive_status); None when this
    # role may not list that directory.
    ready_segment_count: int | None
    # 16 MB unless initdb chose otherwise (https://www.postgresql.org/docs/18/runtime-config-preset.html).
    wal_segment_size_bytes: int
    # https://www.postgresql.org/docs/18/functions-info.html (pg_postmaster_start_time).
    server_started_time: datetime
    database_time: datetime
