# xmr-scanner

pay-7331's Monero sidecar (PLAN.md section 14), a Rust service on monero-oxide (`monero-wallet` 0.2.0,
`monero-simple-request-rpc` 0.1.0; MIT, https://github.com/monero-oxide/monero-oxide). It answers the Python API's
wallet checks, subaddress derivations and txid checks over HTTP and runs one scan loop per Monero chain, writing what
it finds to `monero_payments`. It holds view keys only: it can watch, never spend.

## HTTP contract

Bodies are JSON. A 400 is `{"error": "<fixed message>"}` and never echoes an input. No request body is logged.

| Route | Body | 200 |
| --- | --- | --- |
| `GET /healthz` | | `{"ok", "chains": [{"chain_key", "ok", "node_count", "healthy_operator_count", "tip_height", "confirmed_tip_height", "scanned_height", "last_completed_tick_at", "unusable_wallet_count", "confirmed_tip_lagging_since"}]}` |
| `POST /v1/wallet-checks` | `{"network": "mainnet" \| "stagenet", "primary_address", "private_view_key"}` | `{"address_valid": bool, "view_key_matches": bool}` |
| `POST /v1/subaddresses` | the same plus `"major_index"`, `"minor_index"` | `{"subaddress": "..."}` |
| `POST /v1/transaction-checks` | `{"chain_key", "txid", "invoice_id"}` | `{"outcome", "amount_piconero", "confirmations"}` |

- `address_valid`: a standard (primary) address of that network with a good checksum: prefix 18 ("4") on mainnet, 24
  ("5") on stagenet. Subaddresses and integrated addresses are not valid here.
- `view_key_matches`: `a·G` equals the public view key inside the address (false whenever the address is not valid,
  the key is not a canonical scalar, or the spend key is torsioned, which monero-wallet refuses to scan for).
- `private_view_key` that is not exactly 64 hex characters, an unknown network or a malformed body: 400.
- `/v1/subaddresses` returns 400 unless the address is valid and the view key matches (a subaddress from a wrong key
  would burn the payer's money), and for (0, 0), which is the primary address. A subaddress is 95 characters, prefix
  42 ("8") on mainnet, 36 ("7") on stagenet, derived by monero-wallet's `ViewPair::subaddress`, the same table the
  scanner registers.
- `/v1/transaction-checks` (the payer's "I paid" with a txid, `src/transaction_checks.rs`): `counted` when a row of
  that txid and invoice is already in a block; else the transaction is fetched from the nodes in turn and decoded with
  the invoice wallet's view key: `confirming` when it pays the invoice's minor (in the pool, or in a block not
  recorded yet; the row is written then, as the scan loop would, after the same cross-check), `other_address` when it
  pays another minor or nothing of the wallet, `not_found` when every node that answered lacks it (and no node that
  held it failed to show its block). `unavailable` when the check could not be made: no node answered, a node that
  held it could not show a block listing it and no other node could either, the 20 s limit (or the wait for the
  chain's lock) ran out, or the invoice wallet's sealed key does not open (logged as `monero_wallet_unusable`, an
  operator problem); the API answers 503 and the payer retries. `failed` only for what is proven: the cross-check
  finds a mismatch with no operator confirming, or monerod flags a double spend. `amount_piconero` sums the invoice's
  outputs of it; `confirmations` counts from the tip (0 in the pool), both null for `other_address`, `not_found`,
  `unavailable` and `failed`. A txid that is not 64 lowercase hex, an unscanned chain or an invoice that is not a
  Monero invoice of that chain: 400. Checks run one at a time per chain on their own node connections, each node call
  limited to 6 s, starting at the node that answered the checker last.
- `healthy_operator_count`: operators with a node whose last call succeeded (every node is probed for its tip each
  60 s on a task of its own, all at once, each on a fresh connection dropped afterwards, 6 s per node and never longer
  than one poll interval; the scan loop takes the outcomes at its next tick, so a slow node never holds a tick).
- `tip_height`: the tip the last completed tick read (preferred node, failover); `confirmed_tip_height`: the lower of
  that and the tip a node of another operator reported in the same tick, null when none answered. Other operators'
  nodes are asked in turn until one reports at least `tip_height - 1`; when none does, the highest tip any reported
  counts (monerod's `get_height` answers whether or not the node is synced, so the first answer could be a stalled
  node's). `confirmed_tip_lagging_since` (Unix seconds): since when completed ticks have seen the confirmed tip more
  than 3 blocks under the tip, or null; null while it keeps up. The Python side alerts once it lasts 3 poll
  intervals. The Python watcher
  counts confirmations toward final against `confirmed_tip_height`, never `tip_height`, so one node's tip alone
  never makes money final. Both are published only from completed ticks: a stalled loop keeps the old values and its
  `last_completed_tick_at` (Unix seconds) ages.
- `scanned_height`: the last block the loop scanned (or followed, with no watched wallet). `unusable_wallet_count`:
  watched wallets whose sealed key does not open or match their address.
- `ok` (per chain): a tick completed within 3 poll intervals (counted from start before the first) and no wallet is
  unusable; the top-level `ok` is every chain's. The route answers 200 either way; the container probe
  (`xmr-scanner healthcheck`) fails unless `ok` is true.

## Settings

| Variable | Default | |
| --- | --- | --- |
| `XMR_SCANNER_LISTEN_ADDRESS` | `0.0.0.0:8750` | |
| `XMR_SCANNER_CHAINS` | empty | comma-separated chain keys (`monero`, `monero_stagenet`); empty serves HTTP only |
| `XMR_SCANNER_CHAINS_TOML_PATH` | `/app/chains.toml` | `backend/chains.toml`, mounted read-only; reads `kind`, `network`, `nodes`, `confirmations_final`, `poll_interval_seconds`, `recent_deposit_window_seconds` (default 86400) |
| `PAY_MONERO_NODES_<CHAIN>` | empty | overrides `nodes` exactly as the Python side: `url=operator,...`, mainnet `https` only |
| `DATABASE_URL` | | required with a chain; a `postgresql+psycopg://` URL is accepted |
| `PAY_INVOICE_SALT_SEED` | | required with a chain; opens the sealed view keys |
| `RUST_LOG` | `info` | `debug` adds a line per pool read |

The sealed view key opens exactly as `backend/app/shared/sealed_bytes.py` seals it: HKDF-SHA256 (no salt, info
`pay-7331 account monero private view key encryption v1`, 32 bytes) over the seed bytes, AES-256-GCM, blob = 12-byte
nonce then ciphertext and tag, associated data `{account_id}|{chain_key}|{primary_address}`, plaintext the key's 64 hex
characters. A row that does not open, or whose key does not match its address, is unusable: logged
(`monero_wallet_unusable`, ids only) on every tick, counted in `/healthz`, and the scan cursor holds until it is fixed
(no block is passed over for it; the pool and settled rows still run).

## The scan loop

Each `poll_interval_seconds`, per chain, in one task (so calls to the nodes are sequential, one connection per node):

1. Read the watched wallets (`account_monero_wallets` of the chain with `replaced_at IS NULL`, or replaced but with an
   invoice still open or closed within `recent_deposit_window_seconds`, since invoices keep their wallet and a late
   payment must still be found) and keep one monero-wallet `Scanner` each, with major `major_index` (1) and minors 1
   up to `next_minor_index + 500` registered.
2. Read the tip, failing over through `nodes` from the one that answered last, then the tip of a node of another
   operator (the first within a block of it, else the highest; the confirmed tip is the lower of the two).
3. Unless a watched wallet is unusable, fetch new blocks once each as pruned data (`get_blocks.bin`, 100 a round,
   validated by monero-interface), scan them on tokio's blocking pool (view tag, subaddress table, amount
   decryption, commitment check, all monero-wallet; a block a wallet fails to scan fails the tick and the cursor
   stays on it), drop outputs with
   `unlock_time != 0`, and for each output on (1, minor) with an invoice (`invoices.monero_wallet_id`,
   `subaddress_minor`) write the row with height and hash (a new row's `first_seen_at` is the block's timestamp, so
   a scan catching up after an outage values it at the quote live when it was mined). An output on a minor with no invoice, or on another index,
   is logged with ids and skipped. Outputs are deduplicated by output public key; the same key under a second
   transaction is logged (`monero_duplicate_output_public_key`) and never written.
4. Read the pool (`get_transaction_pool_hashes` and then that node's tip in one failover call, then `get_transactions`
   with `prune`, 100 a call, new hashes only; both fail over) and write sightings: no height, `in_pool = true`, `double_spend_seen` from monerod's flag (a row
   of the same transaction that had left the pool is marked back in it). Rows still without a block follow the pool
   each tick (`in_pool`, `double_spend_seen`) while in it or for 24 h after they left it; after that they are no
   longer read (the row stays, `in_pool = false`). When a pool transaction lands in a block its row gets height and
   hash and `in_pool` clears. A row missing from the pool is marked out of it only in a tick whose block scan
   completed through the tip that pool's node reported right after the pool read; otherwise (a scan that failed or
   held, or a block mined between the scan and the pool read) the departure waits a tick, so a transaction mined in
   between never looks like one that left unmined. The Python side still counts a row out of the pool as on its way
   for 10 minutes (`MONERO_POOL_DEPARTURE_GRACE_SECONDS`), for a pool read that failed over to a node lacking it.
5. Re-read the block hash of every row in a block until `confirmations_final` (and while a cross-check is pending). A
   vanished block sets height and hash back to null, `in_pool` back to true (monerod returns a reorged block's
   transactions to its pool; the next pool read corrects it), resets both cross-checks to `pending`, and rewinds the
   cursor so the transaction is found again wherever it lands. A reorg under the cursor (a new block whose parent is
   not the remembered hash) walks back to the common block the same way; a parent mismatch with no block gone fails
   the tick rather than looping.

Steps 4 and 5 run even when step 3 failed. A tick counts as completed (for `/healthz`) only when every step
succeeded. A panic aborts the process (`panic = "abort"` in both Cargo profiles), so Docker restarts it.

`credited_at` is never written here: crediting is the Python watcher's, from these rows.

### Cross-check

Before a row is first written with a height, and again once it has `confirmations_final` confirmations, the payment is
checked on nodes of other operators than the one the block came from: the block hash at that height matches, the
transaction is in that block's `tx_hashes`, and the amount re-decoded from that node's copy of the transaction
matches (with a scanner of the invoice's one subaddress). Every node of another operator is asked until one
confirms: `confirmed` when one does, `mismatch` only when at least one disagreed and none confirmed, else the state
stays `pending`, `monero_cross_check_pending` is logged with the healthy operator count, and the check is retried each
tick. The final check also reads that node's own tip and stays `pending` until the block is `confirmations_final`
deep there. A `mismatch` is asked again each tick until the row is past `confirmations_final`, so an operator that was
behind can still confirm it. The result goes to `cross_check_state` and `final_cross_check_state` with
`cross_check_operator`. A differing block hash counts as a disagreement only once the block has 2 confirmations
(before that the other node may sit on the other side of a fork race at the tip); a missing transaction or a
different amount is one at once.

### The cursor and restarts

The cursor is `monero_scan_cursors` (migration 0024; last scanned height and its block hash): read on start (from the
tip when the chain has no row), written after each tick and rewound with a vanished block, so blocks mined while the
sidecar was down are scanned when it comes back. With no watched wallet the loop follows the tip without fetching
blocks (the stored hash is then null). With watched wallets that are all unusable it holds instead.

## Logs

JSON lines, one event each (`event` field), ids and counts only: invoice, wallet, account and payment ids, minor
indexes, transaction hashes, heights, operator names and node indexes (position in the node list). Never a node URL
(it may carry credentials; error texts have it cut out), view key, sealed blob, database URL, seed or request body;
database errors are logged by SQLSTATE only.

## Running

- Compose: `docker compose --profile monero up -d xmr-scanner` (service `xmr-scanner` in the root
  `docker-compose.yml`; `PAY_XMR_SCANNER_CHAINS` picks the chains, default `monero`). `deploy/deploy.sh` builds and
  starts it, before the watcher, whenever the deploy env file sets a non-empty `PAY_MONERO_SIDECAR_URL`. The service gets
  `DATABASE_URL` and `PAY_INVOICE_SALT_SEED` only, no `env_file`, so no operator key reaches it.
- Locally: `cargo run` in this directory with the settings above.
- Fast checks: `cargo fmt --check` and `cargo clippy`. There are no unit tests; verification is a stagenet run
  (PLAN.md section 14).

## Not in this version

- Tor and `.onion` nodes, payment proofs.
- The Carrot / FCMP++ fork: monero-wallet 0.2.0 refuses blocks past hard fork version 16; upgrade the library
  before the fork activates.
