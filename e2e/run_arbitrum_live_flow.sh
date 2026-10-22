#!/usr/bin/env bash
# Run by hand against production. Uses the Arbitrum float for three payments and returns the payer's leftovers.
# The overpayment's refund needs an API token of one of the operator's accounts (PAY_OPERATOR_ACCOUNT_IDS), in a file of
# mode 600 whose path PAY_LIVE_OPERATOR_API_TOKEN_FILE names (a file keeps it out of shell history). Mint it on the
# first listed account (or another listed account with no webhook endpoint), never on an integrator's account: its
# invoices fire that account's webhooks and outlive the run. It reaches the container as PAY_LIVE_OPERATOR_API_TOKEN by
# name only, never on a command line, and is never printed; with no file named, the overpayment is skipped.
set -euo pipefail
repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Host paths (deploy/host_paths.env.example); exported for Compose, which interpolates them into the override.
set -a
# shellcheck disable=SC1091
[[ -r "${repository_root}/deploy/host_paths.env" ]] && . "${repository_root}/deploy/host_paths.env"
set +a
environment_file="${PAY_DEPLOY_ENVIRONMENT_FILE:?set PAY_DEPLOY_ENVIRONMENT_FILE in deploy/host_paths.env}"
public_base_url="$(sed -n 's/^PAY_PUBLIC_BASE_URL=//p' "${environment_file}")"
payout_address="$(sed -n 's/^PAY_DEFAULT_DESTINATION_ADDRESS=//p' "${environment_file}")"
live_account_username="live_e2e_$(openssl rand -hex 4)"
readable_copy="$(mktemp -d)"
session_folder="$(mktemp -d)"
trap 'rm -r "${readable_copy}" "${session_folder}"' EXIT
chmod 755 "${readable_copy}"
install -m 644 "${repository_root}/e2e/arbitrum_live_flow.py" "${readable_copy}/arbitrum_live_flow.py"
# Only from the file: a token inherited from the caller's environment is dropped.
unset PAY_LIVE_OPERATOR_API_TOKEN
operator_token_options=()
if [[ -n "${PAY_LIVE_OPERATOR_API_TOKEN_FILE:-}" ]]; then
  if [[ ! -f "${PAY_LIVE_OPERATOR_API_TOKEN_FILE}" ]] \
    || [[ "$(stat -c '%a' "${PAY_LIVE_OPERATOR_API_TOKEN_FILE}")" != 600 ]]; then
    echo "PAY_LIVE_OPERATOR_API_TOKEN_FILE must be a file of mode 600; not starting" >&2
    exit 2
  fi
  PAY_LIVE_OPERATOR_API_TOKEN="$(<"${PAY_LIVE_OPERATOR_API_TOKEN_FILE}")"
  export PAY_LIVE_OPERATOR_API_TOKEN
  operator_token_options=(-e PAY_LIVE_OPERATOR_API_TOKEN)
fi
if [[ "${1:-}" == --resume-invoices ]]; then
  live_account_username=""
  live_session_token=""
else
  uv run --project "${repository_root}/backend" python "${repository_root}/e2e/account_passkey_session.py" \
    --public-base-url "${public_base_url}" --username "${live_account_username}" --payout-address "${payout_address}" \
    --session-token-file "${session_folder}/account_session_token" --through-local-caddy
  live_session_token="$(cat "${session_folder}/account_session_token")"
fi
PAY_LIVE_ACCOUNT_SESSION_TOKEN="${live_session_token}" \
  PAY_LIVE_ACCOUNT_USERNAME="${live_account_username}" docker compose -p pay-7331 \
  --env-file "${environment_file}" \
  -f "${repository_root}/docker-compose.yml" -f "${repository_root}/deploy/docker-compose.production.yml" \
  run --rm -T --no-deps -e PAY_LIVE_ACCOUNT_SESSION_TOKEN -e PAY_LIVE_ACCOUNT_USERNAME "${operator_token_options[@]}" \
  -v "${readable_copy}:/live:ro" watcher python -u /live/arbitrum_live_flow.py "$@"
