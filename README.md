# pay-7331

A self-hosted, non-custodial crypto payment gateway. Live at https://pay.7331.org, API docs at
https://pay.7331.org/docs, and a testnet sandbox at https://pay-sandbox.7331.org.

- Each invoice gets its own deposit address: a CREATE2 forwarder clone with the receiver's address baked in. The
  contract has no owner and no refund path, so money that reaches a deposit can only ever be swept to its receiver.
- Chains: Polygon PoS and Arbitrum One (a curated list of coins, each priced by a Chainlink feed), and Monero (the
  receiver's own wallet, watched with a view key).
- Invoices (signed out, or on an account with a passkey), quantity links that up to N payers can pay, repeating
  invoices, and static deposit addresses for a merchant's customers.
- A REST API with account API tokens, signed webhooks (Standard Webhooks), Discord notices and a CSV export.

[PLAN.md](PLAN.md) is the design and interface reference; code cites its section numbers.

## Repository layout

| Path | What it is |
|---|---|
| `backend/` | Python API, chain watcher and operator CLI (FastAPI, SQLAlchemy, Alembic, Postgres, Redis) |
| `frontend/` | React and TypeScript app (Vite, shadcn/ui, Tailwind), built into static files the API serves |
| `contracts/` | Foundry project: `DepositForwarder` and `DepositForwarderFactory` |
| `xmr-scanner/` | Rust sidecar that scans Monero blocks for the gateway's subaddresses |
| `sdk/python/` | Python client for the API |
| `e2e/` | End-to-end runs against an anvil fork of Polygon and Arbitrum, plus the live mainnet runs |
| `deploy/` | Production and sandbox Compose files, deploy scripts and backups |

## Run it locally

You need Docker with Compose, and the contracts' submodules:

```sh
git clone --recurse-submodules https://github.com/7331/pay-7331.git
cd pay-7331
cp .env.example .env
chmod 600 .env
```

In `.env`, set `POSTGRES_PASSWORD`, `PAY_INVOICE_SALT_SEED` (`openssl rand -hex 32`) and `PAY_IP_HASH_SALT`. Then:

```sh
docker compose up -d --build --wait   # Postgres, Redis, the migration, then the API and the watcher
docker compose down -v                # stop, and drop the volumes
```

The pages and the API are at http://localhost:18080. A chain makes invoices only once it has a deployed factory
(`PAY_FACTORY_ADDRESS_<CHAIN>`); the `local-chain` Compose profile adds an anvil node for that. The simplest way to see
the whole flow is the fork stack, which forks Polygon, deploys the factory and pays invoices on the fork (it needs
[Foundry](https://getfoundry.sh/), [uv](https://docs.astral.sh/uv/) and Playwright's Chromium):

```sh
uv run --project backend python e2e/polygon_fork_flow.py --keep-stack
```

## Checks

All checks run through [pre-commit](https://pre-commit.com/) from `.pre-commit-config.yaml`: auto-fixers on commit,
and the full set (ruff, ty, vulture, xenon, semgrep, import-linter, deptry, forge, tsc, oxlint, knip, zizmor) on push.
Install the hooks once:

```sh
uvx pre-commit install --hook-type pre-commit --hook-type pre-push
uvx pre-commit run --hook-stage pre-push --all-files   # run everything by hand
```

The end-to-end scripts in `e2e/` start their own stack and are run by hand when a change needs them (PLAN.md
section 12).

## Security

See [SECURITY.md](SECURITY.md) to report a vulnerability.
