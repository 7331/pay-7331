#!/usr/bin/env bash
# Weekly restore drill: the newest pay-7331 Postgres backup, restored into a THROWAWAY container and
# checked against production.
#
# The hourly run proves the upload (sha256 of the uploaded bytes). This proves the rest: the key on this
# box still opens the archive, gpg's integrity check passes, pg_restore loads it, and the restored data is
# production's schema and rows. Then the dead-disk case: the newest WAL-G base backup plus every archived WAL
# segment, replayed in a second throwaway container, must reach production's state as of the drill's WAL switch.
# Production gets SELECTs, one restore point and one WAL switch; the bucket is only read.
#
#   deploy/verify_postgres_backup_restore.sh                     # newest completed backup
#   deploy/verify_postgres_backup_restore.sh 20261012T042000Z    # one exact stamp
#
# Exit 0 means: the newest manifest was found; the archive downloaded with the manifest's size and sha256;
# gpg decrypted it with PAY_BACKUP_KEY_FILE and its integrity check passed (gpg exits non-zero on a
# failed check unless --ignore-mdc-error, https://www.gnupg.org/documentation/manuals/gnupg/GPG-Esoteric-Options.html);
# pg_restore --exit-on-error loaded it into postgres:18 (https://www.postgresql.org/docs/18/app-pgrestore.html);
# the restored alembic revision equals production's; and each checked table's restored row count equals
# the manifest's count (taken just after the dump, so the archive may be a few rows behind) within
# PAY_BACKUP_VERIFY_DUMP_SLACK rows, and is no more than that slack above production's count now.
# The newest backup is also refused when older than PAY_BACKUP_VERIFY_MAX_AGE_HOURS. The WAL part passes when the
# newest full base backup is under PAY_BACKUP_VERIFY_MAX_BASE_BACKUP_AGE_HOURS old, the replay finished, its alembic
# revision is production's, its last replayed commit is within PAY_BACKUP_VERIFY_MAX_REPLAY_LAG_SECONDS of the switch,
# and its row counts match production's just before the switch within the same slack.
# A drill right after a deploy that migrated fails on the alembic check until the next nightly backup;
# run deploy/backup_postgres_to_r2.sh by hand after such a deploy.
set -euo pipefail
umask 077

wanted_stamp="${1:-}"
script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Host paths: from deploy/host_paths.env when run from a checkout, from the units' EnvironmentFile= under systemd.
set -a
# shellcheck disable=SC1091
[[ -r "${script_directory}/host_paths.env" ]] && . "${script_directory}/host_paths.env"
set +a
key_file="${PAY_BACKUP_KEY_FILE:?set PAY_BACKUP_KEY_FILE}"
python_interpreter="${PAY_BACKUP_PYTHON:?set PAY_BACKUP_PYTHON}"
storage_credentials_file="${PAY_BACKUP_STORAGE_CREDENTIALS_FILE:?set PAY_BACKUP_STORAGE_CREDENTIALS_FILE}"
production_postgres_container="${PAY_POSTGRES_CONTAINER:-pay-7331-postgres}"
restore_image="${PAY_BACKUP_RESTORE_IMAGE:-postgres:18}"
# Rows written between pg_dump's snapshot and the manifest's count query, or deleted since (payments and
# stray rows are moved by the watcher).
dump_slack_rows="${PAY_BACKUP_VERIFY_DUMP_SLACK:-5}"
# The newest backup must be recent: a drill that keeps passing on an old archive while the nightly run
# has stopped would hide the gap. Two nights plus slack; not applied when a stamp is named.
maximum_backup_age_hours="${PAY_BACKUP_VERIFY_MAX_AGE_HOURS:-3}"
# A test run points this at a throwaway prefix; production never sets it.
bucket_prefix="${PAY_BACKUP_BUCKET_PREFIX:-backups/pay-7331/postgres}"
wal_archive_settings_file="${PAY_WAL_ARCHIVE_SETTINGS_FILE:?set PAY_WAL_ARCHIVE_SETTINGS_FILE}"
# Daily base backups (backup_postgres_to_r2.sh) plus slack for a missed run.
maximum_full_base_backup_age_hours="${PAY_BACKUP_VERIFY_MAX_BASE_BACKUP_AGE_HOURS:-26}"
# The replay must reach production's last commits before the switch: archive_timeout is 60 s, the watcher commits every
# few seconds.
maximum_replay_lag_seconds="${PAY_BACKUP_VERIFY_MAX_REPLAY_LAG_SECONDS:-300}"
wal_restore_timeout_seconds="${PAY_BACKUP_VERIFY_WAL_RESTORE_TIMEOUT_SECONDS:-900}"
wal_archive_wait_seconds="${PAY_WAL_ARCHIVE_WAIT_SECONDS:-180}"
restore_container="pay-7331-restore-check-$$-$(date -u +%s)"
wal_restore_container="pay-7331-wal-restore-check-$$-$(date -u +%s)"
checked_tables=(invoices invoice_payments outgoing_operations invoice_stray_payments static_deposit_addresses static_deposit_payments)
# Checked since 2026-10-21: a backup made before then has no manifest count for them, and only for them.
tables_new_to_the_manifest=(static_deposit_addresses static_deposit_payments)

fail() { printf 'restore check failed: %s\n' "$1" >&2; exit 1; }

work_directory="$(mktemp -d)"
remove_throwaway_state() {
  local exit_status=$?
  trap - EXIT INT TERM
  docker rm --force --volumes "$restore_container" "$wal_restore_container" >/dev/null 2>&1 || true
  rm -rf -- "$work_directory"
  exit "$exit_status"
}
trap remove_throwaway_state EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -m 0700 "$work_directory/gnupg"

# The same credentials as the backup: only the BACKUP_STORAGE_* lines, never echoed or in argv.
[ -r "$storage_credentials_file" ] || fail "cannot read $storage_credentials_file"
set -a
# shellcheck disable=SC1090
. <(grep -E '^BACKUP_STORAGE_(ENDPOINT_URL|ACCESS_KEY|SECRET_KEY|BUCKET)=' "$storage_credentials_file")
set +a
: "${BACKUP_STORAGE_ENDPOINT_URL:?BACKUP_STORAGE_ENDPOINT_URL is unset}"
: "${BACKUP_STORAGE_ACCESS_KEY:?BACKUP_STORAGE_ACCESS_KEY is unset}"
: "${BACKUP_STORAGE_SECRET_KEY:?BACKUP_STORAGE_SECRET_KEY is unset}"
: "${BACKUP_STORAGE_BUCKET:?BACKUP_STORAGE_BUCKET is unset}"
[ -r "$key_file" ] || fail "no readable backup key at $key_file"
[ -x "$python_interpreter" ] || fail "no python at $python_interpreter"
command -v gpg >/dev/null || fail "gpg is missing"

# Newest manifest (or the wanted stamp's), then its archive, checked against the manifest's size and sha256.
WORK_DIRECTORY="$work_directory" WANTED_STAMP="$wanted_stamp" BUCKET_PREFIX="$bucket_prefix" \
  "$python_interpreter" - <<'DOWNLOAD'
import hashlib
import json
import os
from pathlib import Path

import boto3

client = boto3.client(
    "s3",
    endpoint_url=os.environ["BACKUP_STORAGE_ENDPOINT_URL"],
    aws_access_key_id=os.environ["BACKUP_STORAGE_ACCESS_KEY"],
    aws_secret_access_key=os.environ["BACKUP_STORAGE_SECRET_KEY"],
    region_name="auto",
)
bucket = os.environ["BACKUP_STORAGE_BUCKET"]
work = Path(os.environ["WORK_DIRECTORY"])
wanted = os.environ["WANTED_STAMP"]
manifest_keys = [
    entry["Key"]
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=os.environ["BUCKET_PREFIX"] + "/")
    for entry in page.get("Contents", [])
    if entry["Key"].endswith(".manifest.json") and (not wanted or f"-{wanted}." in entry["Key"])
]
if not manifest_keys:
    raise SystemExit("no completed backup found" + (f" for stamp {wanted}" if wanted else ""))
manifest_key = sorted(manifest_keys)[-1]
manifest = json.loads(client.get_object(Bucket=bucket, Key=manifest_key)["Body"].read())
(work / "manifest.json").write_text(json.dumps(manifest))
archive_path = work / "archive.dump.gpg"
client.download_file(bucket, manifest["archive_key"], str(archive_path))
with archive_path.open("rb") as stream:
    digest = hashlib.file_digest(stream, "sha256").hexdigest()
if digest != manifest["sha256"] or archive_path.stat().st_size != manifest["size"]:
    raise SystemExit("downloaded archive does not match its manifest's size and sha256")
print(f"archive {manifest['archive_key']}: {manifest['size']} bytes, sha256 matches the manifest")
DOWNLOAD

read_manifest_value() {
  WORK_DIRECTORY="$work_directory" "$python_interpreter" -c '
import json, os, pathlib, sys
value = json.loads((pathlib.Path(os.environ["WORK_DIRECTORY"]) / "manifest.json").read_text())
for part in sys.argv[1:]:
    value = value[part]
print(value)' "$@"
}

gpg --homedir "$work_directory/gnupg" --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$key_file" \
  --decrypt --output "$work_directory/archive.dump" "$work_directory/archive.dump.gpg" 2>/dev/null || {
  rm -f -- "$work_directory/archive.dump"
  fail "archive did not decrypt with $key_file, or failed its integrity check"
}
backup_stamp="$(read_manifest_value stamp)"
if [ -z "$wanted_stamp" ]; then
  backup_epoch="$(date -u -d "$(echo "$backup_stamp" | sed -E 's/^(....)(..)(..)T(..)(..)(..)Z$/\1-\2-\3 \4:\5:\6/')" +%s)"
  backup_age_hours=$(( ($(date -u +%s) - backup_epoch) / 3600 ))
  [ "$backup_age_hours" -le "$maximum_backup_age_hours" ] ||
    fail "the newest backup ($backup_stamp) is $backup_age_hours hours old; the hourly run has stopped"
fi
printf 'decrypted: %s bytes, integrity check passed\n' "$(stat -c %s "$work_directory/archive.dump")"

# Throwaway server: no network at all (loopback only), trust auth (nothing can reach it), anonymous
# volume removed with it. Readiness over 127.0.0.1: the image's init-time server listens on the socket
# only, so a TCP answer is the final server.
docker run --detach --rm --network none --name "$restore_container" \
  --env POSTGRES_HOST_AUTH_METHOD=trust --env POSTGRES_USER=pay --env POSTGRES_DB=pay \
  "$restore_image" >/dev/null || fail "could not start the throwaway $restore_image container"
for _ in $(seq 1 60); do
  docker exec "$restore_container" pg_isready -h 127.0.0.1 -U pay -d pay >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$restore_container" pg_isready -h 127.0.0.1 -U pay -d pay >/dev/null 2>&1 ||
  fail "the throwaway Postgres did not become ready within a minute"

docker exec -i "$restore_container" pg_restore -h 127.0.0.1 -U pay -d pay --no-owner --no-privileges --exit-on-error \
  < "$work_directory/archive.dump" > "$work_directory/restore.log" 2>&1 || {
  tail -n 5 "$work_directory/restore.log" >&2
  fail "pg_restore did not load the archive"
}

query_restored() { docker exec "$restore_container" psql -h 127.0.0.1 -U pay -d pay -v ON_ERROR_STOP=1 -tAc "$1" | tr -d '\r'; }
read_production_environment_value() {
  docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$production_postgres_container" |
    sed -n "s/^$1=//p" | head -n 1
}
production_user="$(read_production_environment_value POSTGRES_USER)"
production_database="$(read_production_environment_value POSTGRES_DB)"
[[ -n "$production_user" && -n "$production_database" ]] ||
  fail "POSTGRES_USER or POSTGRES_DB missing in $production_postgres_container"
query_production() {
  docker exec "$production_postgres_container" psql -U "$production_user" -d "$production_database" \
    -v ON_ERROR_STOP=1 -tAc "$1" | tr -d '\r'
}

restored_revision="$(query_restored 'SELECT version_num FROM alembic_version')"
production_revision="$(query_production 'SELECT version_num FROM alembic_version')"
[ -n "$restored_revision" ] || fail "restored database has no alembic revision"
[ "$restored_revision" = "$production_revision" ] ||
  fail "restored alembic revision $restored_revision differs from production's $production_revision"
printf 'alembic: restored %s = production %s\n' "$restored_revision" "$production_revision"

for table_name in "${checked_tables[@]}"; do
  restored_count="$(query_restored "SELECT count(*) FROM $table_name")"
  production_count="$(query_production "SELECT count(*) FROM $table_name")"
  if [[ " ${tables_new_to_the_manifest[*]} " == *" $table_name "* ]]; then
    manifest_count="$(read_manifest_value row_counts_after_dump "$table_name" 2>/dev/null || printf 'none')"
  else
    manifest_count="$(read_manifest_value row_counts_after_dump "$table_name")"
  fi
  printf '%s: restored %s, manifest %s, production now %s\n' \
    "$table_name" "$restored_count" "$manifest_count" "$production_count"
  if [ "$manifest_count" != none ]; then
    (( manifest_count - restored_count <= dump_slack_rows && restored_count - manifest_count <= dump_slack_rows )) ||
      fail "$table_name: restored $restored_count rows, the manifest recorded $manifest_count"
  fi
  [ $((restored_count - production_count)) -le "$dump_slack_rows" ] ||
    fail "$table_name: restored $restored_count rows, more than production's $production_count"
done

printf 'dump restore check passed: stamp %s, alembic %s\n' "$backup_stamp" "$restored_revision"

# Part two, the dead-disk case: restore the newest WAL-G base backup and replay the archived WAL to its end, in a
# second throwaway container, and check it reaches production's state of a minute ago.
[ -r "$wal_archive_settings_file" ] || fail "no WAL archive settings at $wal_archive_settings_file (deploy/deploy.sh writes them)"
command -v jq >/dev/null || fail "jq is missing"
production_image="$(docker inspect --format '{{.Config.Image}}' "$production_postgres_container")"
newest_base_backup_epoch="$(docker exec -u postgres "$production_postgres_container" wal-g backup-list --json --detail |
  jq '[.[]?.finish_time | sub("\\.[0-9]+"; "") | fromdateiso8601] | max // 0')"
base_backup_age_hours=$(( ($(date -u +%s) - newest_base_backup_epoch) / 3600 ))
[ "$base_backup_age_hours" -le "$maximum_full_base_backup_age_hours" ] ||
  fail "the newest WAL-G base backup is $base_backup_age_hours hours old; the daily base backup has stopped"
# Production's counts first, then close its WAL segment and wait until it is archived: the replay below must then hold
# at least these rows. The drill's only writes to production: one restore point record and one WAL switch (as the
# backup script's check, which cites the docs).
declare -A production_count_before_switch
for table_name in "${checked_tables[@]}"; do
  production_count_before_switch[$table_name]="$(query_production "SELECT count(*) FROM $table_name")"
done
wal_check_epoch="$(date -u +%s)"
query_production "SELECT pg_create_restore_point('pay-7331 restore drill')" >/dev/null
switched_wal_segment="$(query_production 'SELECT pg_walfile_name(pg_switch_wal())')"
wal_archive_deadline=$(( $(date -u +%s) + wal_archive_wait_seconds ))
while [[ "$(query_production "SELECT coalesce(last_archived_wal, '') FROM pg_stat_archiver")" < "$switched_wal_segment" ]]; do
  [ "$(date -u +%s)" -lt "$wal_archive_deadline" ] ||
    fail "production's WAL segment $switched_wal_segment was not archived within ${wal_archive_wait_seconds}s"
  sleep 2
done

# The production image (Postgres plus WAL-G) and the production WAL-G settings, run as `postgres`. It needs the network
# to read R2, so not --network none; nothing is published. backup-fetch LATEST, then recovery.signal and a
# restore_command make the server replay archived WAL until wal-fetch finds no more, then promote
# (https://www.postgresql.org/docs/18/continuous-archiving.html#BACKUP-PITR-RECOVERY;
# https://github.com/wal-g/wal-g/blob/master/docs/PostgreSQL.md). archive_mode=off: a promoted copy must never push
# its WAL into production's prefix.
docker run --detach --name "$wal_restore_container" --user postgres --env-file "$wal_archive_settings_file" \
  --entrypoint sh "$production_image" -c \
  'mkdir -p -m 0700 "$PGDATA" && wal-g backup-fetch "$PGDATA" LATEST && touch "$PGDATA/recovery.signal" &&
   exec docker-entrypoint.sh postgres -c archive_mode=off -c "restore_command=wal-g wal-fetch %f %p"' \
  >/dev/null || fail "could not start the throwaway WAL restore container"
wal_restore_deadline=$(( $(date -u +%s) + wal_restore_timeout_seconds ))
until [ "$(docker exec "$wal_restore_container" psql -U pay -d pay -tAc 'SELECT NOT pg_is_in_recovery()' 2>/dev/null |
  tr -d '\r')" = t ]; do
  [ "$(docker inspect --format '{{.State.Running}}' "$wal_restore_container" 2>/dev/null)" = true ] || {
    docker logs --tail 20 "$wal_restore_container" >&2 2>&1 || true
    fail "the WAL restore container stopped before recovery finished"
  }
  [ "$(date -u +%s)" -lt "$wal_restore_deadline" ] ||
    fail "WAL replay did not finish within $wal_restore_timeout_seconds seconds"
  sleep 2
done
query_wal_restored() {
  docker exec "$wal_restore_container" psql -U pay -d pay -v ON_ERROR_STOP=1 -tAc "$1" | tr -d '\r'
}

wal_restored_revision="$(query_wal_restored 'SELECT version_num FROM alembic_version')"
[ "$wal_restored_revision" = "$production_revision" ] ||
  fail "WAL restore: alembic revision $wal_restored_revision differs from production's $production_revision"
# "If recovery has completed then this will remain static at the time of the last transaction applied during
# recovery" (https://www.postgresql.org/docs/18/functions-admin.html#FUNCTIONS-RECOVERY-INFO-TABLE). The watcher commits
# every few seconds, so the last replayed commit sits at or after the drill's segment switch.
last_replayed_commit_epoch="$(query_wal_restored \
  'SELECT coalesce(extract(epoch FROM pg_last_xact_replay_timestamp())::bigint, 0)')"
(( wal_check_epoch - last_replayed_commit_epoch <= maximum_replay_lag_seconds )) ||
  fail "WAL restore: the last replayed commit is $((wal_check_epoch - last_replayed_commit_epoch)) s before the check"
for table_name in "${checked_tables[@]}"; do
  wal_restored_count="$(query_wal_restored "SELECT count(*) FROM $table_name")"
  printf 'WAL restore %s: restored %s, production before the switch %s\n' \
    "$table_name" "$wal_restored_count" "${production_count_before_switch[$table_name]}"
  (( wal_restored_count - production_count_before_switch[$table_name] <= dump_slack_rows &&
     production_count_before_switch[$table_name] - wal_restored_count <= dump_slack_rows )) ||
    fail "WAL restore $table_name: restored $wal_restored_count rows, production had ${production_count_before_switch[$table_name]}"
done
printf 'WAL restore check passed: last replayed commit %s s before the switch, base backup %s hours old\n' \
  "$((wal_check_epoch - last_replayed_commit_epoch))" "$base_backup_age_hours"
printf 'restore check passed\n'
