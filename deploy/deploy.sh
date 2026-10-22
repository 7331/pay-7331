#!/usr/bin/env bash
# Build the backend image from this checkout and start the production stack (migrate, then api and watcher, and the
# Monero sidecar when the env file sets PAY_MONERO_SIDECAR_URL).
# Usage: deploy/deploy.sh   (from any directory; settings in PAY_DEPLOY_ENVIRONMENT_FILE, deploy/host_paths.env)
# No rollback: a bad release is fixed forward with a new commit.
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Host paths (deploy/host_paths.env.example); exported for Compose, which interpolates them into the override.
set -a
# shellcheck disable=SC1091
[[ -r "${repository_root}/deploy/host_paths.env" ]] && . "${repository_root}/deploy/host_paths.env"
set +a
environment_file="${PAY_DEPLOY_ENVIRONMENT_FILE:?set PAY_DEPLOY_ENVIRONMENT_FILE in deploy/host_paths.env}"
: "${PAY_WAL_ARCHIVE_SETTINGS_FILE:?set PAY_WAL_ARCHIVE_SETTINGS_FILE in deploy/host_paths.env}"
: "${PAY_REVERSE_PROXY_NETWORK:?set PAY_REVERSE_PROXY_NETWORK in deploy/host_paths.env}"
[[ -f "${environment_file}" ]] || { echo "refusing: ${environment_file} does not exist (copy .env.example)" >&2; exit 78; }

# Every operator key variable the env file names must be blanked for api and migrate in docker-compose.yml
# (x-environment-without-operator-keys): one that is not reaches the API, which then refuses to start (PLAN.md section
# 5.1). Checked before anything changes; key names only, never values.
blanked_key_names="$(grep -oE '^  PAY_(OPERATOR_PRIVATE_KEY|SWEEPER_PRIVATE_KEYS)_[A-Z0-9_]+: ""' \
  "${repository_root}/docker-compose.yml" | sed -E 's/^  ([A-Z0-9_]+): ""$/\1/' || true)"
unblanked_key_names="$(grep -oE '^(PAY_OPERATOR_PRIVATE_KEY|PAY_SWEEPER_PRIVATE_KEYS)_[A-Z0-9_]*=' "${environment_file}" \
  | tr -d '=' | grep -vxF -f <(printf '%s\n' "${blanked_key_names}") || true)"
if [[ -n "${unblanked_key_names}" ]]; then
  echo "refusing: ${environment_file} sets operator key variables docker-compose.yml does not blank for api and" \
    "migrate (add each to x-environment-without-operator-keys):" ${unblanked_key_names} >&2
  exit 78
fi

compose=(docker compose -p pay-7331 --env-file "${environment_file}"
  -f "${repository_root}/docker-compose.yml" -f "${repository_root}/deploy/docker-compose.production.yml")
services=(postgres redis migrate api watcher)
# A watcher told where the Monero sidecar is needs it running, or its Monero ticks write no heartbeat and `up --wait`
# fails: with a non-empty PAY_MONERO_SIDECAR_URL the monero profile is built and xmr-scanner started too, before the
# watcher (its depends_on). Reads whether the variable has a value, never prints it.
if grep -qE '^PAY_MONERO_SIDECAR_URL=("[^"]+"|'"'"'[^'"'"']+'"'"'|[^"'"'"'[:space:]#])' "${environment_file}"; then
  compose+=(--profile monero)
  services=(postgres redis migrate xmr-scanner api watcher)
  # The sidecar runs as nonroot and bind-mounts this public configuration read-only. A checkout created under
  # umask 077 otherwise leaves the host file unreadable to it, so its first boot exits before serving health.
  chmod a+r "${repository_root}/backend/chains.toml"
fi

# A backup right before `migrate` is the way back from a migration gone wrong; one right after keeps the weekly
# restore drill's schema check matching production. The unit is a oneshot,
# so `systemctl start` returns once it has finished. PAY_DEPLOY_SKIP_BACKUP=1 deploys anyway, say while the bucket is down.
back_up_postgres() {
  [[ "${PAY_DEPLOY_SKIP_BACKUP:-}" == 1 ]] && { echo "skipping the $1 backup (PAY_DEPLOY_SKIP_BACKUP=1)"; return; }
  [[ "$(docker inspect -f '{{.State.Running}}' pay-7331-postgres 2>/dev/null)" == true ]] || return 0
  systemctl start pay-7331-backup.service || { echo "refusing: the $1 backup failed (journalctl -u pay-7331-backup)" >&2; exit 1; }
}

# The postgres service archives its WAL to R2 with these settings (deploy/write_postgres_wal_archive_settings.sh).
"${repository_root}/deploy/write_postgres_wal_archive_settings.sh"
# --pull: a floating base tag (distroless, python) is refetched, so a cached base never keeps a since-distrusted root.
"${compose[@]}" build --pull
back_up_postgres pre-deploy
"${compose[@]}" up -d --wait --remove-orphans "${services[@]}"
# The post-deploy run also proves WAL reaches R2 and, on the first deploy with WAL-G, takes the first base backup.
back_up_postgres post-deploy
echo "deployed $(git -C "${repository_root}" rev-parse HEAD)"
