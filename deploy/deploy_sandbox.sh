#!/usr/bin/env bash
# Build the backend image from this checkout and start the testnet sandbox (pay-sandbox.7331.org, PLAN.md section 16):
# migrate, then api and watcher, as Compose project pay-7331-sandbox. Mirrors deploy/deploy.sh without the production
# parts (no backups, no WAL archive, no Monero sidecar). It never touches the production project pay-7331.
# Usage: deploy/deploy_sandbox.sh   (from any directory; settings in PAY_SANDBOX_ENVIRONMENT_FILE, deploy/host_paths.env)
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Host paths (deploy/host_paths.env.example); exported for Compose, which interpolates them into the override.
set -a
# shellcheck disable=SC1091
[[ -r "${repository_root}/deploy/host_paths.env" ]] && . "${repository_root}/deploy/host_paths.env"
set +a
environment_file="${PAY_SANDBOX_ENVIRONMENT_FILE:?set PAY_SANDBOX_ENVIRONMENT_FILE in deploy/host_paths.env}"
: "${PAY_REVERSE_PROXY_NETWORK:?set PAY_REVERSE_PROXY_NETWORK in deploy/host_paths.env}"
[[ -f "${environment_file}" ]] || { echo "refusing: ${environment_file} does not exist (copy .env.example)" >&2; exit 78; }

# The sandbox must say it is one on every page: refuse an env file without PAY_SANDBOX=true. Reads the value of this
# one non-secret setting only.
grep -qxE 'PAY_SANDBOX=(true|"true")' "${environment_file}" \
  || { echo "refusing: ${environment_file} does not set PAY_SANDBOX=true" >&2; exit 78; }

# The sandbox takes testnet money only: refuse any enabled chain but the two testnets, so a mainnet key in this env
# file can never take real money under the testnet banner, with no backups and no float refunds.
grep -qxE 'PAY_ENABLED_CHAINS="?(polygon_amoy|arbitrum_sepolia)(,(polygon_amoy|arbitrum_sepolia))?"?' \
  "${environment_file}" \
  || { echo "refusing: ${environment_file} must set PAY_ENABLED_CHAINS to testnets only (polygon_amoy," \
    "arbitrum_sepolia)" >&2; exit 78; }

# As deploy/deploy.sh: every operator key variable the env file names must be blanked for api and migrate in
# docker-compose.yml (x-environment-without-operator-keys), or the API refuses to start. Key names only, never values.
blanked_key_names="$(grep -oE '^  PAY_(OPERATOR_PRIVATE_KEY|SWEEPER_PRIVATE_KEYS)_[A-Z0-9_]+: ""' \
  "${repository_root}/docker-compose.yml" | sed -E 's/^  ([A-Z0-9_]+): ""$/\1/' || true)"
unblanked_key_names="$(grep -oE '^(PAY_OPERATOR_PRIVATE_KEY|PAY_SWEEPER_PRIVATE_KEYS)_[A-Z0-9_]*=' "${environment_file}" \
  | tr -d '=' | grep -vxF -f <(printf '%s\n' "${blanked_key_names}") || true)"
if [[ -n "${unblanked_key_names}" ]]; then
  echo "refusing: ${environment_file} sets operator key variables docker-compose.yml does not blank for api and" \
    "migrate (add each to x-environment-without-operator-keys):" ${unblanked_key_names} >&2
  exit 78
fi

compose=(docker compose -p pay-7331-sandbox --env-file "${environment_file}"
  -f "${repository_root}/docker-compose.yml" -f "${repository_root}/deploy/docker-compose.sandbox.yml")
# --pull: a floating base tag (distroless, python) is refetched, so a cached base never keeps a since-distrusted root.
"${compose[@]}" build --pull
"${compose[@]}" up -d --wait --remove-orphans postgres redis migrate api watcher
echo "sandbox deployed $(git -C "${repository_root}" rev-parse HEAD)"
