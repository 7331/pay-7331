#!/usr/bin/env bash
# Hourly off-box backup of the pay-7331 Postgres to Cloudflare R2, encrypted.
#
# WHY: every deposit address is a CREATE2 clone whose salt lives in Postgres (random before 2026-10-12, HMAC of the
# invoice id under PAY_INVOICE_SALT_SEED since); without the salt nobody can call `sweepToDestination` for that
# address. Two off-box copies, so a dead disk loses about the last minute, not the last hour:
#   1. this script's hourly pg_dump: logical, restores one table (`pg_restore -t invoices`), checked weekly;
#   2. WAL-G: Postgres archives every WAL segment to R2 as it fills or each minute (archive_timeout,
#      deploy/docker-compose.production.yml). This script proves archiving works (it switches to a new segment and
#      waits until the archiver reports it done and R2 lists it) and takes the daily full base backup the WAL replays
#      onto. A pg_dump cannot be that base: "pg_dump and pg_dumpall do not produce file-system-level backups and cannot be
#      used as part of a continuous-archiving solution" (https://www.postgresql.org/docs/18/continuous-archiving.html).
#
# Postgres only: Redis holds copies with TTLs, nothing a restore needs.
#
#   deploy/backup_postgres_to_r2.sh             # dump, seal, upload, verify, publish manifest, prune; then
#                                               # check WAL archiving, and a full base backup once the newest is a day old
#   DRY_RUN=1 deploy/backup_postgres_to_r2.sh   # prove container, tools, key, bucket and the archive; upload nothing
#
# Format: pg_dump custom format (-Fc), not gzipped plain SQL. It is compressed by default, and pg_restore
# can restore one table from it (`pg_restore -t invoices`), which is the salt-recovery case, and
# `pg_restore --list` reads its table of contents without a server. "Consistent exports even if the
# database is being used concurrently", without blocking readers or writers:
# https://www.postgresql.org/docs/18/app-pgdump.html
#
# Encryption: OpenPGP symmetric, AES-256, passphrase from a root-only key file, via a throwaway GnuPG
# home. gpg always adds integrity protection (CFB+MDC or OCB) and fails a decrypt whose check fails
# unless --ignore-mdc-error is given: https://www.gnupg.org/documentation/manuals/gnupg/OpenPGP-Options.html
# and https://www.gnupg.org/documentation/manuals/gnupg/GPG-Esoteric-Options.html (--passphrase-file
# needs --batch and --pinentry-mode loopback). --compress-algo none: -Fc output is already compressed.
#
# Upload: R2's S3 API (region `auto`; PutObject, GetObject, ListObjectsV2, HeadBucket supported):
# https://developers.cloudflare.com/r2/api/s3/api/
#
# Objects, under backups/pay-7331/postgres/ in the backup bucket:
#   pay-7331-postgres-<UTC stamp>.dump.gpg            the sealed dump
#   pay-7331-postgres-<UTC stamp>.manifest.json       sha256, size, alembic revision, row counts;
#                                                     published LAST, after the uploaded bytes re-hash
#                                                     equal, so an archive without one is a failed run.
#
# Restore by hand:
#   gpg --batch --pinentry-mode loopback --passphrase-file "$PAY_BACKUP_KEY_FILE" \
#     --decrypt --output pay.dump pay-7331-postgres-<stamp>.dump.gpg   # use it only if gpg exited 0
#   docker exec -i pay-7331-postgres pg_restore -U pay -d pay --clean --if-exists --exit-on-error < pay.dump
#
# Restore after a lost disk, to the last archived minute (WAL-G under backups/pay-7331/wal-g; tested by destroying a
# throwaway server and its volume): with the backup key and the BACKUP_STORAGE_* credentials back in place, a checkout
# with deploy/host_paths.env, its variables exported, and the volume empty or new,
#   deploy/write_postgres_wal_archive_settings.sh
#   docker compose -p pay-7331 --env-file "$PAY_DEPLOY_ENVIRONMENT_FILE" -f docker-compose.yml \
#     -f deploy/docker-compose.production.yml build postgres
#   docker run --rm --user postgres --env-file "$PAY_WAL_ARCHIVE_SETTINGS_FILE" \
#     -v pay-7331_postgres_data:/var/lib/postgresql --entrypoint sh pay-7331-postgres:production -c \
#     'mkdir -p -m 0700 "$PGDATA" && wal-g backup-fetch "$PGDATA" LATEST && touch "$PGDATA/recovery.signal"'
#   deploy/deploy.sh   # Postgres replays every archived segment (restore_command), then promotes to a new timeline;
#                      # a day of WAL can outlast the healthcheck's minute: when deploy.sh gives up, let the replay
#                      # finish (`docker logs pay-7331-postgres`, "archive recovery complete") and run it again
# Then run this script once: on the new timeline archive_timeout first fires only after checkpoint_timeout (5 min), as
# the checkpointer computes that sleep while still in recovery (src/backend/postmaster/checkpointer.c, CheckpointerMain),
# and a fresh base backup on the new timeline shortens the next restore.
set -euo pipefail
umask 077

script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Host paths: from deploy/host_paths.env when run from a checkout, from the units' EnvironmentFile= under systemd.
set -a
# shellcheck disable=SC1091
[[ -r "${script_directory}/host_paths.env" ]] && . "${script_directory}/host_paths.env"
set +a
backup_directory="${PAY_BACKUP_DIRECTORY:?set PAY_BACKUP_DIRECTORY}"
local_copies_to_keep="${PAY_BACKUP_LOCAL_COPIES:-48}"
key_file="${PAY_BACKUP_KEY_FILE:?set PAY_BACKUP_KEY_FILE}"
# A Python with boto3.
python_interpreter="${PAY_BACKUP_PYTHON:?set PAY_BACKUP_PYTHON}"
# A file whose BACKUP_STORAGE_* lines hold the bucket credentials.
storage_credentials_file="${PAY_BACKUP_STORAGE_CREDENTIALS_FILE:?set PAY_BACKUP_STORAGE_CREDENTIALS_FILE}"
postgres_container="${PAY_POSTGRES_CONTAINER:-pay-7331-postgres}"
# A test run points these at a throwaway prefix and marker; production never sets them.
bucket_prefix="${PAY_BACKUP_BUCKET_PREFIX:-backups/pay-7331/postgres}"
failure_marker_file="${PAY_BACKUP_FAILURE_MARKER_FILE:-/var/lib/pay-7331/backup-failed}"
# A full base backup once a day (the runs are hourly, so 23 hours keeps the timer's jitter from skipping a day); the
# bucket's 90-day expiry removes old ones with their WAL (deploy/write_postgres_wal_archive_settings.sh).
full_base_backup_interval_seconds="${PAY_FULL_BASE_BACKUP_INTERVAL_SECONDS:-82800}"
# Where WAL-G puts segments: <WALG_S3_PREFIX>/wal_005/<segment>.<compression>; the bucket key is checked independently.
wal_archive_bucket_prefix="${PAY_WAL_ARCHIVE_BUCKET_PREFIX:-backups/pay-7331/wal-g}"
wal_archive_wait_seconds="${PAY_WAL_ARCHIVE_WAIT_SECONDS:-180}"
backup_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
dry_run="${DRY_RUN:-}"

fail() { printf 'backup failed: %s\n' "$1" >&2; exit 1; }

gnupg_home="$(mktemp -d)"
remove_temporary_files() { rm -rf -- "$gnupg_home"; }
trap remove_temporary_files EXIT

# Credentials: only the four BACKUP_STORAGE_* lines of the credentials file, loaded with `set -a` and `.`, so no
# other secret in that file enters this process. Never echoed, never in argv.
[ -r "$storage_credentials_file" ] || fail "cannot read $storage_credentials_file (the backup storage credentials)"
set -a
# shellcheck disable=SC1090
. <(grep -E '^BACKUP_STORAGE_(ENDPOINT_URL|ACCESS_KEY|SECRET_KEY|BUCKET)=' "$storage_credentials_file")
set +a
: "${BACKUP_STORAGE_ENDPOINT_URL:?BACKUP_STORAGE_ENDPOINT_URL is unset}"
: "${BACKUP_STORAGE_ACCESS_KEY:?BACKUP_STORAGE_ACCESS_KEY is unset}"
: "${BACKUP_STORAGE_SECRET_KEY:?BACKUP_STORAGE_SECRET_KEY is unset}"
: "${BACKUP_STORAGE_BUCKET:?BACKUP_STORAGE_BUCKET is unset}"

[ -x "$python_interpreter" ] || fail "no python at $python_interpreter (set PAY_BACKUP_PYTHON)"
"$python_interpreter" -c 'import boto3' 2>/dev/null || fail "boto3 is missing from $python_interpreter"
command -v gpg >/dev/null || fail "gpg is missing (apt-get install gnupg); it seals the archive"
command -v jq >/dev/null || fail "jq is missing (apt-get install jq); it reads wal-g backup-list"
[ "$(docker inspect --format '{{.State.Running}}' "$postgres_container" 2>/dev/null)" = true ] ||
  fail "$postgres_container is not running"

# Database name and user from the container's own environment (not secrets; the password is never read).
read_container_environment_value() {
  docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$postgres_container" |
    sed -n "s/^$1=//p" | head -n 1
}
postgres_user="$(read_container_environment_value POSTGRES_USER)"
postgres_database="$(read_container_environment_value POSTGRES_DB)"
[[ -n "$postgres_user" && -n "$postgres_database" ]] || fail "POSTGRES_USER or POSTGRES_DB missing in $postgres_container"

# WAL-G runs inside the Postgres container as its `postgres` user (it reads PGDATA), with the WALG_*/AWS_* settings the
# container got from deploy/write_postgres_wal_archive_settings.sh; no secret passes through this script.
run_wal_g() { docker exec -u postgres "$postgres_container" wal-g "$@"; }
docker exec "$postgres_container" which wal-g >/dev/null ||
  fail "$postgres_container has no wal-g; deploy with deploy/deploy.sh first (deploy/postgres/Dockerfile)"

run_production_query() {
  docker exec "$postgres_container" psql -U "$postgres_user" -d "$postgres_database" -v ON_ERROR_STOP=1 -tAc "$1" |
    tr -d '\r'
}

if [ -n "$dry_run" ]; then
  printf 'DRY_RUN: would dump %s/%s to s3://<bucket>/%s/pay-7331-postgres-%s.dump.gpg\n' \
    "$postgres_container" "$postgres_database" "$bucket_prefix" "$backup_stamp"
  printf 'DRY_RUN: alembic revision %s\n' "$(run_production_query 'SELECT version_num FROM alembic_version')"
  if [ -r "$key_file" ]; then echo "DRY_RUN: key file $key_file present"; else echo "DRY_RUN: would create $key_file"; fi
  "$python_interpreter" - <<'PROBE'
import os
import boto3

boto3.client(
    "s3",
    endpoint_url=os.environ["BACKUP_STORAGE_ENDPOINT_URL"],
    aws_access_key_id=os.environ["BACKUP_STORAGE_ACCESS_KEY"],
    aws_secret_access_key=os.environ["BACKUP_STORAGE_SECRET_KEY"],
    region_name="auto",
).head_bucket(Bucket=os.environ["BACKUP_STORAGE_BUCKET"])
print("DRY_RUN: credentials work; bucket is reachable")
PROBE
  printf 'DRY_RUN: archive_mode %s; pg_stat_archiver: %s\n' \
    "$(run_production_query "SELECT current_setting('archive_mode')")" \
    "$(run_production_query "SELECT format('archived %s, last %s at %s, failed %s, last failure %s at %s',
      archived_count, last_archived_wal, last_archived_time, failed_count, last_failed_wal, last_failed_time)
      FROM pg_stat_archiver")"
  run_wal_g backup-list
  exit 0
fi

# The key is made once, root-only, and MUST also be copied off this machine: the disk that
# dies takes the key with it, and an archive nobody can decrypt is not a backup. The value goes straight
# to the file, never to argv or output.
if [ ! -e "$key_file" ]; then
  openssl rand -base64 48 > "$key_file"
  chmod 600 "$key_file"
  printf 'created backup key %s; copy it OFF this machine\n' "$key_file"
fi
[[ -r "$key_file" && -s "$key_file" ]] || fail "backup key $key_file is unreadable or empty"

# One run at a time (a manual run and the timer), locked on the key file's inode.
exec 9<"$key_file"
flock -w "${PAY_BACKUP_LOCK_WAIT_SECONDS:-900}" 9 || fail "another backup holds the lock on $key_file"

mkdir -p "$backup_directory"
chmod 700 "$backup_directory"
archive_name="pay-7331-postgres-$backup_stamp.dump.gpg"
manifest_name="pay-7331-postgres-$backup_stamp.manifest.json"
archive_file="$backup_directory/$archive_name"
manifest_file="$backup_directory/$manifest_name"

docker exec "$postgres_container" pg_dump -U "$postgres_user" -d "$postgres_database" --format=custom |
  gpg --homedir "$gnupg_home" --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$key_file" \
    --symmetric --cipher-algo AES256 --s2k-mode 3 --s2k-digest-algo SHA512 --s2k-count 65011712 \
    --compress-algo none --output "$archive_file"
[ -s "$archive_file" ] || fail "the sealed dump is empty"

# What the restore drill compares against. Read just after the dump, from a later snapshot, so they can
# be a few rows ahead of the archive; the drill allows for that.
alembic_revision="$(run_production_query 'SELECT version_num FROM alembic_version')"
row_counts="$(run_production_query "SELECT json_build_object(
  'invoices', (SELECT count(*) FROM invoices),
  'invoice_payments', (SELECT count(*) FROM invoice_payments),
  'outgoing_operations', (SELECT count(*) FROM outgoing_operations),
  'invoice_stray_payments', (SELECT count(*) FROM invoice_stray_payments),
  'static_deposit_addresses', (SELECT count(*) FROM static_deposit_addresses),
  'static_deposit_payments', (SELECT count(*) FROM static_deposit_payments))")"

# Upload, re-read the uploaded bytes and compare length and sha256, then publish the manifest last.
ARCHIVE_FILE="$archive_file" MANIFEST_FILE="$manifest_file" BUCKET_PREFIX="$bucket_prefix" \
  BACKUP_STAMP="$backup_stamp" ALEMBIC_REVISION="$alembic_revision" ROW_COUNTS="$row_counts" \
  "$python_interpreter" - <<'UPLOAD'
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
archive_path = Path(os.environ["ARCHIVE_FILE"])
manifest_path = Path(os.environ["MANIFEST_FILE"])
prefix = os.environ["BUCKET_PREFIX"]
archive_key = f"{prefix}/{archive_path.name}"
manifest_key = f"{prefix}/{manifest_path.name}"

with archive_path.open("rb") as stream:
    local_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
local_size = archive_path.stat().st_size
client.upload_file(str(archive_path), bucket, archive_key)

remote = client.get_object(Bucket=bucket, Key=archive_key)
remote_sha256 = hashlib.sha256()
try:
    for chunk in remote["Body"].iter_chunks(chunk_size=1024 * 1024):
        remote_sha256.update(chunk)
finally:
    remote["Body"].close()
if remote["ContentLength"] != local_size or remote_sha256.hexdigest() != local_sha256:
    raise SystemExit("uploaded archive does not match the local one; manifest not published")

manifest = {
    "stamp": os.environ["BACKUP_STAMP"],
    "archive_key": archive_key,
    "size": local_size,
    "sha256": local_sha256,
    "format": "pg_dump custom (-Fc), OpenPGP symmetric AES-256",
    "alembic_revision": os.environ["ALEMBIC_REVISION"],
    "row_counts_after_dump": json.loads(os.environ["ROW_COUNTS"]),
}
manifest_bytes = json.dumps(manifest, sort_keys=True, indent=2).encode()
manifest_path.write_bytes(manifest_bytes)
client.put_object(Bucket=bucket, Key=manifest_key, Body=manifest_bytes, ContentType="application/json")
print(f"uploaded {archive_key} ({local_size} bytes, sha256 verified) and its manifest")
UPLOAD

# Keep the newest N local archives and their manifests. Local copies are a convenience; the backup is R2.
chmod 600 "$archive_file" "$manifest_file"
for pattern in 'pay-7331-postgres-*.dump.gpg' 'pay-7331-postgres-*.manifest.json'; do
  find "$backup_directory" -maxdepth 1 -type f -name "$pattern" -printf '%f\n' | sort -r |
    tail -n "+$((local_copies_to_keep + 1))" | while read -r stale_name; do
      rm -f -- "$backup_directory/$stale_name"
    done
done

# WAL archive: close the current segment and wait until it is archived. A restore point first, so there is WAL to
# switch: with no activity since the last switch "pg_switch_wal does nothing"; otherwise it returns "the ending
# write-ahead log location plus 1 within the just-completed write-ahead log file"
# (https://www.postgresql.org/docs/18/functions-admin.html#FUNCTIONS-ADMIN-BACKUP-TABLE). The archiver counts a segment
# archived only once archive_command (wal-g wal-push) exited 0, and segments go in order
# (https://www.postgresql.org/docs/18/monitoring-stats.html#MONITORING-PG-STAT-ARCHIVER-VIEW); R2's own listing
# confirms it.
run_production_query "SELECT pg_create_restore_point('pay-7331 backup check')" >/dev/null
switched_wal_segment="$(run_production_query 'SELECT pg_walfile_name(pg_switch_wal())')"
wal_archive_deadline=$(( $(date -u +%s) + wal_archive_wait_seconds ))
while :; do
  last_archived_wal_segment="$(run_production_query "SELECT coalesce(last_archived_wal, '') FROM pg_stat_archiver")"
  # Same timeline, so the fixed-width hex names sort in WAL order.
  [[ "$last_archived_wal_segment" < "$switched_wal_segment" ]] || break
  [ "$(date -u +%s)" -lt "$wal_archive_deadline" ] || fail "WAL segment $switched_wal_segment was not archived within \
${wal_archive_wait_seconds}s; see pg_stat_archiver and \`docker logs $postgres_container\`"
  sleep 2
done
WAL_OBJECT_PREFIX="$wal_archive_bucket_prefix/wal_005/$switched_wal_segment." "$python_interpreter" - <<'WAL_OBJECT'
import os

import boto3

client = boto3.client(
    "s3",
    endpoint_url=os.environ["BACKUP_STORAGE_ENDPOINT_URL"],
    aws_access_key_id=os.environ["BACKUP_STORAGE_ACCESS_KEY"],
    aws_secret_access_key=os.environ["BACKUP_STORAGE_SECRET_KEY"],
    region_name="auto",
)
listing = client.list_objects_v2(Bucket=os.environ["BACKUP_STORAGE_BUCKET"], Prefix=os.environ["WAL_OBJECT_PREFIX"])
if not listing.get("Contents"):
    raise SystemExit(f"the archiver reports {os.environ['WAL_OBJECT_PREFIX']}* done, but R2 does not list it")
print(f"WAL segment {listing['Contents'][0]['Key']} is in R2 ({listing['Contents'][0]['Size']} bytes)")
WAL_OBJECT

# `backup-list --json --detail` gives each backup's finish_time; daily full backups, deltas off (WALG_DELTA_MAX_STEPS
# defaults to 0, https://github.com/wal-g/wal-g/blob/master/docs/PostgreSQL.md).
newest_base_backup_epoch="$(run_wal_g backup-list --json --detail 2>/dev/null |
  jq '[.[]?.finish_time | sub("\\.[0-9]+"; "") | fromdateiso8601] | max // 0')" || newest_base_backup_epoch=0
if (( $(date -u +%s) - newest_base_backup_epoch >= full_base_backup_interval_seconds )); then
  # A full backup a day keeps restores simple: each one restores on its own, with no delta chain.
  docker exec -u postgres "$postgres_container" sh -c 'exec wal-g backup-push "$PGDATA"'
  printf 'completed WAL-G full base backup\n'
fi

# Written by pay-7331-backup-failed.service; cleared only here, at the end of a run that published a
# verified manifest, so its presence means the most recent attempt did not finish.
rm -f "$failure_marker_file"
printf 'completed pay-7331 Postgres backup stamp=%s\n' "$backup_stamp"
