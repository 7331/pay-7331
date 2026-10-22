#!/usr/bin/env bash
# Deploy the DepositForwarderFactory on each sandbox testnet that has none yet (PLAN.md section 16), write its address
# into the sandbox env file (PAY_SANDBOX_ENVIRONMENT_FILE) as PAY_FACTORY_ADDRESS_<CHAIN>, then redeploy the sandbox so it takes
# invoices there. Signs with that chain's sandbox refund key, which needs testnet gas first (PLAN.md section 16 says
# from which faucet). The key goes to forge through the environment only, never a command line, and is never printed.
# Usage: deploy/deploy_sandbox_factories.sh   (from any directory)
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Host paths (deploy/host_paths.env.example); exported for deploy/deploy_sandbox.sh.
set -a
# shellcheck disable=SC1091
[[ -r "${repository_root}/deploy/host_paths.env" ]] && . "${repository_root}/deploy/host_paths.env"
set +a
environment_file="${PAY_SANDBOX_ENVIRONMENT_FILE:?set PAY_SANDBOX_ENVIRONMENT_FILE in deploy/host_paths.env}"
forge_binary="${FORGE_BINARY:-${PAY_FOUNDRY_BIN_DIRECTORY:-${HOME}/.foundry/bin}/forge}"
[[ -f "${environment_file}" ]] || { echo "refusing: ${environment_file} does not exist" >&2; exit 78; }
grep -qxE 'PAY_SANDBOX=(true|"true")' "${environment_file}" \
  || { echo "refusing: ${environment_file} does not set PAY_SANDBOX=true" >&2; exit 78; }

# Each sandbox chain and the first of its chains.toml rpc_urls (the official endpoint where there is one).
declare -A rpc_url_by_chain_key=(
  [polygon_amoy]=https://polygon-amoy-bor-rpc.publicnode.com
  [arbitrum_sepolia]=https://sepolia-rollup.arbitrum.io/rpc
)

# Build output, cache and broadcast records stay out of contracts/.
work_directory="$(mktemp -d)"
trap 'rm -rf "${work_directory}"' EXIT

deployed_any_factory=false
for chain_key in polygon_amoy arbitrum_sepolia; do
  environment_key="${chain_key^^}"
  if grep -qE "^PAY_FACTORY_ADDRESS_${environment_key}=0x[0-9a-fA-F]{40}$" "${environment_file}"; then
    echo "${chain_key}: factory already set, skipped"
    continue
  fi
  deployer_private_key="$(grep -E "^PAY_OPERATOR_PRIVATE_KEY_${environment_key}=" "${environment_file}" | cut -d= -f2- || true)"
  [[ -n "${deployer_private_key}" ]] || { echo "refusing: PAY_OPERATOR_PRIVATE_KEY_${environment_key} is empty" >&2; exit 78; }
  deploy_output="$(DEPLOYER_PRIVATE_KEY="${deployer_private_key}" \
    FOUNDRY_OUT="${work_directory}/out" FOUNDRY_CACHE_PATH="${work_directory}/cache" \
    FOUNDRY_BROADCAST="${work_directory}/broadcast-${chain_key}" \
    "${forge_binary}" script --root "${repository_root}/contracts" \
    "${repository_root}/contracts/script/DeployDepositForwarderFactory.s.sol:DeployDepositForwarderFactory" \
    --rpc-url "${rpc_url_by_chain_key[${chain_key}]}" --broadcast --slow 2>&1)" \
    || { echo "${chain_key}: forge script failed (does the refund key hold gas?):" >&2; echo "${deploy_output}" | tail -20 >&2; exit 1; }
  factory_address="$(grep -oE 'DepositForwarderFactory: 0x[0-9a-fA-F]{40}' <<<"${deploy_output}" | tail -1 | cut -d' ' -f2)"
  [[ -n "${factory_address}" ]] || { echo "${chain_key}: forge script printed no factory address" >&2; exit 1; }
  sed -i -E "s/^PAY_FACTORY_ADDRESS_${environment_key}=.*$/PAY_FACTORY_ADDRESS_${environment_key}=${factory_address}/" \
    "${environment_file}"
  # sed changes nothing when the line is missing; without this the next run would deploy a second factory.
  grep -qx "PAY_FACTORY_ADDRESS_${environment_key}=${factory_address}" "${environment_file}" \
    || { echo "${chain_key}: ${environment_file} has no PAY_FACTORY_ADDRESS_${environment_key} line; add it as" \
      "PAY_FACTORY_ADDRESS_${environment_key}=${factory_address}" >&2; exit 1; }
  echo "${chain_key}: factory ${factory_address}"
  deployed_any_factory=true
done
unset deployer_private_key

if [[ "${deployed_any_factory}" == true ]]; then
  "${repository_root}/deploy/deploy_sandbox.sh"
fi
