#!/usr/bin/env bash
# Beside `e2e/run_live_flow.sh sweep_survives_a_watcher_crash`: once that invoice's sweep attempt is stored, SIGKILL
# the production watcher's process (a crash, not `docker stop`, so the restart policy must bring it back), then wait
# for Docker to restart it. Prints what it saw; exits 1 if the watcher is not back within two minutes.
set -euo pipefail

repository_root="$(cd "$(dirname "$0")/.." && pwd)"
# Host paths (deploy/host_paths.env.example); exported for Compose, which interpolates them into the override.
set -a
# shellcheck disable=SC1091
[[ -r "${repository_root}/deploy/host_paths.env" ]] && . "${repository_root}/deploy/host_paths.env"
set +a
environment_file="${PAY_DEPLOY_ENVIRONMENT_FILE:?set PAY_DEPLOY_ENVIRONMENT_FILE in deploy/host_paths.env}"
compose=(docker compose -p pay-7331 --env-file "${environment_file}"
         -f "${repository_root}/docker-compose.yml" -f "${repository_root}/deploy/docker-compose.production.yml")
note="live e2e: watcher killed mid-sweep"
query="SELECT a.id, a.last_broadcast_at IS NOT NULL FROM outgoing_attempts a JOIN outgoing_operations o ON o.id = a.operation_id
       JOIN invoices i ON i.id = o.invoice_id WHERE i.note = '${note}' AND o.kind = 'sweep'
       AND i.created_at > now() - interval '30 minutes' ORDER BY a.id LIMIT 1"

deadline=$((SECONDS + 900))
until attempt="$("${compose[@]}" exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F" "' <<<"${query}")" \
      && [[ -n "${attempt}" ]]; do
  ((SECONDS < deadline)) || { echo "no sweep attempt in 15 minutes"; exit 1; }
  sleep 0.2
done
restart_count_before="$(docker inspect -f '{{.RestartCount}}' pay-7331-watcher)"
kill -9 "$(docker inspect -f '{{.State.Pid}}' pay-7331-watcher)"
echo "$(date -u +%T) killed the watcher at attempt ${attempt% *} (broadcast before the kill: ${attempt#* })"

deadline=$((SECONDS + 120))
until [[ "$(docker inspect -f '{{.State.Running}} {{.RestartCount}}' pay-7331-watcher)" == "true $((restart_count_before + 1))" ]]; do
  ((SECONDS < deadline)) || { echo "the watcher did not come back in two minutes"; exit 1; }
  sleep 1
done
echo "$(date -u +%T) the watcher is back (restart count $((restart_count_before + 1)))"
