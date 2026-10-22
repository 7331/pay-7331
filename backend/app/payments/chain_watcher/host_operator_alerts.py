"""Operator alerts the host raises, not the watcher: its systemd backup units failing (2026-10-12).

``pay-7331-backup-failed.service`` (the backup's ``OnFailure=``) and the restore drill's ``ExecStopPost=`` run
``docker exec pay-7331-api python -m app.cli record-operator-alert --kind ...``, which records the alert through
``OperatorAlertRecorder`` like every other (once per kind while it lasts, then at the reminder cadence; shown on the
operator's /activity), beside the marker file and journal line they already leave. The message is fixed per kind here,
so no host text with a secret reaches /activity; ``--detail`` adds only systemd's result words.
"""

from enum import StrEnum

from app.payments.chain_watcher.types import ReconciliationFinding

# systemd's $MONITOR_SERVICE_RESULT / $SERVICE_RESULT, $..._EXIT_CODE and $..._EXIT_STATUS words, e.g.
# "result=exit-code code=exited status=1" (https://github.com/systemd/systemd/blob/main/man/systemd.exec.xml).
HOST_OPERATOR_ALERT_DETAIL_PATTERN = r"^[A-Za-z0-9=._:-]+( [A-Za-z0-9=._:-]+)*$"
HOST_OPERATOR_ALERT_DETAIL_MAXIMUM_LENGTH = 120


class HostOperatorAlertKind(StrEnum):
    BACKUP_FAILED = "backup_failed"
    BACKUP_RESTORE_DRILL_FAILED = "backup_restore_drill_failed"


HOST_OPERATOR_ALERT_MESSAGES: dict[HostOperatorAlertKind, str] = {
    HostOperatorAlertKind.BACKUP_FAILED: "The hourly Postgres backup did not complete, so the newest dump or the WAL "
    "archive check is not proven in R2. On the host: `journalctl -u pay-7331-backup` says why; "
    "/var/lib/pay-7331/backup-failed stays until a backup succeeds.",
    HostOperatorAlertKind.BACKUP_RESTORE_DRILL_FAILED: "The weekly backup restore drill did not pass: the newest "
    "backup may not restore. On the host: `journalctl -u pay-7331-backup-verify` says why; "
    "/var/lib/pay-7331/backup-verify-failed stays until a drill passes.",
}


def build_host_operator_alert_finding(kind: HostOperatorAlertKind, *, detail: str | None) -> ReconciliationFinding:
    message = HOST_OPERATOR_ALERT_MESSAGES[kind]
    return ReconciliationFinding(
        problem_key=kind.value,
        alert_kind=kind.value,
        invoice_id=None,
        message=f"{message} ({detail})" if detail else message,
        columns={"kind": kind.value, "detail": detail or ""},
    )
