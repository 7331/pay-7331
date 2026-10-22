#!/usr/bin/env bash
# Write the WAL-G settings the production Postgres archives its WAL with (deploy/docker-compose.production.yml reads the
# file as the postgres service's env_file; deploy.sh runs this before every `up`, so a rotated R2 key lands with the
# next deploy). The backup script and the restore drill use the same file.
#
#   deploy/write_postgres_wal_archive_settings.sh    # writes PAY_WAL_ARCHIVE_SETTINGS_FILE (mode 600)
#
# Where: the R2 backup bucket (the BACKUP_STORAGE_* lines the hourly dump uses), under backups/pay-7331/wal-g,
# beside the dumps' backups/pay-7331/postgres. Everything under backups/ sits under a 7-day object lock rule and a
# 90-day expiry rule on the bucket: an object cannot be overwritten or deleted for 7 days, and goes by itself after 90.
# WAL-G writes each key once (no index file it rewrites), so it works under the lock, and the expiry is the retention:
# nothing here deletes.
# S3 settings for an S3-compatible store: AWS_ENDPOINT, AWS_REGION, AWS_S3_FORCE_PATH_STYLE
# (https://github.com/wal-g/wal-g/blob/master/docs/STORAGES.md); R2's region is `auto`
# (https://developers.cloudflare.com/r2/api/s3/api/).
# Sealing: libsodium, client-side, "fixed-size keys of 32 bytes" (WALG_LIBSODIUM_KEY,
# https://github.com/wal-g/wal-g/blob/master/docs/README.md). The key is SHA-256 of the backup key file
# (PAY_BACKUP_KEY_FILE: 48 random bytes, base64, the key the dumps are sealed with), so the one key copied off-box
# opens both. To rebuild it elsewhere: `sha256sum < backup.key` with the file exactly as it is (its trailing newline
# included), the first 64 hex digits.
# WALG_PREVENT_WAL_OVERWRITE: a segment that is already archived is compared, not uploaded again; equal content counts
# as archived (internal/databases/postgres/wal_push_handler.go, checkWALOverwrite). Without it a retried push would
# try to overwrite a locked object and fail for 7 days.
set -euo pipefail
umask 077

script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Host paths: from deploy/host_paths.env when run from a checkout, from the units' EnvironmentFile= under systemd.
set -a
# shellcheck disable=SC1091
[[ -r "${script_directory}/host_paths.env" ]] && . "${script_directory}/host_paths.env"
set +a
storage_credentials_file="${PAY_BACKUP_STORAGE_CREDENTIALS_FILE:?set PAY_BACKUP_STORAGE_CREDENTIALS_FILE}"
key_file="${PAY_BACKUP_KEY_FILE:?set PAY_BACKUP_KEY_FILE}"
settings_file="${PAY_WAL_ARCHIVE_SETTINGS_FILE:?set PAY_WAL_ARCHIVE_SETTINGS_FILE}"
# A test run points this at a throwaway prefix; production never sets it.
bucket_prefix="${PAY_WAL_ARCHIVE_BUCKET_PREFIX:-backups/pay-7331/wal-g}"

fail() { printf 'refusing: %s\n' "$1" >&2; exit 1; }

[ -r "$storage_credentials_file" ] || fail "cannot read $storage_credentials_file (the backup storage credentials)"
# The key is created by the first backup run (backup_postgres_to_r2.sh), never here, so a wrong path cannot start a
# second key.
[[ -r "$key_file" && -s "$key_file" ]] || fail "no backup key at $key_file (run deploy/backup_postgres_to_r2.sh once)"
[[ "$bucket_prefix" != /* && "$bucket_prefix" != */ ]] || fail "PAY_WAL_ARCHIVE_BUCKET_PREFIX has a leading or trailing /"

# Only the four BACKUP_STORAGE_* lines, never echoed or in argv (as the backup script loads them).
set -a
# shellcheck disable=SC1090
. <(grep -E '^BACKUP_STORAGE_(ENDPOINT_URL|ACCESS_KEY|SECRET_KEY|BUCKET)=' "$storage_credentials_file")
set +a
: "${BACKUP_STORAGE_ENDPOINT_URL:?BACKUP_STORAGE_ENDPOINT_URL is unset}"
: "${BACKUP_STORAGE_ACCESS_KEY:?BACKUP_STORAGE_ACCESS_KEY is unset}"
: "${BACKUP_STORAGE_SECRET_KEY:?BACKUP_STORAGE_SECRET_KEY is unset}"
: "${BACKUP_STORAGE_BUCKET:?BACKUP_STORAGE_BUCKET is unset}"
libsodium_key_hex="$(sha256sum < "$key_file" | cut -c1-64)"

temporary_settings_file="$(mktemp "$(dirname "$settings_file")/.postgres-wal-archive.env.XXXXXX")"
trap 'rm -f -- "$temporary_settings_file"' EXIT
{
  echo "WALG_S3_PREFIX=s3://$BACKUP_STORAGE_BUCKET/$bucket_prefix"
  echo "AWS_ENDPOINT=$BACKUP_STORAGE_ENDPOINT_URL"
  echo "AWS_REGION=auto"
  echo "AWS_S3_FORCE_PATH_STYLE=true"
  echo "AWS_ACCESS_KEY_ID=$BACKUP_STORAGE_ACCESS_KEY"
  echo "AWS_SECRET_ACCESS_KEY=$BACKUP_STORAGE_SECRET_KEY"
  echo "WALG_LIBSODIUM_KEY=$libsodium_key_hex"
  echo "WALG_LIBSODIUM_KEY_TRANSFORM=hex"
  echo "WALG_COMPRESSION_METHOD=zstd"
  echo "WALG_PREVENT_WAL_OVERWRITE=true"
  # backup-push talks to the server over its socket as the image's superuser (POSTGRES_USER, pay; trust locally).
  echo "PGHOST=/var/run/postgresql"
  echo "PGUSER=pay"
  echo "PGDATABASE=pay"
} > "$temporary_settings_file"
chmod 600 "$temporary_settings_file"
mv -f -- "$temporary_settings_file" "$settings_file"
trap - EXIT
printf 'wrote %s (prefix %s)\n' "$settings_file" "$bucket_prefix"
