# pay-7331 plan

A self-hosted, Cryptomus-style payment gateway anyone can use. Code cites this file's section numbers; this file is
the design and interface reference.

## 0. Goal and the success test

Anyone opens `/`, taps $1, $2, $5 or $10 or types a USD amount, pastes the receiver address and gets a one-off invoice
link (`/i/inv_...`), with no history. Signing up with a passkey gives an
account (a username and a saved payout address): `/` then fills the receiver in, and its Options sheet has every
other invoice option (amount in USD, EUR or crypto, chain, time to pay, ...) and "How many payers", where more than one makes a
quantity link (`/l/lnk_...`) that up to that many payers pay, each with an invoice of their own (2026-10-11,
replacing "no reusable payment links"), and `/activity` lists the account's invoices, its links and their live
events. The payer opens the link, sees a QR, the address, the amount in crypto plus USD and EUR, and
the note, and pays within N minutes. Once the payment is final the page shows it paid, the account's activity page is
told, and the money is swept to the destination in one transaction. Underpaid invoices stay open for top-ups. Refunds
from the operator float (an overpaid excess, by itself once final, only to the address that sent it; an underpaid
amount on a signed request) exist only on invoices whose destination is the operator's own payout address
(section 6).

Done when the end-to-end script (section 12) passes on an anvil fork of Polygon mainnet: create, pay USDC and POL,
finalize, the events recorded, swept to the destination, underpay plus top-up, overpay plus automatic refund.

## 1. Stack

- Chain: Polygon PoS (chain id 137): POL (native, 18 decimals), USDC (Circle native,
  `0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359`, 6), USDT0 (`0xc2132D05D31c914a87C6611C10748AEb04B58e8F`, 6; on-chain
  `name()` is "USDT0", checked 2026-10-09; shown to payers as "Tether USD", 2026-10-11), and since 2026-10-11 USDC.e (bridged USDC, on-chain symbol "USDC"), WETH,
  WBTC, DAI, LINK, AAVE and UNI: a curated top list, each a plain ERC-20 priced by a live Chainlink USD feed
  (backend/chains.toml has addresses, decimals, feeds and how each was checked). Testnet: Amoy (80002), Circle test USDC
  `0x41E94Eb019C0762f9Bfcf9Fb1E58725BfB0e7582` (https://developers.circle.com/stablecoins/usdc-contract-addresses).
  Local: anvil fork of Polygon mainnet. EVM-generic: Base or Arbitrum is a config entry plus a factory deploy, plus
  one check the Chainlink client lacks (2026-10-13): on a rollup a feed can go stale or be read unfairly while the
  sequencer is down, so prices there must also read Chainlink's L2 Sequencer Uptime Feed and refuse a quote while it
  reports down or inside a grace period after it came back (Chainlink's example uses one hour;
  https://docs.chain.link/data-feeds/l2-sequencer-feeds). Polygon PoS is no rollup and has no such feed.
  Bridged assets (accepted risk, 2026-10-13): USDC.e, WETH, WBTC, DAI, LINK, AAVE and UNI are PoS-bridge tokens
  priced off their underlying's feed, and Chainlink feeds report the underlying's market price, not the bridged
  token's, so a bridge exploit can leave them worth less than quoted ("Extreme Events Causing Price Deviations in
  Wrapped or Bridged Assets", https://docs.chain.link/data-feeds/selecting-data-feeds). Nothing here detects that
  (no circuit breaker, no Proof of Reserve); turning an asset off is a chains.toml edit and a deploy.
  Upgradeable tokens (2026-10-13): 7 of the 9 tokens are upgradeable proxies (USDC behind Circle's FiatToken proxy;
  USDT0, USDC.e, WBTC, DAI, AAVE and UNI behind Polygon's `UChildERC20Proxy`; WETH and LINK are not; read from their
  implementation slots on a fork 2026-10-13), so the no-fee-on-transfer and no-rebasing check of 2026-10-11 holds only
  until an upgrade. Circle's USDC `transfer` is also `whenNotPaused` and `notBlacklisted` on both sender and
  recipient (https://github.com/circlefin/stablecoin-evm, `contracts/v1/FiatTokenV1.sol`) and USDT0 keeps a block
  list (`isBlocked(address)` answers on the fork), so a frozen deposit or destination reverts a sweep that lists
  that token (section 5, step 6).
- No own node: a Bor+Heimdall full node needs 32-64 GB RAM and 4-6 TB disk
  (https://docs.polygon.technology/pos/how-to/prerequisites/). Free public RPCs with failover, in order:
  polygon-bor-rpc.publicnode.com, polygon.drpc.org, tenderly.rpc.polygon.community, rpc-mainnet.matic.quiknode.pro,
  poly.api.pocket.network (https://docs.polygon.technology/pos/reference/rpc-endpoints/; polygon-rpc.com, Nodies,
  Ankr, OnFinality want a key or plan, 1rpc.io is dead). No block explorer API: everything comes over JSON-RPC.
  Keyless `eth_getLogs`, measured 2026-10-12 (sources and refusal texts beside each in chains.toml; every one served
  200 `eth_blockNumber` at 10 a second without a refusal; "deep reads" are 40 reads of 2,000 blocks of LINK
  Transfers 1M blocks back at 2 a second):

  | Endpoint | Widest range | History | Deep reads | Documented limits |
  |---|---|---|---|---|
  | publicnode | 10,000 blocks (-32701 "exceed maximum block range: 10000"; 97 on 2026-10-12) | at least 100,000 blocks, not 5M ("Archive requests require a personal token"; 98 blocks on 2026-10-12) | 40 refused | none found |
  | dRPC | 101 blocks (the refusal says "over 10000") | full | 40 refused (range) | 120,000 CU a minute per IP, 2 s timeout, 10,000 logs |
  | Tenderly community | 10,000 blocks | full | 40 passed | "no rate limits for basic usage"; 3,000 logs (5,912 passed) |
  | QuickNode public | 10,000 blocks (3.7 s at 10,000) | full | 40 passed | none found for the public endpoint |
  | Pocket Network | 100,000 passed | full (once "historical state is not available") | 37 passed, 3 relay timeouts | 2,000 a minute per IP |

  The watcher starts an endpoint's log reads at 2,000 blocks, halves a range it refuses (range, result count or
  timeout) down to 100 blocks, keeping the narrowed range while ticks stay on it, and fails the tick over on a
  history refusal or a refusal at 100 blocks (section 5, 2026-10-12). publicnode's depth has moved (98 blocks on
  2026-10-12, at least 100,000 on 2026-10-14), so the watcher reads its refusals, not a fixed depth; behind its
  history, dRPC catches up at 100 blocks a read, Tenderly at 2,000.
  Rate limits (2026-10-14): an endpoint that answers HTTP 429, a 503 with `Retry-After`, or a rate-limit JSON-RPC
  error (QuickNode's -32007, -32008, -32011, -32029, https://www.quicknode.com/docs/ethereum/error-references; not
  -32005, which QuickNode's references list for a range or result too large) is set aside for its `Retry-After` (seconds or an
  HTTP-date, at most 600 s), else 60 s for a per-minute limit (-32008) and 10 s for the rest: no request goes to it
  meanwhile, and a failover passes over it while another endpoint is free. QuickNode's public endpoint answers -32007 past 20 requests a
  second, so a busy tick on it moves on instead of hammering it.
- Finality: the `finalized` block tag. Heimdall v2 gives deterministic finality in 2-5 s and finalized blocks are
  irreversible (https://docs.polygon.technology/pos/concepts/finality/finality); measured 2-3 blocks (3-5 s) behind
  `latest` on 2026-10-09. A finality stall happened once after that upgrade (https://www.theblock.co/post/370249), so
  the page shows "confirming" for as long as it takes; nothing times out on it. `safe` is not served at all
  ("safe block not found" from all five endpoints, 2026-10-13).
- Prices: Chainlink feeds read with `eth_call` over the same RPC, no price API. Polygon proxies (8 decimals, all live
  2026-10-09): MATIC/USD (POL) `0xAB594600376Ec9fD91F8e885dADF0CE036862dE0`, USDC/USD
  `0xfE4A8cc5b5B2366C1B58Bea3858e81843581b2F7`, USDT/USD `0x0A6513e40db6EB1b165753AD52E80663aeA50545`, EUR/USD
  `0x73366Fe0AA0Ded304479862808e02506FE556a98`; the feeds of the 2026-10-11 assets are in chains.toml (USDC.e reads
  USDC/USD, WBTC reads BTC/USD: WBTC/USD is low-risk but has a 300 s heartbeat, which the chain-wide 27 s heartbeat
  check would alert on, 2026-10-13). A quote refuses a feed older than the chain's
  `maximum_price_age_seconds`, and one stale feed refuses every price of the chain, so only fast-heartbeat feeds
  (27 s on Polygon) are listed. A read at `latest` sees the endpoint's own head, so an endpoint lagging behind the tip
  serves an old round: a stale answer fails the read over to the next endpoint like a down one, and a quote is refused
  only when no endpoint gives a fresh answer (2026-10-13).
  Amoy has some feeds but no POL/USD (Chainlink's directory, 2026-10-13), so Amoy reads the mainnet feeds through a
  mainnet RPC (prices only).
- Batch reads: Multicall3 `0xcA11bde05977b3631167028862bE2a173976CA11` (deployed on Polygon and Amoy, checked
  2026-10-09).
- Fees, measured 2026-10-09: base fee about 245 gwei, `eth_maxPriorityFeePerGas` 30 gwei, Polygon's documented minimum
  tip 25 gwei (https://docs.polygon.technology/tools/gas/polygon-gas-station.md). A 100k-gas sweep costs about 0.03 POL
  (well under $0.01).
- Backend: Python 3.15, uv, FastAPI, SQLAlchemy 2 async + psycopg 3, Alembic, Postgres 18, Redis 8, web3.py 8
  (`AsyncWeb3`, batch requests), eth-account (EIP-1559 signing, EIP-191 recovery), segno (QR as SVG),
  py_webauthn (passkeys), httpx, structlog, typer. Layout and tooling follow a house backend template (domains under
  `app/payments/`, typed log events, import-linter contracts).
- Contracts: Foundry (forge 1.8.5), Solidity 0.8.x, OpenZeppelin Contracts 5.7.0 (`Clones` with immutable args since
  5.2.0, `SafeERC20`).
- No Hishel, cachetools or caching library: RPC calls are POSTs Hishel does not cache by default and RPCs send no
  cache headers (https://github.com/karpetrosyan/hishel); the few caches (ip-api verdicts, price reads) are Redis keys
  with TTLs owned by a domain's `store.py`.

## 2. Processes and data

| Process | Command | Does |
|---|---|---|
| api | `uvicorn app.main:app --workers ${PAY_API_WORKERS:-2}` | REST API, the pages, QR SVGs, the account event stream (one shared poller per process, section 8.1); webhook and Discord delivery every 2 s, the watcher liveness and ip-api key checks once a minute (sections 8.2-8.4, 9), stopped within 5 s on shutdown (`--timeout-graceful-shutdown`), in every worker: deliveries claim rows with `SKIP LOCKED`, operator alerts record under an advisory lock (2026-10-13) |
| watcher | `python -m app.watcher` | per chain: watch deposits, send sweeps and refunds; once a minute the operator key look (leak check, treasury top-ups, retired keys' returns, section 5.1); every 15 s the invoice schedules' due invoices (section 18); a Redis heartbeat per completed tick (its container healthcheck, `python -m app.watcher_healthcheck`) and operator alerts once a minute |
| cli | `python -m app.cli ...` in the watcher container (`docker exec pay-7331-watcher ...`; the api's holds no operator key, section 5.1) | the operator's tools: `reconcile` (read only, exit 1 when something needs the operator), sweep one invoice by hand, settle a `needs_owner` refund, record stray money from a transaction hash, find a deposit again without Postgres (`recover-deposit`), fetch the coin logos, record a host alert (`record-operator-alert`, the backup units), the operator keys (`operator list-keys`, `retire-key`, `unfreeze-key`) and the treasury Safe (`treasury create-safe`, `setup-transactions`, `execute-setup`, `status`; section 5.1) |

One replica each; the watcher holds a Postgres advisory lock so a second copy idles. Postgres is the only source of
truth (accounts, invoices, payments seen, outgoing transactions, account events). Redis holds copies with TTLs only
(rate-limit budgets, ip-api verdicts, price cache); losing it loses nothing.

Backups (production, `deploy/`; 2026-10-12): two sealed copies in R2, so a dead disk loses about the last minute.
Postgres archives every WAL segment with WAL-G (`archive_timeout` 60 s, libsodium client-side, zstd), onto a daily
full base backup; the bucket's 90-day expiry is the retention. An hourly `pg_dump` stays beside it for a one-table
restore. The weekly drill restores both into throwaway containers and checks them against production. WAL-G, not
pgBackRest: the bucket's 7-day object lock refuses rewrites, and pgBackRest rewrites its `archive.info`,
`backup.info` and backup manifests (tried 2026-10-12: R2 answered 409 `ObjectLockedByBucketPolicy`), while WAL-G
writes each key once. Not a plain `archive_command` either: that is a hand-rolled upload with no base backups or
restore tooling. The owner hears of a failure (2026-10-12): the watcher's once-a-minute checks read `pg_stat_archiver`
and alert `wal_archiving_failing` or `wal_archiving_stalled` (section 5 step 8), and a failed backup or restore drill
runs `docker exec pay-7331-api python -m app.cli record-operator-alert --kind backup_failed` (or
`backup_restore_drill_failed`) from its systemd unit, beside its marker file and journal line; each is an
`operator.alert` (section 8), shown on the owner's `/activity` and sent to the operator account's Discord (section 8.4).

## 3. Deposit addresses (contracts/)

Each invoice gets a fresh deposit address: a CREATE2 clone (OpenZeppelin `Clones`, immutable args) of
`DepositForwarder` whose args are the invoice's destination address, made by `DepositForwarderFactory`. The address
exists before any code is deployed there; the first sweep deploys the clone and forwards in one transaction.

- The factory has no owner, no operator and no refund path. The only way money leaves a deposit address is to its own
  destination, so no key on the server can redirect a deposit. The server's operator keys only pay gas and, the
  refund key, refunds out of its own small float (sections 5.1 and 6); a leaked key loses what it holds, never a
  deposit.
- The destination is part of the init code, so a different destination is a different address.
- `sweepToDestination` is permissionless and idempotent: it forwards the whole native balance and the whole balance
  of each listed token, so anyone (the owner with `cast send`, if the server is gone) can push funds to the
  destination, and a repeat call only moves what arrived since.
- Anyone can deploy a clone early by sweeping. Once an address has code, a native send needs about 24k gas, so a
  wallet that sends with a fixed 21,000 limit fails; the page asks payers to use a wallet that estimates gas. The
  plan never promises codeless top-ups, it only avoids deploying early itself (section 4).

```solidity
contract DepositForwarder {            // implementation; a clone reads its destination with fetchCloneArgs(address(this))
    address public immutable factory;
    receive() external payable;
    function destination() public view returns (address payable);
    function forwardAll(address[] calldata tokens) external returns (uint256 nativeAmount, uint256[] memory tokenAmounts); // only factory
}
contract DepositForwarderFactory {     // no owner, no admin, no upgrade
    address public immutable implementation;
    function predictDepositAddress(bytes32 invoiceSalt, address destination) external view returns (address);
    function sweepToDestination(bytes32 invoiceSalt, address payable destination, address[] calldata tokens) external; // anyone; nonReentrant
    event DepositSwept(address indexed depositAddress, address indexed destination, address indexed token, uint256 amount); // token 0 = native, one per non-zero amount
}
```

- Clone deployment is skipped when the address already has code. A zero destination reverts. Native forwarding
  requires the call to succeed, so a destination that rejects native coin makes the sweep revert with no event; token
  transfers use `SafeERC20`. No arbitrary calls, approvals or hooks.
- `invoiceSalt` is HMAC-SHA256 of the invoice id under `PAY_INVOICE_SALT_SEED` (32 secret bytes in `.env`; 32 bytes
  is CREATE2's salt size, https://eips.ethereum.org/EIPS/eip-1014), and every invoice row still stores it; invoices
  made before 2026-10-12 keep the random salt they stored. The backend gets the address from
  `predictDepositAddress` (one `eth_call`), never from its own copy of the init code. One factory per chain, deployed
  with a forge script.
- Recovery without Postgres (2026-10-12): the API's `invoice_create` log line names the invoice id, chain, deposit and
  destination (never the salt or seed). `python -m app.cli recover-deposit --from-log FILE` (or `--invoice-id
  --destination [--chain]`) re-derives each salt, confirms the address with `predictDepositAddress`, prints its
  balances and the `sweepToDestination` call, and sends nothing; it needs no database. The Docker log sits on the same
  disk, so this covers a lost database, not a lost disk (that needs the WAL archive, section 2). The salt stays secret:
  `sweepToDestination` is permissionless, so anyone holding one could sweep that deposit (to its destination only)
  before the watcher has read and settled it.
- The backend refuses a destination that is zero, the factory or a deposit address, and one with code unless the
  request sets `allow_contract_destination` (a Safe receives native coin fine).

Foundry tests (only these two): the main path (native and USDC to a predicted address, sweep, destination receives,
predicted equals deployed, a second sweep moves only new money), and the failure that would hurt most (whoever calls,
funds only ever reach the destination; a destination that rejects native coin reverts the sweep and emits nothing).

## 4. Invoice lifecycle and money rules

Amounts are integers in the asset's base units in Postgres (`numeric(78,0)`) and decimal strings on the wire. Fiat
amounts are `numeric(20,2)`. Only configured assets are accepted (no fee-on-transfer tokens).

- Gross received at a finalized block B = deposit balance at B + every `DepositSwept` amount for that deposit and
  asset mined at or before B, whoever sent the sweep. Detection reads balances, so internal transfers count too.
  Decisions use finalized readings only; `latest` drives the "confirming" display.
- Quote: when the asset is known, the fiat amount is converted with the Chainlink feeds (answer > 0, scaled by the
  feed's decimals, younger than `maximum_price_age_seconds`) and rounded up to the asset's `quote_decimals` (POL 4,
  stablecoins 2, the others one step under a cent: WETH 6, WBTC 7), so the amount stays short to type. A USD
  stablecoin (`usd_peg`) is priced at exactly 1 USD within 0.5% of the peg and at its feed price but never above 1 USD
  off it (2026-10-13), so a feed above the peg never asks fewer coins than the fiat amount's USD worth. It holds until
  `expires_at` (lifetime, default 30 min, 5 min to 12 h, as Cryptomus). A crypto-denominated invoice has no quote.
- Valuing money in a fiat invoice: each finalized rise in gross received is a credit valued at the quote live at the
  block time of the newest final payment in the rise, or, when none is live, at the price (pegged and capped as above) when the watcher sees
  it final. A payment mined inside its quote keeps that quote however late it is finalized or credited (2026-10-09:
  valuing at the finalized head's time underpaid a payer who paid in time); a rise no payment row explains uses the
  finalized head's time. So a dust payment cannot hold an old price open. An underpaid invoice whose quote has lapsed gets a fresh quote for the remainder when
  the payer opens it (`POST /public/invoices/{id}/quote`, one live quote at a time).
- `accuracy_percent` (default 1.0, 0-5): paid once the shortfall is at most `accuracy_percent` of the amount (as
  Cryptomus `accuracy_payment_percent`). An excess below `minimum_refund_usd` (default 1.00) is kept.

| Status | Meaning |
|---|---|
| `awaiting_asset` | invoice offering several assets whose payer has not picked one yet |
| `awaiting_payment` | quote live, nothing seen |
| `confirming` | money seen at `latest`, not yet final |
| `underpaid` | final but short; stays open for top-ups, unlimited sends, shows the remainder |
| `paid` | final and within accuracy (`partial = true` when the account accepted less) |
| `overpaid` | final and the excess is at least `minimum_refund_usd`; on an operator-destination invoice the excess goes back to its sender, elsewhere the receiver keeps it |
| `expired` | nothing final by `expires_at` (a `confirming` invoice waits for finality first) |
| `cancelled` | cancelled by its account with nothing received, or an underpaid amount refunded in full |

- Underpaid stays open until `underpaid_open_until` (null = forever, the default). The invoice's
  account may `accept-partial` (refused once a refund was made on the invoice, 2026-10-09); on an operator-destination
  invoice the payer may ask for the partial amount back once `expires_at` has passed (section 6). A signed-out
  invoice's underpaid money stays in the deposit for top-ups; it reaches the receiver when topped up, when anyone runs
  the factory's permissionless sweep, or when the operator runs `sweep-invoice`.
- Creation: an account invoice (`POST /invoices`) or a signed-out one (`POST /public/invoices`, USD, the first
  chain, the assets its maker ticked or else every asset, 30 minutes, 1% accuracy). A fiat amount is at least 1.00 (USD or EUR); every web creation is
  counted against the per-IP open-invoice cap (section 9), never screened (only sign-up is). Each invoice keeps the salted hash of its creator's
  network (`client_ip_hash`), never the IP.
- Refreshing an expired order (2026-10-21, Cryptomus `is_refresh`): `POST /invoices` with an `order_id` and
  `refresh_expired: true` whose current invoice is `expired` makes a new invoice instead of returning that one. The new
  one is an ordinary invoice from this request's fields: a new id, so a new salt (HMAC of the id, section 3) and a new
  deposit, a new payment window and quote, admitted by the same caps; it carries the order id and
  `refreshed_from_invoice_id`. The old row changes in one column only, `refreshed_into_invoice_id`: status, amounts,
  deposit and destination stay, so the rescan keeps watching its deposit and money that reaches it later is late money
  on the old invoice (swept to its destination, `late_payment`, `invoice.late_payment` with the same order id), never a
  credit on the new one. One deposit belongs to one invoice and each invoice counts only its own deposit, so nothing
  can be credited twice and nothing leaves the watch. Why not reopen the old row with a new window (Cryptomus keeps
  the invoice): money paid between expiry and refresh would then count as on time, and money the rescan already
  flagged late and swept would be credited again by the reopened invoice's gross; a new address for the same id would
  need a second salt per invoice and would drop the first deposit from every place that reads one deposit per invoice.
  Refused (409 `invoice_not_refreshable`) once money ever reached the old invoice (a payment or stray row, final or
  not, `late_payment`, or any credited or seen gross) or its deposit holds, at `latest`, at least the minimum sweep
  amount of any coin its chain is configured with (one Multicall3 read under the row lock, so money mined since the
  watcher's last pass counts too): a fresh invoice could make that payer pay twice, so the account settles it first.
  A payment still in the mempool cannot be seen: it lands later as late money on the old invoice, so a
  `late_payment` on a refreshed order means the payer paid twice and the account settles the second payment with
  them. An XMR invoice is checked on its recorded rows only. Any other status, or `refresh_expired` left out, returns the current invoice unchanged, as
  before. Under the per-account lock (a token) or none (a session), the old row is locked `FOR UPDATE` before
  anything is checked and stays locked until the new row is in, so two refreshes make one invoice; the loser returns
  the winner's. One current invoice per order: `ux_invoices_account_id_order_id_current`, unique `(account_id,
  order_id)` where `refreshed_into_invoice_id` is null (migration 0032), which `?order_id=` and a repeated `order_id`
  read. A payment already on its way to the old deposit at refresh time arrives as late money there; nothing reads the
  chain at refresh.
- Sweeps: everything that reaches a deposit goes to its destination, because the contract allows nothing else. The
  watcher sweeps once an invoice is `paid` or `overpaid` (`accept-partial` makes it `paid`) and its finalized deposit
  holds money, and when an underpaid invoice closes (refunded, or `underpaid_open_until` passed) if its last reading
  holds at least the minimum sweep (2026-10-09, live: a third party had emptied it, and the gateway paid a sweep of
  nothing); it leaves an underpaid deposit alone so top-ups go to a codeless address.
- Late and stray money: every deposit address ever made is rescanned (Multicall3, all configured assets, at the
  finalized block) in two tiers (2026-10-13; one pass over every deposit outgrew its 10 minutes at about 45,000
  deposits): recent deposits, whose invoice was made within `recent_deposit_window_seconds` (a day) or is not closed
  yet, every `rescan_interval_seconds` (10 minutes), and every other deposit every
  `old_deposit_rescan_interval_seconds` (a day), so late money on an old deposit can wait that long, and one that
  just left the recent window waits for the next old pass (up to about 25 hours; `reconcile` may list it as unswept
  meanwhile, and the old pass sweeps it without the owner). Each tier's
  pass fixes its cutoff when it starts and reads a bounded slice a tick, in (created_at, id) order on the invoices'
  indexes, sized to end in about half its interval (at least one 500-call chunk). Both share at most 3,000 balance
  calls a tick (300 deposits with 10 assets): the recent tier goes first, leaving one 500-call chunk to a running
  old pass, and the old tier reads what is left. A pass that overruns its interval is logged with its tier. The
  recent tier's pass is held in memory, so a restarted watcher starts it afresh (2026-10-09). The old tier's running
  pass (cutoff, position, pace) and its last start are copied to Redis after every applied slice with a TTL of twice
  its interval, so a deploy resumes it and keeps its interval; losing the copy only restarts the pass from the
  oldest deposit (2026-10-13). Money found on an invoice that is
  closed (expired, cancelled, paid and swept) is swept to the destination and flagged `late_payment` with an
  `invoice.late_payment` event; the receiver settles with the payer. Money in an asset the invoice did not ask
  for is handled the same way. Only money past what the invoice already counted is late: all that ever reached the
  deposit in an asset (its balance plus finalized sweeps) against `received_base_units` in the invoice's own asset
  and its stray rows in any other (2026-10-13: a lapsed underpaid invoice's credited USDC, left in the deposit by
  `sweep-invoice --skip-token USDC`, was flagged late). Nothing is ever left unwatched.
- Stray money on a live invoice (INC-001, 2026-10-11; a 25 USDC invoice got POL instead and the payer page showed
  nothing): the watcher sees money in any other configured coin within a tick (section 5) and records it as
  `invoice_stray_payments` rows (asset, sender, tx, block, final). It is never credited and never changes the status
  or amounts; it is swept to the destination with the rest, and money those rows explain is not flagged late once the
  invoice closes. `invoice.stray_payment` is recorded when a row goes final. The payer page and `/activity` show it,
  and it goes back to its sender by itself where the refund rules allow (section 6). Stray money
  the watcher never saw (a closed deposit, as INC-001 was) is recorded from its transaction hash by the operator's
  `record-stray-payment-from-transaction`, or by the payer's "I paid" with that hash (section 7, 2026-10-12); neither
  credits the invoice.
- Dust (2026-10-09, after a live check showed 1 wei on an old deposit made the operator pay a sweep's gas): each
  asset has a `minimum_sweep_amount` in chains.toml (about 0.05 USD: 0.5 POL, 0.05 of a stablecoin, 0.00002 WETH). No invoice is made
  for less (a crypto amount at creation, a fiat amount at its whole-amount quote; an underpaid remainder can be any
  size). A deposit the gateway swept before (its own final sweep; a third party's does not count, 2026-10-09: after
  one, a payer's 0.3 POL top-up was left behind), a closed deposit and a lapsed underpaid invoice are swept, and late
  money flagged, only at or above it; below, the dust stays in the deposit (it can only ever go to the destination)
  until more arrives, or the operator runs `sweep-invoice`.
- A sweep that fails (frozen token, a destination that rejects native coin) marks the invoice `sweep_blocked` and
  records `invoice.sweep_failed`; the money stays in the deposit until the operator acts.

## 5. Chain watcher

Per chain, every `poll_interval_seconds` (default 2, about a block), against one RPC for the whole tick (failover
switches between ticks, never inside one):

1. Read `latest` and `finalized` (number and hash). A finalized number lower than the last processed one is ignored.
2. One Multicall3 `aggregate3` pinned to the finalized block hash (EIP-1898, https://eips.ethereum.org/EIPS/eip-1898)
   and one at `latest` for every open invoice: `getEthBalance` or the token's `balanceOf` (chunks of 500).
3. `eth_getLogs` for the factory's `DepositSwept` up to the finalized block (five ranges of up to 2,000 blocks a
   tick), each event stored once by chain, transaction hash and log index. A range refused for its size or result
   count is halved, down to 100 blocks, and stays narrowed while ticks stay on that endpoint; a refusal at 100 blocks
   or under (so any at the tip) and a history refusal fail the tick, so the next one runs on the next endpoint
   (2026-10-12, section 1). Token Transfer reads (step 5) do the same. Both log streams reread the last 32 finalized
   blocks every tick, since a load-balanced endpoint can answer part of a range without an error (2026-10-13).
4. Hand the readings to the invoices service, which applies section 4 and writes status changes, sweep requests and
   event rows in one transaction. Then the rescan reads one slice of each tier's pass, recent and old (section 4,
   late and stray money; 2026-10-13).
5. Attribution (who sent what, for display and refunds): token payments from `eth_getLogs` Transfer events with `to`
   = watched addresses (one filter per 100 deposits naming every token they are watched in, so the reads do not grow
   with the asset count; a transfer counts only in the deposit's own token, 2026-10-11; the filter names every
   configured token and a transfer in another token is a stray row, INC-001, 2026-10-11); native payments by fetching only the blocks between the last reading and the one where the
   balance rose and matching `tx.to`, kept only when its receipt succeeded (a reverted one moved nothing,
   2026-10-09). A sweep can hide a rise at `latest` (swept 0.3, paid 0.3: the same balance), so a deposit whose
   finalized gross rose past what was credited also gets the blocks finalized since the last tick (2026-10-09, live).
   A Transfer read whose `toBlock` is past the answering node's head (a load balancer's backend a block behind) stops
   that tick's reads at the finalized block instead of failing the tick (2026-10-13).
   A rise no direct transaction explains (a contract sent it) is the unknown sender's share, never a row
   (`accounting_service.py`; checked live 2026-10-09). Each payment row keeps its block hash and block timestamp (a
   native row's from the fetched block, a token row's from its block's head, since a log entry need not carry one)
   and is rechecked once its block is final; a row from a block already final when read goes in final. Transfer logs
   are read from the last applied finalized block to `latest` every tick, so a reorged range is read again and every
   block once while final. A range's filters are read at most 4 at a time (the free endpoints rate limit per IP), all
   of them before any is written, so a refused one rereads the range narrowed (2026-10-13). Finality is settled per transaction and invoice: rows still at their canonical block go
   final with the canonical header's timestamp; otherwise the receipt decides. A reorg can re-mine a token transfer
   at another log index (`logIndex` is the log's position in its block,
   https://ethereum.org/en/developers/docs/apis/json-rpc/), so the rows become exactly the receipt's Transfer logs of
   the invoice's token into the deposit (a native payment: the transaction at its new block), with the new block's
   hash and header timestamp, and are dropped when the receipt is missing or failed. One transfer is one row, at its
   canonical block and log index, so the refund split never counts it twice (2026-10-09).
   A live token invoice's deposit also has its native balance in the `latest` aggregate3 (one call more per deposit,
   no extra eth_call below 500 calls); a rise is matched by the same block scan, from the last reading's block, into
   native stray rows; a rise no transaction explains is one stray row with no sender. Stray rows go final by the same
   per-transaction check, and one with no transaction whose block left the chain is deleted (INC-001, 2026-10-11).
   A rise the scan cannot reach (more than 200 blocks back, a stalled watcher) is a no-sender row too, so the owner
   settles it; `record-stray-payment-from-transaction` later attributes it, shrinking that row by what the proven
   transaction sent at or before its block. A token deposit whose native balance read failed is not applied that
   tick, so its scan never skips blocks (2026-10-12).
6. Outgoing transactions (sweeps, refunds and the key operations of section 5.1) are operations with attempts, each
   signed by the operator key it names. A durable nonce allocator row per operator key hands out each nonce once. An attempt is signed and stored (raw transaction and hash) before broadcast; a
   restart rebroadcasts stored attempts. EIP-1559 with `maxPriorityFeePerGas` = max(node suggestion, chain minimum)
   and `maxFeePerGas` = 2 × base fee + tip, neither above the chain's `maximum_fee_per_gas_gwei` (2000 on Polygon,
   2026-10-09); not mined after `fee_bump_after_seconds` means a replacement at the same nonce with both caps raised
   by at least 12%, while a full raise still fits under that cap. Once the operator's nonce at `latest` has passed
   the operation's, nothing is bumped or rebroadcast (no attempt at a used nonce can mine); five minutes after the
   newest attempt with no receipt of ours on any endpoint that answers (at least two when the chain has two; a dead
   one is skipped, 2026-10-09), a sweep fails and a refund becomes `needs_owner` (section 6). Every attempt's receipt is reconciled, exactly one can mine, and an unresolved operation is never retried at a
   new nonce. An operation is done when its block is final; what moved is
   read from the receipt (or its `DepositSwept` events), not from what was asked.
   A sweep lists only the tokens its deposit holds at `latest` when the attempt is signed (2026-10-11): the
   contract calls every listed token and one failing call reverts the sweep, and each empty listed token cost about
   11,000 gas on a fork (all 9 listed: 206,430 against 118,547 for USDC alone). A token arriving later is stray
   money the rescan sweeps. A token whose transfer reverts (USDC blacklisting the deposit or the destination, a
   paused token) reverts the whole sweep, native coin and every other token included, and the invoice is
   `sweep_blocked`: `sweep-invoice <id> --skip-token USDC` (a configured token's symbol, repeatable; the native coin
   is always forwarded and cannot be skipped) records a sweep that never lists it (its operation's
   `skipped_token_addresses`, kept by a frozen key's replacement sweep), so the rest moves (2026-10-13). Each moved
   token's `DepositSwept` event is counted as before; the skipped token stays in the deposit, and the next sweep
   that lists it fails again, so the invoice is `sweep_blocked` until the owner sweeps it once it can move. On a
   closed invoice that next sweep is the rescan's, and a skipped balance the invoice credited or recorded as stray
   money is not a late payment (section 4).
   Gas limit (2026-10-09): the estimate plus 25% and 10,000; a sweep 40,000 more per configured asset (dust landing
   between estimate and send, or a replacement listing a token received since, adds a transfer); exactly 21,000 for a native transfer to a plain account, the estimate
   rule when the recipient runs code (an EIP-7702 account, a contract). A native refund's float check runs before its
   estimate; an operator that cannot pay a sweep's gas leaves it waiting.
   A pass's chain reads do not grow as serial calls per operation (2026-10-13, for chains with ~17-minute
   finality, where operations wait in `mined` for hundreds of ticks): before the first step, one JSON-RPC batch reads
   the receipt of every `broadcast` attempt and of every `mined` operation whose block is final, then, only when a
   step needs them, the operator's nonce at `latest` and the node's tip suggestion, once each; a `mined` operation
   above the finalized head reads nothing. A failing batch is read again one request at a time, and a read that
   still fails fails only the steps that need it; a failing tip suggestion falls back to the chain's minimum tip
   (`settlements/pass_readings.py`, 2026-10-13). Per operation stay only the reads that
   decide money or a nonce: a sweep's held tokens, a refund's float, the gas estimate, the pending nonce count right
   before the nonce is taken, and the every-endpoint receipt check before an unproven end (the tick's endpoint
   asked again). Each operation's step runs on its own, so one failing never stalls the others. An operation's new
   status (`final`, `failed`, `needs_owner`, `waiting_for_float`) commits in one transaction with its effect on the
   invoice (`sweep_blocked`, an underpaid invoice's cancel) and its events, so a crash or error between them rolls both back and the next pass resolves it
   again, never twice (2026-10-12).
7. Last, in its own transaction: automatic refunds deferred for want of a price (section 6) are decided again, from
   one indexed query (`invoices.automatic_refund_deferred_since`); while no feed answers it stops after one price
   read (2026-10-12).
8. After a completed tick (2026-10-12): the chain's heartbeat in Redis (`SET` with a TTL; the watcher container is
   unhealthy after about 1.5 minutes without one), and once a minute the operator alert checks: the applied finalized
   head more than 150 blocks behind, a feed older than `price_feed_heartbeat_seconds` plus a minute or unreadable,
   a sweep or refund unresolved after 30 minutes, a refund `needs_owner`, a failed sweep (`sweep_blocked`), refunds
   waiting for a float that cannot pay them, an operation whose step failed 10 passes in a row, and, while
   `archive_mode` is on, Postgres's WAL archiving failing (a failure newer than the last success, none for 5 minutes)
   or stalled (a segment waiting, nothing archived for 15 minutes), read from `pg_stat_archiver`. Each is an
   `operator.alert` event (section 8) when the problem opens, then at the Discord reminder cadence (section 8.2).
   `python -m app.cli reconcile` runs the same
   checks plus those that read more (unapplied outcomes, a failed sweep left unblocked and unswept, an underpaid
   invoice refunded in full, stray
   rows with no sender, refunds deferred over 30 minutes, every deposit's balance at the finalized block) and exits 1
   when any finds something. A dead or stuck watcher raises nothing, so the API process reads the heartbeat once a
   minute and records `operator.alert` `watcher_not_ticking` once none came for 120 s (not in the API's own first
   120 s), once per chain while it lasts (section 8.2; 2026-10-12); Monero chains too while a sidecar is configured, since a Monero
   tick writes its heartbeat only after reading the sidecar, so a dead sidecar alerts as well (2026-10-14). No host cron runs `reconcile`: the watcher's once-a-minute
   checks cover what can alert cheaply, the API covers the watcher itself, and both reach the owner as `operator.alert`
   events (section 8.2) on `/activity` and the operator account's Discord (section 8.4); `reconcile` stays the operator's deeper look by hand (2026-10-12).

### 5.1 Operator keys and the treasury Safe (2026-10-13)

Keys per chain: one refund key (`PAY_OPERATOR_PRIVATE_KEY_<CHAIN>`) holding the refund float and paying refunds,
and a sweeper pool (`PAY_SWEEPER_PRIVATE_KEYS_<CHAIN>`, comma-separated) holding gas only. Each new sweep goes to the
active sweeper with the fewest active operations (ties: the lowest address); with no sweeper set, the refund key
sweeps as before. A pending sweep whose key stopped being usable moves to another sweeper when it is signed; one
already signed stays on its key and its nonce. Each key has its own nonce row and its state in
`operator_key_states` (`active`, `frozen`, `retired`; role, reason, when). Code: `settlements/operator_keys.py`,
`operator_key_service.py`, `operator_key_maintenance_service.py`, `treasury_service.py`.

Leak check: once a minute (before that tick's settlement pass) the watcher reads every configured key's nonce at
`latest` in one batch, then the nonce rows. The allocator commits a nonce before its attempt is sent, so a key whose
`latest` nonce is past its row's `next_nonce` signed something this system never handed out: it is frozen, alerted
`operator_key_used_elsewhere` (the message names the key's row id and address) and never signs or gets work
again; its stored attempts are still rebroadcast (top-ups excepted) but never bumped. Its unmined sweeps end `failed`
with `operator_key_frozen` once the newest attempt is five minutes old and every endpoint was asked for a receipt;
that reason never sets `sweep_blocked`, and the same transaction asks for a new sweep on another key (left to the
rescan, a closed invoice's money would wait for its pass); payer and account pages do not list the ended one. A
late mine of the old one is harmless: `sweepToDestination` is permissionless and pays only the destination baked into
the deposit, and the second sweep then moves what is left, possibly nothing, without reverting. Its unmined top-ups are
never rebroadcast and end the same way, so no more allowance flows into a leaked key (the key a top-up fills may then
get it from another sender next look). A top-up another key sends for a key that froze after its first attempt goes
on, resending the stored call: the frozen key signs nothing, the sender's nonce is used, and the leaked delegate could
pull that allowance itself anyway until the owner removes it. Its refunds and other key
operations end by the nonce-taken-elsewhere path; a refund is never issued again this way. A nonce an operation ended
unused stays a gap before the key's next one, so `operator unfreeze-key` and `operator retire-key` refuse a frozen
key with one until `operator fill-abandoned-nonces` (the owner's call, not the runner's: the key may have leaked)
used each by a 0-value transfer to itself, priced over the stored attempts at that nonce
(`abandoned_nonce_service.py`). The freeze alert tells
the owner to remove the key's Allowance Module delegate at once (`treasury setup-transactions` lists its
`removeDelegate`): the module's `executeAllowanceTransfer` checks only the delegate and its allowance, never `to`, so
a leaked delegate can still pull up to a day's allowance from the Safe to any address until it is removed. The
runner checks the same with the `pending` count right before it takes a nonce, and signs nothing with a key that has
no state row yet. A key seen for the first time (its state row made) is adopted: its nonce row is made or moved up to
the highest `latest` count any endpoint answers (a lagging one's lower count would later read as a leak), never down (main's allocator before 2026-10-13 absorbed by-hand sends, so an older row can be
behind). A `retired` key is never frozen or alerted (the owner may empty it by hand); the runner adopts its `pending`
count for its returns. `operator unfreeze-key --reason` (the owner's own use) moves the row up to the `pending`
count and makes it active again.

Process boundary (2026-10-13): only the watcher and the operator CLI (run in the watcher container) read the keys.
The internet-facing API holds none, not even their addresses: a sweep it asks for names the zero address and the
runner picks a sweeper when it signs, and a refund is always signed by the refund key, whose address is written on
it then. The compose files blank every `PAY_OPERATOR_PRIVATE_KEY_*` and `PAY_SWEEPER_PRIVATE_KEYS_*` variable for the
`api` and `migrate` services (`environment` wins over `env_file`), and the API refuses to start with one set.
`deploy/deploy.sh` refuses, before it changes anything, an env file naming such a variable that list lacks (names
only, never values).

Treasury (`[chains.<key>.treasury]` in chains.toml, `PAY_TREASURY_SAFE_ADDRESS_<CHAIN>` per deployment): a Safe owned
by the owner tops the keys up through Safe's Allowance Module, so the server never holds more than the keys' daily
allowances can pull. Each key is the module's delegate with a daily allowance per asset of its role (`resetTimeMin`
1440); a key pulls for itself (`executeAllowanceTransfer` with itself as `to` and `delegate`, an empty signature, no
relayer payment), so no other key or address can be paid from the Safe. A key too short of gas for its own top-up
(a new sweeper starts empty, 2026-10-14) has it sent by the active key with the most native coin left that look,
carrying the needy key's delegate signature over the module's transfer hash at its allowance nonce: still only to
that key, never with a payment; a replacement resends the first attempt's call, never a new signature, since the
signature does not bind its sender. Once a minute, per active key and per
top-up rule of its role whose asset it holds below `low_amount` with no top-up of it active (none failed in the last
30 minutes unless its sender was at fault, none final in the last 5 in a block after the one the balances were read
at, since a balance read from a lagging endpoint after a failover may not show it): a `top_up` operation of
min(`target_amount` - balance, what the allowance has left after its daily reset, the Safe's balance), at most one
active per chain, key it fills (`recipient_address`) and asset (a unique index, 0022). A sender at fault (frozen,
retired, out of gas) sends no top-up for 30 minutes. The reads are one `aggregate3`:
every key's balance in each top-up asset and its gas, the Safe's balances, every key's `getTokenAllowance`. Alerts:
`allowance_exhausted` (nothing left or no allowance), `treasury_low` (the Safe holds less than one day of its active
keys' allowances), `top_up_gas_short` (neither the key nor another active key can pay a top-up's gas: the owner funds
one by hand), `top_up_failed`, `operation_stuck` (a key operation unresolved for 30 minutes); both name a relayed
top-up's sender too.
`operator retire-key`: no new work; once its last operation ends the watcher returns the key's top-up tokens, then
its native coin less 60,000 gas at the fee cap, to the configured Safe only (`treasury_return`; the runner refuses any
other recipient). A return that fails waits 30 minutes for that key and alerts `treasury_return_failed`. `treasury_return`, `top_up` and `treasury_safe_creation` operations have no invoice.

Treasury CLI: `treasury create-safe --owner-address` (never an operator key's address) records a `treasury_safe_creation` the watcher sends from the
refund key: a SafeL2 proxy from SafeProxyFactory `createProxyWithNonce` with owners [owner], threshold 1 and the
CompatibilityFallbackHandler, its `saltNonce` keccak("pay-7331 treasury", chain id, owner), so the same owner gets the
same address every run. `treasury setup-transactions` prints a Safe Transaction Builder batch (version 1.0, with the
builder's `meta.checksum`) of what the Safe still lacks: `enableModule`, `addDelegate` per active key, `setAllowance`
per key and asset, `removeDelegate` (allowances too) for frozen, retired and no longer configured keys; the owner
loads it in the Safe app. `treasury execute-setup --owner-key-environment-variable NAME` (never an operator key's
variable or address) sends the same calls as one `execTransaction`
delegatecall to MultiSendCallOnly with the owner's pre-validated signature (`v` 1, sent by the owner): fork e2e and
test Safes only. `treasury status` and `operator list-keys` show owners, module, delegates, balances and allowances;
`reconcile` prints the key report too.

Addresses (Polygon 137, chains.toml): Safe v1.4.1 canonical SafeL2 `0x29fcB43b46531BcA003ddC8FCB67FFE91900C762`,
SafeProxyFactory `0x4e1DCf7AD4e460CfD30791CCC4F9c8a4f820ec67`, CompatibilityFallbackHandler
`0xfd0732Dc9E303f09fCEf3a7388Ad10A83459Ec99`, MultiSendCallOnly `0x9641d764fc13c8B624c04430C7356C1C7C8102e2`
(https://github.com/safe-global/safe-deployments, `src/assets/v1.4.1`); Allowance Module v0.1.1
`0xAA46724893dedD72658219405185Fb0Fc91e091C` (https://github.com/safe-global/safe-modules-deployments,
`src/assets/allowance-module/v0.1.1`; 137 only, so Amoy has no treasury). Behaviour read from Safe v1.4.1 `Safe.sol`,
`SafeProxyFactory.sol`, `MultiSendCallOnly.sol` (https://github.com/safe-global/safe-smart-account/tree/v1.4.1), the
module's `AllowanceModule.sol` at tag `allowance/v0.1.1`
(https://github.com/safe-global/safe-modules/blob/allowance/v0.1.1/modules/allowances/contracts/AllowanceModule.sol) and the
Transaction Builder's `src/lib/checksum.ts` (https://github.com/safe-global/safe-react-apps, `apps/tx-builder`),
checked 2026-10-13.

## 6. Refunds (only to the sender, never anywhere else)

A refund goes only to an address that sent money to the invoice, capped at what that address sent, so the gateway
can never be used to pay in from one address and out to another (mixing). Refunds are paid from the refund key's float
(section 5.1),
not from the deposit (the deposit can only go to the destination, where the swept money ends up anyway). The float is
kept small, topped up from the treasury Safe between its low mark and its target (on Polygon 20 to 25 POL and 8 to 10
of each stablecoin, 2026-10-14), and its low mark must cover a minimum refund (`PAY_MINIMUM_REFUND_USD`) in each coin;
a refund the float cannot cover waits as `waiting_for_float` and a
`refund.waiting_for_float` event is recorded.

- Only operator-destination invoices get refunds (2026-10-09): an invoice made by one of the operator's own accounts
  (`PAY_OPERATOR_ACCOUNT_IDS`, comma-separated ids) whose `destination_address` equals `PAY_DEFAULT_DESTINATION_ADDRESS`
  (the operator's own payout address; compared checksummed). Both must hold: payout addresses are not verified, so
  anyone can make an invoice paying the operator's address (signed out or from another account), and on it each overpay
  would make the float pay a refund's gas. With `PAY_OPERATOR_ACCOUNT_IDS` empty no invoice gets float refunds. On any
  other invoice the swept money is the receiver's, and a float refund on top would be free money from the float, so: no
  automatic overpay refund (the watcher's pass returns early), no refund offer (`refund_offer.reason` is
  `receiver_settles`), `refund-offer`, `refund-requests` and the account's `POST /invoices/{id}/refunds` are refused
  with `refund_not_allowed`. The receiver settles with the payer. The one gate is `InvoiceService.allows_float_refunds`
  (`pays_the_operator` in presenters.py), checked where a refund is offered, recorded and paid automatically.

- Allocation: final payments in chain order (block, transaction index, log index) fill the amount due first; whatever
  comes after belongs to its sender. Each sender can get back at most their own unrefunded excess. An unknown-sender
  contribution is never refunded automatically.
- Overpaid, automatic: once the payments are final, each
  plain-account sender's excess of at least `minimum_refund_usd` goes back to it with no request, recorded by the
  watcher in the same tick that credits it. A sender that had any refund on the invoice gets no automatic second one
  (a reverted or owner-unpaid refund is asked again with a signature), so a refund that keeps failing cannot loop on
  gas. An exchange hot wallet sender may not pass it on; that was accepted over asking first. Contract and
  unknown senders are settled by the owner.
- Signed request: offered for an `overpaid` excess the automatic refund did not pay, and for the whole of an
  `underpaid` invoice (each sender their own part) once its payment window (`expires_at`) has passed (a
  refund closes the invoice, so earlier anyone could drain the float or close a payer's open invoice). Only when the amount is at least `minimum_refund_usd` and
  the sender is a plain account (no code; contract wallets such as Safes are settled by the owner by hand, since they
  cannot `personal_sign`).
- Payer path: `POST /public/invoices/{id}/refund-offer` returns the exact text to sign and creates one offer (nonce,
  amount, recipient, expiry 15 min) per sender; a sender's live offer with 5+ minutes left and the same amount is
  handed back instead, and a live offer is never deleted (2026-10-09: replacing it let anyone with the invoice id void
  the nonce a payer was signing; two live offers cannot pay twice, the refund rechecks the cap under the invoice
  lock). The browser wallet signs it with `personal_sign` (EIP-191 version 0x45,
  https://eips.ethereum.org/EIPS/eip-191). The text names the gateway origin,
  chain id, deposit address, token address, amount in base units and as a decimal, recipient, nonce and expiry. The
  API recovers the signer with eth-account and, in one transaction, consumes the nonce (`UPDATE ... WHERE consumed_at
  IS NULL`), rechecks the refundable amount and creates the refund operation. A second request with the same nonce
  finds it consumed. The signature proves the payer holds the key of the sending address, which rules out an exchange
  hot wallet the payer does not control.
- Account path: `POST /api/v1/invoices/{id}/refunds` with a sender and an amount, under the same caps and only on an
  operator-destination invoice; the recipient is always a recorded sender, never a free field.
- Stray coin (2026-10-11, after INC-001): money in a coin the invoice did not ask for goes back by itself once
  final, under the overpay excess's rules: operator-destination invoices only, only to its plain-account sender, at
  most what it sent in that coin less refunds that moved or may move, at least the minimum refund, and not while that
  sender has a refund in that coin not yet final. Contract and unknown senders are settled by the owner; the invoice's
  status does not change. There is no manual stray refund.
- No price is no verdict (2026-10-12, after a stray refund silently never fired): when an automatic refund (overpay
  excess or stray coin) passes every other rule but no feed answers, it is neither sent nor taken as below the
  minimum. The invoice is marked `automatic_refund_deferred_since` with a WARNING log, and the watcher decides it again
  every tick (section 5, step 7), closed or not, until a price is readable; the amounts subtract every refund that
  moved or may move, so it is never paid twice.
- The operator pays the gas, so the payer gets the full amount back.
- An unproven end (the operator's nonce mined, no receipt of ours on any endpoint) is `needs_owner`: never retried,
  the amount stays reserved, `refund.needs_owner` is recorded. The operator settles it from the CLI (2026-10-09):
  `refunds list-needing-owner` shows the nonce and every signed attempt; `refunds mark-paid <id> --transaction-hash`
  makes it `final` (and records `invoice.refunded`) once the chain shows that finalized transaction moved exactly the
  reserved amount to the recipient, from any sender; `refunds mark-unpaid <id> --nonce-taken-by <hash>` makes it
  `failed`, which offers the amount again, only once the chain shows that transaction was sent by the operator, has
  exactly the refund's nonce, is none of its signed attempts, moved nothing to the recipient, and sits at or below
  the finalized block. Every attempt shares that nonce, so none of them can ever mine. "No endpoint shows a receipt"
  alone is no proof: Bor and geth index transactions for the last 2,350,000 blocks only (`--history.transactions`)
  and Bor answers a receipt outside that index with null, so an old paid refund looks unpaid (2026-10-09). It is
  still refused while the nonce is unused or any endpoint shows a receipt for a signed attempt. A `waiting_for_float`
  refund (which may never be sendable: a blacklisted token recipient, a reverting account) is listed and settled the
  same way; one that never signed an attempt used no nonce, so `mark-unpaid` frees it without (and refuses) the flag
  (2026-10-09).

## 7. HTTP API (`/api/v1`)

Account routes need the account session cookie a passkey sets. The one exception (2026-10-12, replacing "no API keys"
of 2026-10-09): `POST /invoices`, `GET /invoices/{id}`, `GET /invoices` and `GET /invoices/export.csv` (2026-10-21)
also take an account API token as `Authorization: Bearer pay_tok_...` (section 8.3), and since 2026-10-21 so do `POST
/invoices/{id}/cancel`, `POST /invoices/{id}/accept-partial`, `POST` and `GET /payment-links`, `GET
/payment-links/{id}`, `POST /payment-links/{id}/close`, the invoice schedule routes (section 18) and `GET /events`
(the events the webhook sends); on every other route an `Authorization` header counts for nothing. Whatever changes where money or notifications go (the
payout address, the webhooks, the API tokens, the Monero wallet) and the float refund route stay session only, the
first behind a fresh passkey check.
The cookie is `__Host-pay_account_session`, HttpOnly, Secure,
SameSite=Strict, 30 days, only its SHA-256 stored. A request with any method but GET, HEAD or OPTIONS that carries the
cookie must also carry `Origin` equal to `PAY_PUBLIC_BASE_URL`'s origin, else 403 `origin_mismatch`. Errors are
`{"code", "message"}`. Public ids are random (a type prefix + 22 base58 characters), never sequential: `inv_`
invoices, `lnk_` links, `sda_` customer addresses, `sch_` schedules, and since migration 0036 `evt_` account events,
`pk_` passkeys, `tok_` API tokens and `whd_` webhook deliveries. Their integer keys stay internal (ordering, joins)
and are not sent, except as the `webhook-id` of a delivery queued before migration 0036.

Accounts (2026-10-09): a username (3-20 of `[a-z0-9_]`, stored lowercased, unique), a checksummed payout address
(not verified) and one or more passkeys. The username is the payout address's alias for its owner and never shows on
payer pages. Sign-up is open to anyone.

- Passkeys (WebAuthn, py_webauthn; RP id the host of `PAY_PUBLIC_BASE_URL`, discoverable credentials, user verification
  required, WebAuthn `user.name` the username, `user.id` a random 32-byte handle): `POST /accounts/registration-options`
  (`username`, `payout_address`; 409 `username_taken`) then `POST /accounts` (`label`, `credential`) creates the account
  (both screened, section 9: 403 `sign_up_refused`, 503 `sign_up_screening_unavailable`)
  and its first passkey and sets the cookie. Signed in, adding and deleting a passkey each take a fresh passkey check
  (as the payout address change below), so a stolen session can neither add the thief's passkey nor delete the
  owner's: `POST /account-passkeys/addition-options` (`{}`; request options naming the account's passkeys) then
  `POST /account-passkeys/registration-options` (`{"credential"}`, that assertion; the creation options) and
  `POST /account-passkeys` (`label`, `credential`) add one more and record `account.passkey_added`;
  `POST /account-passkeys/{id}/deletion-options` (`{}`; a check bound to that passkey) then
  `POST /account-passkeys/{id}/deletion` (`{"credential"}`; 204) deletes it, its sessions end with it, and records
  `account.passkey_deleted` (the last passkey is refused, 409 `last_passkey`). `GET /account-passkeys` lists them.
  Challenges are Postgres rows (`passkey_challenges`), 10 minutes, deleted by the verify that uses them; a sign-up's
  username and payout address wait on its challenge row, so nothing is made until the passkey is verified. The
  browser's prompt timeout is 300,000 ms and challenges live like the range's upper limit (W3C WebAuthn Level 3,
  sections 15.1 and 13.4.3, https://www.w3.org/TR/webauthn-3/; 2026-10-13). Beyond py_webauthn's checks: a
  `crossOrigin: true` ceremony (another site's iframe) is refused, a new credential id over 1023 bytes is refused
  (section 7.1), and a discoverable sign-in's `userHandle` must be present and be its account's (section 7.2).
- Passkey enrollment links, the account recovery path (2026-10-15): the operator runs `python -m app.cli
  mint-passkey-enrollment-link <username> [--expires-minutes 15]`, which prints `<origin>/enroll-passkey#<token>`
  (the token in the fragment, never sent to the server; only its SHA-256 stored; a new link deletes the account's
  unused ones). The page posts the token to `POST /passkey-enrollment-links/registration-options` (`{"token"}`; the
  "add a passkey" options of the link's account) and the new credential to `POST /passkey-enrollment-links/passkeys`
  (`token`, `label`, `credential`), which adds the passkey, marks the link used, records
  `account.passkey_enrolled_by_link` and sets the cookie. Public, rate limited as public writes; 404
  `passkey_enrollment_link_unknown`, 410 `passkey_enrollment_link_used` or `passkey_enrollment_link_expired`.
- Sessions: `POST /sessions/authentication-options` and `POST /sessions` (`credential`, a discoverable credential;
  sets the cookie); `GET /sessions/current` and `DELETE /sessions/current` (signs out, clears the cookie).
- Settings (2026-10-11): `PATCH /accounts/current` (`accepted_assets_by_chain`, `{chain key: [symbols]}`, 1-16
  symbols each, matched case-insensitively; chains left out keep theirs; unknown chain 400 `chain_not_enabled`,
  unknown symbol 400 `asset_not_supported`) saves the coins the account's invoices start with ticked, per chain, and
  returns the session view. Making a fiat invoice signed in (`POST /invoices`, `POST /public/invoices`) saves that
  invoice's coins the same way; there is no separate "save as default". `support_contact` (2026-10-11): an email
  address, a Telegram handle (`@name`, 5-32 of `[A-Za-z0-9_]`) or an https link, at most 200 characters; "" or null
  clears it; anything else 400. The payout address is not here.
- Payout address change (2026-10-11), behind a fresh passkey check so a stolen session cannot redirect payouts:
  `POST /accounts/current/payout-address-change-options` (`{"payout_address"}`; checked as at sign-up, 400 if it is
  already the payout address) returns request options naming only the account's passkeys, user verification
  required, with a single-use challenge bound to this account and this address (Postgres, 10 minutes); `PUT
  /accounts/current/payout-address` (`{"payout_address", "credential"}`) verifies the assertion against that challenge
  and a passkey of this account, changes the address, records `account.payout_address_changed` and returns the
  session view. Invoices already made keep their destination. `PATCH /account-passkeys/{id}` (`{"label"}`) renames a
  passkey.
- API tokens and the webhook (2026-10-12, section 8.3): `POST /account-api-tokens/creation-options` (`{}`) then
  `POST /account-api-tokens` (`name`, `credential`; 201 with `token`, shown once), `GET /account-api-tokens`,
  `DELETE /account-api-tokens/{id}` (204); `GET`, `PUT` (`url`, `credential`) and `DELETE /account-webhook`,
  `POST /account-webhook/change-options` (`{}`), `POST /account-webhook/secret-rotation` (`credential`),
  `POST /account-webhook/test-events` (202; `{"event_type"}`, optional, section 8.3), `POST
  /account-webhook/deliveries/{id}/resend` (202). Session only.
  `GET` and `PUT` answer `{"url", "secret", "disabled_at", "deliveries"}`; `disabled_at` is set once the receiver
  answered 410 Gone, and then the test and resend routes are 409 `webhook_disabled` until a `PUT` sets the URL again
  (2026-10-13).
- The Discord webhook (2026-10-13, section 8.4): `GET`, `PUT` (`url`, `credential`) and `DELETE
  /account-discord-webhook`, `POST /account-discord-webhook/change-options` (`{}`), `POST
  /account-discord-webhook/test-messages` (202; 409 `discord_webhook_not_set` with none, or one Discord deleted).
  `GET` and `PUT` answer `{"discord_webhook_id", "discord_thread_id", "status": "active" | "deleted_in_discord",
  "deleted_in_discord_at", "latest_message"}` (all null with none; `latest_message` a delivery as in the webhook's
  list), never the URL.
  Session only.
- The Monero wallet (2026-10-14, section 14): `GET /account-monero-wallet?chain_key=` (left out: the first enabled
  Monero chain), `POST /account-monero-wallet/change-options` (`{}`), `PUT /account-monero-wallet` (`chain_key`,
  `primary_address`, `private_view_key`, `credential`; behind a fresh passkey check like the webhook, the sidecar's
  address and view key checks, then `account.monero_wallet_set`), `POST /account-monero-wallet/lookahead-confirmations`
  (`chain_key`; session only, no passkey: the receiver ran `set subaddress-lookahead 50:1000`, the wallet's
  `confirmed_lookahead_minor_count` becomes 1000). Session only; the routes live in the invoices domain, which reads
  `monero_payments` for the gap and sits above accounts, account events and monero wallets. Refusals:
  `monero_address_invalid`, `monero_view_key_mismatch`, `chain_kind_not_supported` (400), `chain_not_enabled` (400),
  `monero_wallet_unchanged`, `monero_address_taken`, `monero_wallet_change_conflict`, `monero_wallet_missing` (409),
  `monero_sidecar_unavailable` (503: no `PAY_MONERO_SIDECAR_URL` or no answer from it; nothing is saved unchecked).
- The two sign-up routes, the two sign-in routes, the three add-passkey routes, the two delete-passkey routes,
  `POST /invoices` and every POST or PUT under `/account-api-tokens`, `/account-webhook` and
  `/account-discord-webhook`, and the Monero wallet's change
  options and `PUT` count as public writes (section 9).

Account (the session's own invoices only; another account's id is 404):
- `POST /invoices`: `amount`, `currency` (`USD`, `EUR` or an asset symbol), `chain` (`polygon`), `assets` (one or more
  of the chain's symbols from `GET /chains`, at most 16, matched case-insensitively; one means no picker; with no
  network field every enabled EVM chain is offered; with no coin field a fiat invoice takes the account's saved coins
  on each chain and a coin amount its own coin, so `amount` and `currency` alone make an invoice), `destination_address` (defaults to the account's payout address),
  `note` (shown to the payer, 500 chars), `order_id` (optional; reusing one of the account's returns that invoice, as
  Cryptomus), `refresh_expired` (with `order_id`: an expired, unpaid invoice of that order is replaced by a new one,
  section 4; 2026-10-21), `lifetime_seconds`, `accuracy_percent`, `underpaid_open_until`, `return_url` (back to the
  shop, Cryptomus `url_return`: the payer page's back button before paying and, with no `success_url`, its link after
  paying), `success_url` (the link once paid, Cryptomus `url_success`; 2026-10-21), `callback_url` (this invoice's
  events, section 8.3; 2026-10-21), `metadata` (private JSON).
  Returns the Invoice. By an API token (section 8.3) the per-IP cap is skipped for the
  per-account ones, and the coins are not saved as the account's default.
- `GET /invoices/{id}`, `GET /invoices?status=&cursor=&order_id=&order_id_prefix=` (`order_id` gives 0 or 1 invoice;
  `order_id_prefix`, 1-128 chars matched literally, pages the invoices whose `order_id` starts with it, over
  `ix_invoices_account_id_order_id_pattern`; both at once is 400 `validation_error`; 2026-10-15), `POST /invoices/{id}/cancel`,
  `POST /invoices/{id}/accept-partial`, `POST /invoices/{id}/refunds` (section 6; session only: it pays from the
  float).
- `GET /invoices/export.csv?status=&created_from=&created_before=` (2026-10-21): the account's invoices and their money
  movements as CSV for accounting (section 7.1), by session or API token, oldest first, streamed: 200 invoices a batch,
  each batch on its own short transaction (the request's session commits before the body is sent), so a large
  account is never in memory or one long transaction. `created_from` is inclusive, `created_before` exclusive (a time
  without a zone is UTC; `created_before` not after `created_from` is 400 `validation_error`). One row per invoice,
  then one per payment, wrong-coin payment, sweep and refund, as BTCPay's invoice report does ("An invoice with several
  payments can produce several rows", https://docs.btcpayserver.org/Users/invoices/). Its own rate limit (section 9).
- `GET /events/stream`: the account's event stream, Server-Sent Events (section 8.1).
- `GET /events?cursor=&limit=` (2026-10-13): the account's events, newest first, `limit` 1-200 (default 50), as
  `{"events": [...], "next_cursor"}` (each event as the stream sends it); `next_cursor` (the page's last event id,
  `evt_...`, since migration 0036) reads the older page and is null once there is none. Keyset by the row id that event
  id maps to within the account, so new rows never shift a page; a malformed cursor, or the id of an event the account
  does not have, is 400 `validation_error` (the older base64 cursors are still read). By an API token (2026-10-21) only the events the webhook sends (section 8.3):
  never `operator.alert` or an `account.*` event, which name the operator's problems or the account's addresses.
- Quantity links (2026-10-11; an open invoice holds its slot until it is paid or expires, and slots are counted under
  a row lock on the link, so a link never sells more than its quantity):
  `POST /payment-links` (`amount`, `currency` USD or EUR, `chain`, `assets`, `destination_address` (defaults to the
  payout address, fixed on the link), `allow_contract_destination`, `note`, `quantity` 1-1000, `lifetime_seconds`,
  `accuracy_percent`, `return_url`; each payer's invoice gets these), `GET /payment-links` (newest 100),
  `GET /payment-links/{id}` (with its invoices, newest 200), `POST /payment-links/{id}/close` (no new payers).
  `POST /payment-links` counts as a public write. Each also by an API token (2026-10-21); a token's link saves no
  coins as the account's default.
- Invoice schedules (2026-10-21, section 18): `POST /invoice-schedules` (the `POST /invoices` fields but `order_id`,
  `refresh_expired` and `underpaid_open_until`, plus `interval` `daily`, `weekly` or `monthly`, `starts_at` (default
  now), `order_id_prefix` and at most one of `ends_after_invoice_count` (1-10000) and `ends_at`; 201),
  `GET /invoice-schedules` (newest 100), `GET /invoice-schedules/{id}`, `GET /invoice-schedules/{id}/invoices`
  (newest period first, at most 200) and `POST /invoice-schedules/{id}/pause`, `/resume` and `/cancel` (409
  `invoice_schedule_state_conflict` on an ended one). Session or API token; the create is a public write and counts
  in a token's create budget.

Public (rate-limited; never screened, only sign-up is, section 9):
- `POST /public/invoices`: `amount_usd` (at least 1.00, 2 decimals), `assets` (optional, the symbols the maker
  ticked, as in `POST /invoices`; left out, every asset of the chain; an unknown one is 400 `asset_not_supported`),
  `destination_address` (required signed out, 400 `destination_required`; signed in it defaults to the payout
  address), `note`. A one-off USD invoice on the first chain that takes invoices (`GET /chains` lists it first), 30 minutes, 1% accuracy, no `order_id`, `metadata` or `return_url`. Signed in, the invoice
  belongs to the account. Returns the public view; 429 `too_many_open_invoices` past the per-IP cap.
- `GET /public/invoices/{id}`: the payer's view (no metadata).
- `GET /public/invoices/{id}/qr.svg?kind=payment|address`.
- `POST /public/invoices/{id}/asset`: pick the asset while `awaiting_asset`; starts the quote and the lifetime.
- `POST /public/invoices/{id}/quote`: a fresh quote for the remainder of an underpaid fiat invoice whose quote lapsed.
- `POST /public/invoices/{id}/refund-offer` (returns the message to sign) and
  `POST /public/invoices/{id}/refund-requests` (the signature); operator-destination invoices only (section 6).
- `POST /public/invoices/{id}/payment-checks`: the payer's "I paid" (2026-10-12). No hash: the deposit is
  reread at once in every configured asset, at `latest` and finalized. A `transaction_hash`: every RPC endpoint is
  asked for it, then its receipt and block, and the answer says what the chain shows of it for this deposit. It never
  credits an invoice; a final transfer in another listed coin into this deposit is recorded as stray money exactly as
  `record-stray-payment-from-transaction` does, and the refund rules follow. A public write, so rate limited per IP.
- `GET /public/payment-links/{id}`: a quantity link as the payer sees it. `POST /public/payment-links/{id}/invoices`
  (`asset`) takes a slot: under a row lock on the link it counts the slots held and makes the payer's invoice in that
  coin (capped like any creation), else 409 `sold_out`; 409 `payment_link_closed` once closed.
- `GET /chains`: the chains a new invoice can go to (a deposit factory deployed, in `PAY_ENABLED_CHAINS` order, so the
  first is the one `/`'s public invoices use), their assets, decimals, explorer base URL, so pages derive nothing by hand. Each asset's logo is
  `/static/coin_logos/<symbol lowercased>.png` (`usdc.e.png`), committed under `frontend/public/coin_logos/` by the
  CLI's `download-coin-logos`, run by hand from CoinGecko's keyless token list (2026-10-11); nothing fetches logos at
  runtime.
- `GET /rates` (2026-10-21): every coin of every chain in `GET /chains` at the price a quote would use now (the same
  Chainlink read and 30-second Redis copy, so the stablecoin peg rule holds), `Cache-Control: public, max-age=10`, so
  a rate is at most about 40 s old;
  rate limited as every read.

The Invoice resource: `id`, `url`, `status`, `partial`, `late_payment`, `sweep_blocked`, `chain`, `asset`, `assets`, `deposit_address`,
`destination_address`, `amount_due` and `amount_received` (asset units), `fiat` (`currency`, `amount`, plus `usd` and
`eur` equivalents), `note`, `order_id`, `expires_at`, `underpaid_open_until`, `payments` (tx hash, sender, amount,
final), `sweeps` and `refunds` (tx hash, asset, amount, status), `refund_offer` (amount, recipient, or null with a reason),
`stray_payments` (2026-10-11),
`created_at`, `updated_at`. The public view drops `metadata` and `order_id`. `refund_offer` is always an object
(section 7.1).

### 7.1 Wire shapes (the frontend and backend both build against these)

Times are ISO 8601 UTC strings; asset and fiat amounts are decimal strings. `GET /api/v1/public/invoices/{id}`:

```json
{
  "id": "inv_7Qm2...", "url": "https://pay.7331.org/i/inv_7Qm2...",
  "status": "underpaid", "partial": false, "late_payment": false, "sweep_blocked": false,
  "chain": {"key": "polygon", "name": "Polygon", "chain_id": 137, "explorer_url": "https://polygonscan.com"},
  "asset": {"symbol": "POL", "name": "POL", "decimals": 18, "token_address": null},
  "assets": ["POL", "USDC", "USDT"],
  "deposit_address": "0x1234...",
  "amount_due": "201.946", "amount_received": "100", "amount_remaining": "101.946",
  "fiat": {"currency": "USD", "amount": "20.00", "usd": "20.00", "eur": "17.84"},
  "quote": {"asset_price_usd": "0.099023", "expires_at": "2026-10-09T12:30:00Z"},
  "note": "Logo design, invoice 14", "return_url": "https://example.org/cart", "success_url": "https://example.org/thanks",
  "expires_at": "2026-10-09T12:30:00Z", "underpaid_open_until": null,
  "payment_uri": "ethereum:0x1234...@137?value=1.01946e20",
  "payments": [{"tx_hash": "0xab...", "sender": "0x99...", "amount": "100", "block_number": 95198175, "final": true}],
  "sweeps": [{"tx_hash": "0xcd...", "asset_symbol": "POL", "amount": "100", "status": "final"}],
  "refunds": [{"tx_hash": null, "recipient": "0x99...", "asset_symbol": "POL", "amount": "12.5", "status": "pending"}],
  "refund_offer": {"available": false, "reason": "not_overpaid", "senders": []},
  "stray_payments": [],
  "created_at": "2026-10-09T12:00:00Z", "updated_at": "2026-10-09T12:05:00Z"
}
```

- `asset`, `deposit_address`, `quote`, `payment_uri` and `amount_*` are null while `awaiting_asset`;
  `amount_remaining` is "0" once paid. `payment_uri` asks for `amount_remaining`.
- `payment_options` is filled only while `awaiting_asset` on a fiat invoice: each offered coin on each chain with what
  picking it would ask right now (the quote's own rounding at the live feed price), for the coin picker; not a quote.
  `asset_estimates`, the first chain's copy of the same, was dropped 2026-10-22: no client read it.
- `fiat` is always an object: for a crypto-denominated invoice `currency` and `amount` are null and `usd` and `eur`
  are the live equivalents of `amount_due` (feed price, cached about 30 s); null only when no feed answers.
- `refund_offer.senders` is `[{"sender": "0x99...", "amount": "12.5"}]` when available; `reason` is one of
  `not_overpaid`, `receiver_settles` (not an operator-destination invoice, section 6), `payment_window_open`,
  `below_minimum`, `price_unavailable`, `unknown_sender`, `contract_sender`, `refund_in_progress`,
  `already_refunded`. An `overpaid` invoice whose automatic refund waits for a price says `refund_in_progress`
  (section 6, 2026-10-12); otherwise a refundable sender with no USD price to check the minimum against says
  `price_unavailable` (try again shortly), never `below_minimum` (2026-10-13).
- `sweeps[].asset_symbol` and `amount` are null while a sweep is under way or failed (it moves whatever the deposit
  holds); every final sweep is listed, in every coin (2026-10-11). `refunds[].asset_symbol` is the invoice's coin or
  a stray coin sent back. The public view lists outcomes only: a `failed` refund is left out once a later one to the
  same recipient in the same coin is `final` or under way; the account view lists every attempt (2026-10-14).
- `stray_payments` (INC-001, 2026-10-11): `[{"asset_symbol": "POL", "amount": "50.25", "sender": "0x32...",
  "tx_hash": "0x89...", "block_number": 95198175, "final": true, "swept": false, "refund_handling": "automatic"}]`,
  money in a coin the invoice did not ask for; `sender` and `tx_hash` are null when no transaction explains it.
  `refund_handling` is `automatic` (back by itself; its refund is in `refunds`), `receiver_settles`, `owner_settles`
  (a contract or unknown sender) or `below_minimum`. Without a price it is `automatic` unless the watcher already
  decided below the minimum (2026-10-12).
- The account view (`/api/v1/invoices/{id}`) adds `destination_address`, `order_id`, `metadata`, `accuracy_percent`,
  `payment_link_id` (the quantity link a payer made it from, else null), `paid_at` (when it became paid or overpaid, or
  was accepted as partially paid; null before, 2026-10-15), `callback_url`, `refreshed_from_invoice_id` and
  `refreshed_into_invoice_id` (null unless a refresh links it to another invoice of its order; 2026-10-21),
  `invoice_schedule_id` and `invoice_schedule_period_start` (the schedule that made it and the period it bills, else
  null; 2026-10-21, section 18).
- `success_url` (2026-10-21) is null unless the invoice was made with one; the payer page links it once paid, else
  `return_url`, and shows `return_url` as its back button only while nothing was paid, seen or sent from the page.
- Monero invoices (section 14, 2026-10-14): the public view gains `seen` (a pool transaction pays the subaddress and
  is not in a block yet; false on every EVM invoice), `confirmations` and `confirmations_final` (the newest credited
  output's count at the tip the watcher last read, and the count at which money moves; null on every EVM invoice and
  while nothing is credited), and the account view gains `monero: {"confirmations": 4, "confirmations_final": 10}` (on every Monero invoice, `confirmations` 0 until an output is credited; null on every
  EVM invoice). Credited outputs show in `payments` with `sender` null (ring signatures hide it). `deposit_address`
  holds the subaddress and `destination_address` the receiver's primary address (both columns are 106 characters
  wide); the `destination_address` a request sends for an XMR invoice or link is ignored.
- The Monero wallet (2026-10-14): `GET`, `PUT /account-monero-wallet` and `POST
  /account-monero-wallet/lookahead-confirmations` answer `{"monero_chains": [{"key": "monero_stagenet", "name":
  "Monero (stagenet)", "network": "stagenet"}], "wallet": null | {"chain_key", "primary_address", "network",
  "confirmed_lookahead_minor_count", "unreceived_minor_gap",
  "lookahead_warning", "created_at"}}`, never the view key. `monero_chains` lists every enabled Monero chain (empty:
  `/settings` shows no Monero row; the page asks only once `/chains` lists a Monero chain). `unreceived_minor_gap` counts the allocated minors past the highest one with an
  output in a block; `lookahead_warning` is true from the confirmed lookahead less 50 (150 of 200, 950 of 1000). `POST
  /account-monero-wallet/change-options` takes `{"chain_key", "primary_address"}` and binds its passkey challenge to
  SHA-256 of `chain_key|primary_address` (400 `chain_not_enabled` and no challenge with no Monero chain enabled); `PUT`
  takes `{"chain_key", "primary_address", "private_view_key" (64 hex), "credential"}` and refuses an assertion made for
  another wallet; the lookahead confirmation `{"chain_key"}`.
- A quantity link (`GET /public/payment-links/{id}`): `{"id": "lnk_...", "url": ".../l/lnk_...", "status": "open",
  "quantity": 10, "slots_left": 7, "chain": {...}, "assets": ["POL", "USDC"], "payment_options": [...], "fiat":
  {"currency": "USD", "amount": "5.00", "usd": "5.00", "eur": "4.46"}, "note": "...", "created_at": "..."}`; `status`
  is `open`, `all_taken` (no slot free, some held by unpaid invoices that may expire), `sold_out` (every slot paid)
  or `closed`. The account view adds `destination_address`, `lifetime_seconds`, `accuracy_percent`, `return_url`,
  `slots_held`, `paid_count`, `closed_at`; `GET /payment-links/{id}` adds `invoices` (account views);
  `GET /payment-links` returns `{"payment_links": [...]}`. `POST /public/payment-links/{id}/invoices`
  `{"asset": "USDC"}` returns the public view of the payer's new invoice.
- An invoice schedule (2026-10-21, section 18): `{"id": "sch_...", "status": "active", "interval": "monthly",
  "starts_at", "ends_after_invoice_count", "ends_at", "next_period_start" (the next period still to come, never a past one; null
  once ended and while paused),
  "created_invoice_count", "last_invoice_created_at", "last_failure_code", "last_failure_at", "amount": "5",
  "currency": "USD", "chains", "assets", "assets_by_chain", "destination_address", "note", "metadata",
  "order_id_prefix", "lifetime_seconds", "accuracy_percent", "return_url", "success_url", "callback_url",
  "paused_at", "ended_at", "created_at", "updated_at"}`; `status` is `active`, `paused`, `cancelled` or
  `completed`. `GET /invoice-schedules` returns `{"invoice_schedules": [...]}` (at most 100, active and paused first,
  then newest), `GET .../invoices`
  `{"invoices": [...]}` (account views).
- `POST /public/invoices` `{"amount_usd": "5", "assets": ["USDC", "USDT", "POL"], "destination_address": "0x...",
  "note": "..."}` returns the public view; `assets` may be left out (every asset).
- `POST /public/invoices/{id}/asset` `{"asset": "USDC"}` and `POST /public/invoices/{id}/quote` (no body) return the
  invoice.
- `POST /public/invoices/{id}/refund-offer` (no body) returns
  `{"offers": [{"sender": "0x99...", "amount": "12.5", "asset": "POL", "nonce": "...", "message": "...",
  "expires_at": "..."}]}`; `POST /public/invoices/{id}/refund-requests` `{"nonce": "...", "signature": "0x..."}`
  returns the invoice.
- `POST /public/invoices/{id}/payment-checks` `{}` or `{"transaction_hash": "0x..."}` (any text up to 200 characters)
  returns `{"outcome": "...", "asset_symbol": "POL" | null, "amount": "30" | null, "invoice": {public view}}`.
  `outcome`: `payment_counted`, `payment_confirming` (the invoice's coin into this deposit, not final or not counted
  yet), `payment_after_close` (late, never counted), `wrong_coin_recorded`, `wrong_coin_confirming`,
  `unlisted_token`, `other_address` (nothing into this deposit; never says whether it paid another deposit),
  `transaction_failed` (receipt status 0), `transaction_not_mined`, `transaction_unknown` (no endpoint knows it),
  `hash_malformed` (not `0x` and 64 hex: a Tron, Solana or Bitcoin id), and for the reread `nothing_found`. 409
  `invoice_state_conflict` before a coin is picked, 503 `service_unavailable` when no endpoint answers.
- In account requests `chain` is the chain's key (`"polygon"`); in responses it is the chain object.
- `GET /api/v1/rates` returns `{"rates": [{"chain": "polygon", "asset": "USDC", "usd": "1", "eur": "0.86206896"}]}`:
  USD per whole coin as the feed answers (a pegged stablecoin's 1 within 0.5% of the peg, never above 1 off it) and
  EUR per whole coin (that over the chain's EUR/USD feed, 8 decimals); both null for every coin of a chain whose feeds
  are stale or unreadable, the other chains unaffected (2026-10-21).
- `GET /api/v1/invoices/export.csv` (2026-10-21): `text/csv; charset=utf-8`, an attachment, RFC 4180 with CRLF line
  ends. Columns: `row_kind` (`invoice`, `payment`, `stray_payment`, `sweep`, `refund`), `occurred_at` (an invoice's
  creation, a payment's block time, a sweep's or refund's recorded time), the invoice's `invoice_id`,
  `invoice_created_at`, `order_id`, `invoice_status`, `partial`, `late_payment`, `note`, `fiat_currency`,
  `fiat_amount`, `chain`, `asset`, `amount_due` (as quoted), `amount_received`, `destination_address`,
  `deposit_address`, `paid_at`, `payment_link_id` on every row, then `row_status` (`final`, `confirming`, a sweep's or
  refund's operation status), `row_asset`, `row_amount`, `asset_price_usd` (the quote live at the payment's block time;
  blank when none was live then), `transaction_hash`, `block_number`, `from_address`, `to_address`. A sweep under way or failed has no
  coin or amount, as in `sweeps`; a refund's `from_address` is blank (the float pays it). Amounts are exact decimal
  strings, times ISO 8601 UTC, booleans `true`/`false`. A note or order id that starts, as is or after leading whitespace,
  with `=`, `+`, `-`, `@`, a tab, CR, LF or a full-width `＝＋－＠` gets a leading `'`, and a `'` goes after any `;`
  (and its following whitespace) that one of `=+-@＝＋－＠` follows, so a spreadsheet splitting on `,` or `;` shows the
  text rather than running it (OWASP's triggers, https://owasp.org/www-community/attacks/CSV_Injection; OWASP warns
  Excel may still run one after a save and reopen). Each batch of 200 invoices is read in one REPEATABLE READ
  read-only transaction, so an invoice's rows agree with each other.
- `GET /api/v1/chains` returns `{"chains": [{"key", "name", "chain_id", "explorer_url", "assets": [{"symbol", "name",
  "decimals", "token_address"}], "default_accepted_assets": ["POL", "USDC", "USDT"]}]}`; `default_accepted_assets`
  are the coins a new invoice starts with ticked, signed out or before an account saves its own.
- Passkeys: both options routes return WebAuthn's JSON forms (`PublicKeyCredentialCreationOptionsJSON`,
  `PublicKeyCredentialRequestOptionsJSON`; base64url for bytes) for `PublicKeyCredential.parseCreationOptionsFromJSON` /
  `parseRequestOptionsFromJSON`. `POST /accounts/registration-options` takes `{"username": "alice", "payout_address":
  "0x..."}`; `credential` in `POST /accounts` and `POST /account-passkeys` `{"label": "Laptop", "credential": {...}}`
  and in `POST /sessions` `{"credential": {...}}` is the browser's `credential.toJSON()`. A passkey is `{"id",
  "label", "transports", "created_at", "last_used_at"}`; `GET /account-passkeys` returns `{"passkeys": [...]}`.
  `POST /accounts`, `POST /sessions`, `GET /sessions/current` and `PATCH /accounts/current` return `{"username",
  "payout_address", "expires_at", "passkey_label", "accepted_assets_by_chain": {"polygon": ["POL", "USDC", "USDT"]}}`
  (every enabled chain: the saved coins, else its `default_accepted_assets`; a Monero chain only for an account with
  an active Monero wallet on it, 2026-10-14); `GET /sessions/current` answers `null` signed out (no 401, which every page load
  would log as a console error). Refusals: `passkey_response_invalid`, `passkey_challenge_expired` (400), `passkey_unknown`,
  `account_session_expired`, `unauthorized` (401), `origin_mismatch` (403), `account_passkey_not_found` (404),
  `passkey_already_registered`, `username_taken`, `last_passkey` (409), `passkey_not_for_this_account` (403, the payout
  address change signed by another account's passkey). The session view also carries `support_contact` (string or
  null); a passkey also `used_by_current_session` (2026-10-11).
- The public view carries `seller_support_contact`: `{"kind": "email" | "telegram" | "url", "display_text",
  "href"}` (`mailto:...`, `https://t.me/name` or the https link) when the invoice's account set one, else null; never
  the username (2026-10-11).
- Errors: HTTP status plus `{"code": "invoice_not_found", "message": "No invoice with that id."}`.

Pages are one React app built by Vite into `frontend/dist/` (2026-10-11): the API answers `/`, `/i/{id}`, `/l/{id}`, `/new`,
`/activity` and `/settings` with `frontend/dist/index.html`, `/static/` maps to `frontend/dist/`, and the app routes on the client
and fetches the JSON above. Every page and every `/static/` file carries the same CSP (`frame-ancestors 'none'`),
`Referrer-Policy` and `nosniff`, so `/static/index.html` is no looser than `/` (2026-10-13).

## 8. Events

An account can set one signed webhook (2026-10-12, section 8.3, replacing "no webhooks" of 2026-10-09) and one
Discord channel webhook (2026-10-13, section 8.4). Every
invoice and refund change records an event in the same transaction: `invoice.payment_detected` (seen, not final), `invoice.underpaid`,
`invoice.paid`, `invoice.overpaid`, `invoice.expired`, `invoice.cancelled`, `invoice.swept`, `invoice.sweep_failed`,
`invoice.late_payment`, `invoice.stray_payment` (money in a coin the invoice did not ask for went final, 2026-10-11),
`invoice.refunded`, `refund.waiting_for_float`, `refund.needs_owner`, `schedule.invoice_created` (an invoice
schedule made its period's invoice; the invoice is its data, 2026-10-21, section 18), and on Monero (section 14, 2026-10-14)
`invoice.seen` (a pool transaction pays the subaddress, once per transaction) and `invoice.payment_held` (the second
node's cross-check disagreed; nothing credits). Five account events have
no invoice: `account.payout_address_changed` (`{"previous_payout_address", "payout_address"}`, 2026-10-11),
`account.monero_wallet_set` (`{"chain_key"}`, 2026-10-14; the wallet's row id dropped 2026-10-22),
`account.passkey_enrolled_by_link` (`{"account_passkey_id", "passkey_enrollment_link_id"}`, ids only, 2026-10-15),
`account.passkey_added` and `account.passkey_deleted` (`{"account_passkey_id"}`, ids only). The
watcher's `operator.alert` (`{"kind", "message", "problem_key", "chain_key", "invoice_id", "first_seen_at",
"is_reminder"}`, no invoice of its own; the last two since 2026-10-18) goes only to the operator's account, the first
`PAY_OPERATOR_ACCOUNT_IDS` names (pinned by id, since payout addresses are not verified and anyone could sign up with
the operator's; empty, no account gets them; the watcher logs at start when the list is empty and for each listed id no
account has; section 5 step 8); the host's backup alerts (`record-operator-alert`) have `chain_key` null (2026-10-12).

### 8.1 Account events (2026-10-09)

Every event above is an `account_events` row (sequential internal id, random public `evt_` id, invoice id, the
invoice's `account_id`, type, the account view of the Invoice resource). A signed-out invoice's events are recorded with no account and streamed to nobody.
`GET /api/v1/events/stream` (session cookie) is Server-Sent Events built on FastAPI's `EventSourceResponse`
(https://fastapi.tiangolo.com/tutorial/server-sent-events/): the account's latest 50 events (or, given
`Last-Event-ID`, those after it; one that is malformed or not one of the account's events counts as none), one
`caught_up` event (`{"latest_event_id"}`, null with no event), then each new row of the account as an `account_event`
event (`{"id", "type", "invoice_id", "created_at", "data"}`, SSE `id` = the event's public id, which the server maps
back to the row id to resume). A connection reads
only its backlog, on its own short transactions; new rows come from one shared poller per API process
(`account_events/stream_service.py`, 2026-10-13), which every 2 s, and only while a stream is open, re-checks every
open stream's session in one query (a sign-out or a deleted passkey ends the stream) and reads the new rows of every
subscribed account in another, re-reading the last 2 minutes so a row committed after a higher id is not missed, then
hands each row to its account's open streams through in-process queues. A connection subscribes before its backlog
read and skips ids it already sent, so the handoff has no gap and no duplicate. A stream whose queue fills (1,000
events, a page that stopped reading) is closed and its page reconnects with `Last-Event-ID`. A session holds at most 3
streams open at once, counted in Redis across every API worker (a sorted set of 60 s leases per session, renewed every
20 s and removed when the stream ends, `account_events/stream_slot_store.py`); a fourth answers 429
`too_many_event_streams` before the stream starts. Polling, not LISTEN/NOTIFY:
NOTIFY is lost while the listener is disconnected, so the same catch-up read is needed anyway, and a full notification
queue would fail the watcher's commits (https://www.postgresql.org/docs/current/sql-notify.html). The client dedupes
by id. FastAPI pings every 15 s; Caddy flushes `text/event-stream` at once
(https://caddyserver.com/docs/caddyfile/directives/reverse_proxy). The page reads the stream with `fetch` and sends
`Last-Event-ID` itself (https://developer.mozilla.org/en-US/docs/Web/API/EventSource/EventSource); the cookie goes
with it. Older history is `GET /api/v1/events` (section 7), paged back by row id (read in index order backwards, the
planner's choice of index); `/activity` loads its first page beside the stream, merges both by id, orders them by
`created_at`, and pages back on "Load older".

### 8.2 Alerts (2026-10-13)

Alerts reach people three ways, no push to a closed browser page (removed 2026-10-13: nobody installs the site as an
app). The account's `/activity` streams its events over SSE (section 8.1) and, once allowed on a tap, pops a browser
notification while the page is open; the operator's `operator.alert` events arrive there too. The tap is offered only
where the page itself can notify: Android Chrome and Samsung Internet throw on `new Notification()` and iOS allows it
only in a home-screen app (MDN browser-compat-data, `api.Notification.Notification`), so there `/activity` keeps its
toasts and offers nothing. An account's own server gets its invoice events as signed webhooks (section 8.3), and an
account's Discord channel gets them and, for the operator's account, its alerts (section 8.4).

An operator alert goes out once when its problem (`problem_key`) opens and, while it lasts, again every
`operator_alert_repeat_hours` of the operator account's Discord webhook ("Alert reminders" in its Discord sheet: off or
every 1 to 24 hours, the API takes 0 to 168; 1 by default); without a webhook it never repeats, since /activity keeps
the first (2026-10-18). A reminder has
`is_reminder` and says since when it is open. `operator_alert_problems` (migration 0031) holds when each open problem
opened (`created_at`), was last seen and last alerted, under the recording advisory lock. A problem not seen for its
source's window has cleared, so if it comes back it alerts anew: 20 minutes for the watcher's and the API's minute
checks, 3 hours for the host's hourly backup (a hung run holds the next start for up to its 1 h timeout), a day for
the ip-api key (seen only while sign-ups are screened). A reminder is due 10 minutes early, so sightings a refresh or
a timer's random delay apart do not push it a whole cycle. With no operator account (`PAY_OPERATOR_ACCOUNT_IDS` empty
or its first id naming no account) nothing is kept and every
sighting is only a WARNING log line (every 5 minutes per problem in a long-running process).

### 8.3 API tokens and webhooks (2026-10-12)

For an account's own server: it makes the invoice, sends the payer to its
`url`, and is told when it is paid.

- API tokens: `pay_tok_` plus 32 random bytes in base64url (`secrets.token_urlsafe(32)`), made on `/settings` behind a
  fresh passkey check, shown once; only the SHA-256 is stored, with a name and `last_used_at` (written at most once a
  minute). At most 20 per account. Sent as `Authorization: Bearer pay_tok_...`, accepted only on `POST /invoices`,
  `GET /invoices/{id}`, `GET /invoices` (with `order_id`, 0 or 1 item) and `GET /invoices/export.csv` (2026-10-21),
  and since 2026-10-21 on the invoice's cancel and accept-partial, the quantity link routes, the invoice schedule
  routes (section 18) and `GET /events` (section 7). A malformed, unknown or revoked token is
  401 `api_token_invalid` with `WWW-Authenticate: Bearer error="invalid_token"` (RFC 6750 section 3); with neither a
  token nor a live session cookie these routes answer 401 with `WWW-Authenticate: Bearer` and no error code
  (RFC 6750 section 3.1; RFC 9110 section 15.5.2 asks every 401 for one). A bearer on any other route counts for
  nothing, so a cookie-only route answers 401 without its cookie. A token request skips the
  per-IP open-invoice cap and the per-IP public-write budget; instead `PAY_MAXIMUM_OPEN_INVOICES_PER_ACCOUNT`
  (500) open invoices under a per-account advisory lock (which also makes a repeated `order_id` race-free) and
  `PAY_API_TOKEN_INVOICE_CREATES_PER_MINUTE` (60) creates of invoices and links together, and its 120 per minute is
  keyed by account (section 9).
  A repeated `order_id` answers 201 with the invoice already made.
- Webhook: one https URL per account (`PUT /account-webhook`, behind a fresh passkey check like the payout address,
  so a stolen session cannot read payment news elsewhere), signed per Standard Webhooks
  (https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md) with the
  `standardwebhooks` library: `webhook-id` (the account event's `evt_` id, the same on every retry; `test_...` for a test),
  `webhook-timestamp` (unix seconds of this attempt), `webhook-signature: v1,<base64 HMAC-SHA256 of
  "{id}.{timestamp}.{body}">` under the secret `whsec_` plus 32 random bytes in base64, shown on first set and on
  rotate (no overlap: the old secret stops at once). The body is `{"type", "timestamp" (ISO 8601, UTC), "data"}`;
  `data` is the invoice as `GET /invoices/{id}` returns it (with `order_id` and `metadata`). Sent: every account event
  the webhook's one rule takes (`webhook_event_scope.py`: an event with an invoice, today the `invoice.*` and
  `refund.*` types of section 8, or one whose type starts with a prefix listed there; never `account.*` or
  `operator.alert`) recorded after the URL was set; `GET /events` by API token reads exactly that set, by the same
  rule's SQL half (2026-10-21). Plus a test from
  "Send test" (2026-10-21): `webhook.test` (`{"message"}`) or, with `event_type` one of the EVM invoice event types an
  account's invoice can get (every `InvoiceEventType` but the Monero-only `seen` and `payment_held` and the refund
  events, which only operator-destination invoices get), a sample invoice in the state that event leaves one in (a
  paid one's `payment_uri` asks for 0, a late payment has no payment row of its own, only the sweep under way), built from `InvoiceAccountResponse` (destination the account's
  payout address; made-up deposit, payer and hashes; id `inv_TestSampLe7e57SampLe7e`, which reads 404). A test is
  signed like any delivery and marked three ways: `webhook-id` `test_...`, the body's top-level `"test": true` (a real
  event's body has no `test` key) and `"test": true` in the sample's `metadata`. Cryptomus' test webhook sends a test
  invoice of the status the merchant picks the same way (https://doc.cryptomus.com/merchant-api/payments/testing-webhook);
  an unknown `event_type` is 400 `validation_error`. The secret is stored AES-256-GCM sealed under a key
  HKDF-SHA256 derives from `PAY_INVOICE_SALT_SEED` (the account id as associated data), since it must be read back
  to sign.
- Per-invoice callbacks (2026-10-21, Cryptomus `url_callback`): `POST /invoices` may name a `callback_url`. It meets
  the webhook URL's rules (`split_webhook_url`: https, a host, no user info or fragment, no IP literal or `localhost`
  off the public internet; resolved and checked again at every send) and needs the account's webhook, whose secret
  signs it: without one 409 `webhook_not_set`, rather than making a secret nobody has seen. In addition to the account
  webhook, not instead: the account's server keeps every event, and the callback gets its invoice's, queued by the
  same claim as an `account_webhook_deliveries` row of `receiver_kind` `invoice_callback` with its URL fixed in
  `receiver_url` (migration 0032), the same body and the same `webhook-id` (the account event's `evt_` id, so a receiver getting
  both dedupes them), the account webhook's current secret, the same lease and retry schedule. A callback is queued
  while the account has a webhook, even one a 410 disabled (that 410 was the other URL's); a 410 from the callback
  fails that delivery at once and leaves the account webhook on; removing the webhook fails the pending callbacks as
  `no_webhook`. The URL is in the account view and the event `data`, never on the payer's view; /settings lists only
  the webhook's own deliveries. The invoices domain sits below account_webhooks, so app/lifespan wires the check
  into `InvoiceWiring.invoice_callback_url_check`.
- Delivery, at least once: every 2 s the API claims new account events (`account_events.webhook_claimed_at`, `FOR
  UPDATE SKIP LOCKED`) into `account_webhook_deliveries` (unique per event and receiver kind), then claims due
  deliveries under a 2-minute lease and posts each: 15 s timeout and 60 s for the whole attempt, no redirects (a 3xx
  is a failure), any 2xx is delivered; Discord messages go from a loop of their own (section 8.4), so webhooks never
  wait on them. An outcome is recorded only while the delivery is still pending under its claim's lease (its
  `next_attempt_at` unchanged): a pass recording late, or after the webhook was removed, changes nothing. A failure
  retries after 5 s, 5 min, 30 min, 2 h, 5 h, 10 h, 14 h, 20 h and 24 h plus up to 10% jitter, the spec's schedule
  (about 3 days, 10 attempts), then the delivery is `failed`; "Resend" on `/settings` queues one again from now. A
  failure's `Retry-After` (seconds or an HTTP-date) puts the next attempt no earlier than it, at most an hour away,
  and still counts as an attempt, so the span grows by at most 9 hours. A 410 Gone ("Sender should disable the webhook
  endpoint, and stop sending it messages") fails the delivery at once and disables the webhook
  (`account_webhook_endpoints.disabled_at`): its pending deliveries fail as `receiver_gone`, nothing more is queued,
  `/settings` says so, and setting the URL again turns it back on (2026-10-13). Not done: throttling a whole endpoint
  on a 429, 502 or 504 that names no `Retry-After`; each delivery backs off on its own schedule.
  SSRF: the host is resolved at send time, every address must be global (`ipaddress.is_global`, not multicast,
  IPv4-mapped unwrapped), and the request goes to that checked address with the `Host` header and TLS SNI and
  certificate check on the hostname (httpx `sni_hostname`); refused hosts fail the attempt as `refused_address`.
  httpcore pools a connection by scheme, IP and port alone, so a kept-alive connection to one host could carry a
  request for another host on the same IP past that host's certificate check: webhooks keep no connection alive, and
  Discord messages, which only ever go to one host, have their own client that does (2026-10-13).
  `PAY_WEBHOOK_ALLOW_PRIVATE_HOSTS` (off; e2e only) allows private hosts and http, and is read only while every enabled
  chain is a fork chain, as `PAY_E2E_DISCORD_API_BASE_URL` is, so it can never be on where polygon is enabled. Logs
  name the receiver host only, never the URL's path or query, the body or the secret.

### 8.4 Discord (2026-10-13)

Account holders are merchants and developers; besides the signed webhook for their server, a Discord channel gets a
short line per invoice event.

- Setting: `PUT /account-discord-webhook` behind a fresh passkey check like the webhook URL (it decides where payment
  news goes), the URL from Discord's "Copy Webhook URL". One account per Discord webhook (Discord limits per webhook,
  so two accounts sharing one could not be kept to it): another account's is 409 `discord_webhook_in_use`. Accepted only `https://` on `discord.com`,
  `ptb.discord.com`, `canary.discord.com` or `discordapp.com`, default port, path `/api[/v{n}]/webhooks/{id}/{token}`,
  no user info or fragment, and no query but `?thread_id=<snowflake>`: a forum or media channel's webhook posts only
  into a thread, else Discord answers 400 code 220001, which fails the message as `discord_thread_required` (Execute
  Webhook, https://github.com/discord/discord-api-docs/blob/main/developers/resources/webhook.mdx; 2026-10-13). Only
  the id, the token and the thread id are kept; every message goes to
  `https://discord.com/api/v10/webhooks/{id}/{token}?wait=true` (plus `&thread_id=`; answered once the
  message is saved; with `wait=false` an unsaved one returns no error, 2026-10-13) whatever host was typed, so no
  account can point the API elsewhere (the SSRF guard), and the host is still resolved and checked at send time as
  for the webhook. The token alone lets anyone post to the channel, so it is AES-256-GCM sealed like a
  webhook secret under its own HKDF key from `PAY_INVOICE_SALT_SEED`, never logged and never answered: the page shows
  the webhook's id only.
- Sent: the same events as the webhook (every `invoice.*` and `refund.*` account event recorded after it was first
  set) and `operator.alert` (only the operator's account has those, so the owner hears of alerts in Discord;
  2026-10-13), plus `discord.test` from "Send test message". The body, fixed when queued, is `{"content",
  "allowed_mentions": {"parse": []}, "flags": 4}`: one line in /activity's words, `**Paid**: 5.00 USD (5.00 USDC) ·
  2.00 of 5.00 USDC received · <note> · https://pay.7331.org/i/inv_...` (the received part while short; a wrong coin
  adds its amount), the note with Discord markdown escaped, no mention parsed (a note's `@everyone` pings nobody),
  `SUPPRESS_EMBEDS` so the link does not unfurl, at most 2000 characters, sent as `pay-7331` (`username`, over the
  webhook's own name; 2026-10-18). An invoice line has no address, IP, order id or metadata; an alert names the
  treasury Safe's or operator key's address to send to (2026-10-18). An alert is `**Needs the operator** (arbitrum,
  treasury low): <message> · Reminder every 1 h while it lasts (Settings, Discord) · https://pay.7331.org/activity`
  (`host` for an alert about no chain; a reminder starts `**Still needs the operator**` and adds `Open since` as a
  Discord timestamp; an alert must say which account and what it refers to, 2026-10-18). An alert is our
  own text (ip-api's quoted words lose `[]<>` and backticks first), so only `*`, `~`, `|` and `\` are escaped in it: its commands show as code and a copied alert has no
  backslashes in its words (2026-10-18). Every recorder (the watcher, the API's liveness check, `record-operator-alert`) only
  commits the event; the API's delivery claim queues it, as it does invoice events, so no recorder path is missed or
  queued twice.
- Delivery shares the webhook's (section 8.3): the same claim queues each event once per receiver kind into
  `account_webhook_deliveries` (`receiver_kind` `discord`), the same lease and retry schedule, but a loop of its own
  every 2 s beside the webhook's. Discord messages are claimed per account: only accounts whose
  `account_discord_webhooks.next_discord_send_at` has passed and that no pass holds (`discord_send_lease_ends_at`),
  the least recently sent first, at most 5 messages of each and 20 in all per claim, their webhook row locked `FOR
  UPDATE SKIP LOCKED` while claiming and then leased, so one pass at a time sends an account's messages, in event
  order (`account_event_id`), and the outcomes count only while the account's lease is still the claim's. Recording
  locks the accounts' webhook rows first, in `account_id` order, then their messages, the order queuing, removal and
  replacing take, so none deadlocks, `User-Agent: DiscordBot (https://pay.7331.org, 1)`
  (https://github.com/discord/discord-api-docs/blob/main/developers/reference.mdx). The sends stop at the first
  message not delivered or that spends the rate limit, and after 60 s; the rest are released unsent, and the account's
  next messages wait until the rate limit allows and the failed one is due again, so none overtakes an earlier one.
  An answer with `X-RateLimit-Remaining: 0` holds the account until `X-RateLimit-Reset-After`; a 429 is retried after
  its `retry_after` (else `Retry-After`) outside the schedule, and fails once the schedule's span (about 3 days) from
  when it was queued has passed (https://github.com/discord/discord-api-docs/blob/main/developers/topics/rate-limits.mdx).
  Every account's sends pause, through a Redis key with a TTL, on a 429 with `"global": true` (its `retry_after`) or
  without Discord's JSON body (10 minutes), and when 2,000 answers of 401, 403 or 429 not scoped `shared` fall in one
  10-minute window, until it ends: Discord restricts an IP at 10,000 such in 10 minutes, and a fifth leaves room for
  windows that do not line up and for the host's other traffic. The pause is read before each message with a 0.5 s
  cap, failing open, and a pause or count that Discord's answer asks for is written after the 60 s cap, so the cap
  cannot lose it. A Discord webhook can be set at most 10 times a day per account, counted once the URL and the
  passkey check pass. Replacing it drops the old one's hold (its rate limit, a failed message's retry): the account's
  waiting messages go to the new one at once, or when the pass that has some out records. Any 404, or JSON code 10015
  (Unknown Webhook) or 50027 (Invalid Webhook Token)
  (https://github.com/discord/discord-api-docs/blob/main/developers/topics/opcodes-and-status-codes.mdx), means the
  webhook (or its thread) is gone (Discord: "If a webhook returns a 404 status you should not attempt to use it again",
  rate-limits.mdx; every 404 since 2026-10-13): it is marked deleted in Discord, its pending messages fail, nothing
  more is queued or sent, and /settings says so until the account sets one again; if the account already set another
  (or another thread), the message goes to that one at once. Any other 401 or 403 is a failure on the schedule.
  Removing it fails the pending messages.
- `PAY_E2E_DISCORD_API_BASE_URL` (the fork e2e's receiver container standing in for `https://discord.com/api`) is
  read only while `local_fork` is the one enabled chain, so production cannot turn it on.

## 9. Spam protection and feature flags

Anyone can make an invoice, signed in or not (2026-10-09), and point it at any address; the float pays refunds only
on the operator's own destination (section 6), so an invoice to someone else's address cannot cost the float more than
gas. Public routes are guarded by:

- GCRA rate limits in Redis (`infrastructure/rate_limit`): 120 requests per minute per IP on every
  route but health and `/static/`, 10 per minute on public writes (`POST /public/...`, `POST /invoices`,
  `POST /payment-links`, `POST /invoice-schedules`, sign-up, sign-in, add-passkey and delete-passkey, and POST or PUT on `/account-api-tokens`,
  `/account-webhook` and `/account-discord-webhook`). A request with a valid API token on its routes (section 8.3) is counted by account
  instead: 120 per minute, and `PAY_API_TOKEN_INVOICE_CREATES_PER_MINUTE` on `POST /invoices` (2026-10-12),
  `POST /payment-links` and `POST /invoice-schedules`. `GET /invoices/export.csv` reads every invoice of the account, so it also has its own 6 per
  minute, per account by token and per network otherwise (2026-10-21).
  The client IP is `X-Real-IP` only from a peer in `PAY_TRUSTED_PROXY_ADDRESSES` (the reverse proxy's pinned address
  on a shared Docker network, 2026-10-09), else the TCP peer. Every per-IP control counts an IPv6 address's /64
  and keys Redis by its salted hash, never the IP (2026-10-09). One site commonly holds a whole /48 (65,536 /64s), so
  public writes from IPv6 are also counted over the /48 together: `PAY_PUBLIC_WRITES_PER_MINUTE_PER_IPV6_48` (60).
- Open-invoice cap: at most `PAY_MAXIMUM_OPEN_INVOICES_PER_IP` (default 5) unclosed invoices per client network
  hash, counted on both creation routes under a Postgres advisory lock on the hash (a burst cannot pass it), else 429
  `too_many_open_invoices` with `Retry-After` set to when the first of them closes by the clock (`expires_at` unpaid,
  `underpaid_open_until` underpaid) plus a watcher tick, and none when no open one has a clock; the pages turn it into
  "the next one in about N minutes, at HH:MM" (2026-10-11). Underpaid invoices stay open, so they count. A quantity
  link payer's slot is such a creation, so one network holds at most that many of a link's slots at once (2026-10-11).
  From IPv6 the /48 is capped too, on top of the /64: `PAY_MAXIMUM_OPEN_INVOICES_PER_IPV6_48` (default 50), counted
  over `invoices.client_ipv6_48_hash` under its own advisory lock, taken after the /64's, with the same refusal.
- Public invoice capacity: every open invoice is read on every watcher tick, so a public invoice (signed out, or a
  quantity link's payer) is refused with 429 `public_invoice_capacity_reached` (`Retry-After` 300) while a chain it
  offers already has `PAY_MAXIMUM_OPEN_PUBLIC_INVOICES_PER_CHAIN` (default 2000) open public invoices. No lock: a burst
  passes it by at most the creations running at once.
- Quantity links take a slot only on a payer's "Pay 2.00 USDC on Polygon", never on a page view, so reading links
  holds nothing; an unpaid slot frees itself when its invoice expires.
- The smallest fiat invoice is 1.00 USD or EUR; a crypto amount must reach the asset's `minimum_sweep_amount`.
- ip-api.com (`PAY_IP_API_KEY`, a paid key) on sign-up only, both routes
  (`POST /accounts/registration-options` before the passkey prompt and `POST /accounts`): invoice creation, payers and
  API tokens are never screened (2026-10-20; until then invoice creation and the payer's asset pick, quote refresh and
  "I paid" recheck were). Flags `PAY_SCREENING_BLOCK_PROXY` (VPN, proxy, Tor; default off,
  many crypto payers use VPNs), `PAY_SCREENING_BLOCK_HOSTING` (data centres; default on),
  `PAY_SCREENING_BLOCKED_COUNTRY_CODES` (default empty), `PAY_SCREENING_FAIL_CLOSED` (default off). Verdicts are
  cached in Redis for an hour by IP hash. A paid key can expire (https://members.ip-api.com/faq),
  so once ip-api rejects it (HTTP 403, `invalid/expired key, ...`; or a key restriction) every lookup fails and
  screening admits everyone: each such lookup leaves a 10-minute Redis marker with ip-api's message, and the API reads
  it once a minute and records `operator.alert` `ip_api_key_rejected` (once per problem, section 8.2; 2026-10-13).
  The lookup failure's log line names its reason and ip-api's message, never the IP or the key.

## 10. Pages (frontend/)

One React app (Vite, TypeScript, shadcn/ui on Tailwind, Motion for React), served from `/static/` on this origin, no
CDN (2026-10-11, replacing the no-build-step pages). The ids and test ids the e2e scripts read are renamed together
with `e2e/`.
Mobile first at 390 px, one column, centered and never stretched on a wide screen; light and dark follow the system.

The payer's pages (`/i/` and `/l/`, their sheets and the API refusals a payer can meet) speak the browser's language,
the first of `navigator.languages` we have (https://developer.mozilla.org/en-US/docs/Web/API/Navigator/languages),
else English, with no switcher (2026-10-21): English, Spanish, Portuguese
(Brazil; any Portuguese), Russian, Turkish, Simplified Chinese (zh, zh-CN, zh-SG, zh-Hans; Traditional falls through,
read with `Intl.Locale#maximize`, and a bare zh beside a Traditional tag gets English), German and French; `<html lang>` follows. The account pages, the maker's own lines on
a payer page ("This is what your payer sees", "Your payer picks the coin.") and `/docs` stay English. A small typed
dictionary per language in `frontend/src/lib/payer_strings/` rather than a library: i18next loads catalogues through a
backend plugin or inlines them all (https://www.i18next.com/overview/getting-started) and Lingui needs a macro build
plugin (https://lingui.dev/installation); here each language is one same-origin chunk the payer route loads with the
page (https://vite.dev/guide/features#dynamic-import), so nothing flashes in English and nothing comes from another
origin. Only the words change: crypto amounts stay the exact formatted API string and fiat stays the English "$5.00"
in every language, so a fiat figure never uses another decimal mark than the crypto amount beside it ("50,00 $" next
to "201.946 POL" could be misread a thousandfold); dates and lists go through `Intl` in the payer's language (English
keeps its old output, which the e2e reads, and the e2e pins its browsers to en-US).

The account pages share one account sheet from the top bar's "Sign in" or "@username" (a Drawer on a phone, a Dialog
on a wide screen), with no visible title or blurb: signed out it is passkey first, one "Sign in with passkey" and under
it "New here? Create an account", which shows the sign-up form (username, payout address prefilled from a receiver
typed on `/`, "Create account with passkey"); a sign-in or sign-up closes it. Signed in it says "Signed in as
@{username}" and where it is paid, with "Sign out". Signed in, `/`, `/activity` and `/settings` end in a bottom tab bar
(New, Activity, Settings: an icon over a short label, the current tab marked, over the phone's safe-area inset; at the
bottom of the card on a wide screen); never on a payer page, never signed out (2026-10-14). Light and dark follow
the device, with no switch and nothing stored.
The site itself uses no API token and keeps nothing in `localStorage` (2026-10-09; tokens are for an
account's own server, section 8.3). It uses the browser's own
`PublicKeyCredential.parseCreationOptionsFromJSON`, `parseRequestOptionsFromJSON` and `credential.toJSON()`
(https://developer.mozilla.org/en-US/docs/Web/API/PublicKeyCredential), no library. Requests carry no Authorization
header; the cookie rides along.

- `/`, the one screen that makes a link, sentence-shaped with no labels the content already explains
  (2026-10-14): a big USD amount (at least $1, at most 999,999,999.99) with $1/$2/$5/$10 as small round chips centered
  under it; the receiver address, shown whole on two lines with a Paste button (required signed out; signed in it is the
  payout address, shown short with "Change"; a wrong amount or address shows under its field); one row of the accepted
  coins (their logos overlapping, "POL, USDC, USDT" and a chevron) that opens a sheet with the coin toggles: the first
  chain's coins, the account's saved coins ticked signed in, else the chain's `default_accepted_assets` (POL, USDC,
  USDT), the rest one tap away; signed in, the ticks a link is made with are saved for next time. Under them a quiet
  "+ Add a note" once the address is valid, which shows the note field focused, and signed in "Options" on the same
  line, naming the changed ones ("Options: EUR, 15 min"): a sheet with the currency (money or a coin; the big amount
  then reads "€" or the coin's symbol, and a coin amount is paid in that coin only), the chain when more than one is
  enabled, "How many payers", time to pay (presets and minutes), the back-to-shop link, top-ups open until, accepted
  shortfall %, order id, metadata JSON and the contract-wallet tick; a hint only where the value doesn't explain itself;
  a wrong one shows under its field and "Create link" opens the sheet at it. `/new`, the old account form, redirects to
  `/?options=1`, which opens that sheet signed in. "Create link" (pinned at the bottom) posts `POST /public/invoices`
  signed out (with `assets` when the ticks differ from every coin), `POST /invoices` signed in, or with more than one
  payer `POST /payment-links` ("Create link for N"; USD or EUR, no order id, metadata or top-up deadline), or with
  Options' "Repeat" (Never, Daily, Weekly, Monthly; one payer, no top-up deadline, the order id a prefix, the time to
  pay moved to 12 h while untouched) `POST /invoice-schedules` ("Create monthly invoice") and then `/activity#schedules`
  (2026-10-21, section 18); otherwise it goes to
  the invoice's (or the link's `/l/{id}`) own page, so the address bar is the link, under a share overlay (a Drawer on a phone, a Dialog on a wide screen):
  the link's QR and the link on one line, tap to copy, "Share link" through the Web Share API or a copy, "Make
  another" and "Done". Done shows the payer view as a payer sees it, with one quiet "This is what your payer sees"
  line, a share icon back to the overlay and the coin's forward button disabled, since the coin choice is the
  payer's and one-way (on a one-coin invoice nothing is chosen, so it stays live and only previews the send screen); the creating tab keeps this in its history entry (a reload keeps it, Back returns to a fresh
  form), and a signed-in account sees the same line on its own invoices (2026-10-11).
- `/i/{id}`, the payer's invoice, dumb simple (2026-10-11): one screen per state, at most a forward and a back
  button, no username; the top bar holds the brand only (the countdown ring on the send screen). Pick a coin: the
  amount in its currency (and EUR or USD), the note, a visible "Choose how to pay" over the accepted coins (rows with
  a radio mark, none dimmed) with each one's estimate; a tap selects, and the forward button names amount, coin and
  network ("Pay 5.00 USDC on Polygon", one muted line under it: "Send on Polygon only. You can't switch coins after
  this."): that one tap commits the coin (one-way) and is the payer's confirmation of coin and network, so the send
  screen follows at once; "Back" deselects. An invoice whose coin is already fixed (one coin, or committed in another
  tab) shows the same screen with that coin picked and no Back, and the tap only confirms; the confirmation is kept per
  tab (2026-10-14). Once the invoice's `expires_at` passes it shows Expired, not the coins.
  Send: the amount left (large, tap to copy), USD and EUR under it (underpaid: the rest's USD worth, "more to send"
  and what arrived, both to the same places), the coin on its chain and "Only send X on {chain}", the QR (EIP-681
  `ethereum:<address>@137?value=<wei>` or `ethereum:<token>@137/transfer?address=<deposit>&uint256=<units>`; a small
  text link switches to an address-only QR because many wallets read only the address,
  https://ethereum-magicians.org/t/tools-for-implementing-eip-681-and-eip-831/1320), the address (tap to copy), a
  countdown reading "29:59 left", and one primary: "Pay with wallet" (`window.ethereum`: switch to the chain,
  send the remainder) or "Open in wallet" on the payment URI. Once the wallet returns a transaction hash the primary
  is a disabled "Waiting for confirmation..." until the invoice shows the payment; after 2 minutes with nothing seen it
  offers "Send again" under a warning that it pays twice (2026-10-11: a payer tapped twice and paid twice). A lapsed
  price for the rest: the amount, the note, that what arrived is safe, "Refresh price" (and "Get my money back" when
  offered). Confirming: a calm ring and what arrived (`amount_received` plus the payments not final yet). Settled
  screens are one hero, one heading, one muted line and at most one sentence (2026-10-14). Paid (an overpay
  too, heading "Paid"): a check, what arrived and the note ("1.00 USDC · Logo design"), for an overpay one sentence on
  the extra ("10.50 POL extra went back to 0x1234...abcd" linked to the refund, "is going back ... by itself", or "Ask
  the seller for the extra; they settle it with you."), for an accepted underpayment "The seller accepted what arrived.", a quiet
  "View payment" link to the explorer, and "Back to shop" when there is a return URL. Closed: the amount and the note
  in one line and one sentence (expired with nothing paid: "ask the seller for a new one"; refunded: "Your 20.00 POL
  went back to 0x5678...ef01" linked to the refund). When the payer may ask for money back (an underpaid amount after
  the window, section 6): "Get my money back", signed in the wallet; once a refund is underway the page says "Refund
  on its way" with the amount and the address it goes to, and offers nothing to pay with until it closes as Refunded
  ("Refund held up" when every refund stopped short of final: the seller follows up; 2026-10-14). Wallet errors read as
  plain sentences (declined, unknown network, a request already open, not enough funds). The status updates every 3 s
  while it can change. When the seller's account set a support contact, the coin choice, send, price-lapsed, paid and
  closed screens show one small "Need help? Contact the seller" link above their buttons (2026-10-11).
  Money in a coin the invoice did not ask for shows as a calm Alert under the note, no button (INC-001, 2026-10-11):
  "We got 50.25 POL. This link takes USDC only, so it wasn't counted. It goes to the seller, who can send it
  back.", then "The seller is sending / sent it back to 0x..."; the confirming screen then shows the support link too.
  On `/activity` the invoice overlay lists it under "Wrong coin received" with "Send it back" per sender after a
  confirm on operator-destination invoices, else a line saying why not.
- `/l/{id}`, a quantity link's payer page, as dumb simple as `/i/`: the amount (and EUR or USD), the note, "3 of 10
  left", "Choose how to pay" over the coins with their estimates; a tap selects, "Pay 2.00 USDC on Polygon" takes a
  slot and goes on to the payer's own `/i/{id}` already confirmed, on its send screen (the same one tap, 2026-10-14),
  "Back" deselects. Back from that invoice shows "You started paying" with "Go to your
  invoice" (the `/l/` history entry keeps its id) rather than the picker, so a payer does not take two slots by
  accident. Otherwise one heading and one line: "All taken for now" (with "Check again"), "Sold out", "Link closed".
  The maker sees the payer view with the forward button disabled (2026-10-11).
- `/activity`, the account's invoices and events, behind the passkey sign-in, with no page blurb (2026-10-14): one
  line with "Paid today" (summed by the time of each invoice's `invoice.paid`/`invoice.overpaid` event, else its
  `updated_at`: the resource has no paid time), a small green "live" dot while the stream is up ("Reconnecting..." only
  while it reconnects; the state is read out to screen readers) and a bell, "Notify me on this device", offered only
  where the page can notify (section 8.2). Then "Needs you", only when something does: the loaded invoices that wait
  on the account, read from the Invoice resource alone and only in states that move on, so each clears itself (a sweep
  that failed, a refund that ended `needs_owner` or failed with its sender still offered, an underpaid invoice past its
  window with no refund of its coin under way), one sentence each and "Resolve", which opens the invoice's overlay;
  and a Monero wallet whose `lookahead_warning` is true, with the exact `set subaddress-lookahead 50:1000` line (tap to
  copy) and "I set it", which clears it (2026-10-14).
  Money the owner settles with the payer (from a contract or an unknown sender, or wrong-coin money that can't go back
  by itself) never clears, so it is a line in the invoice's overlay instead. Then the tabs. "Invoices": by
  day (Today, Yesterday, then dates; `GET /invoices`, reloaded after each event, "Load older" for the next page by its
  `next_cursor`), each row the note (or "No note"), the coin and time (for a quantity link's invoice, "Link · 1 of 2
  sold"), and the amount over a status pill; a tap opens the overlay (a Drawer on a phone, a Dialog on a wide screen:
  share the link while it can be paid, copy, open, cancel after a confirm, accept partial, refund a sender the API
  offers after a confirm). "Links": the quantity links, "Sold 3 of 10", a drawer per link with its payers' invoices,
  copy or share, and "Close link" after a confirm (2026-10-11); under them "Repeating invoices" (2026-10-21, section
  18; a fifth tab doesn't fit 390 px): the invoice schedules, "Monthly · next Nov 20 · 3 made", a drawer per
  schedule with its invoices, pause or resume, and "Cancel schedule" after a confirm; `/activity#schedules` opens
  there. "Updates": the events newest first with "Load older"
  (`GET /events`). For each new event a toast (at the bottom on phones) plus a browser notification (Notification API,
  asked on the bell, https://developer.mozilla.org/en-US/docs/Web/API/Notification) while the tab is open (section 8.1;
  2026-10-11). Making an invoice is the tab bar's "New", not a button here.
- `/settings`, behind the passkey sign-in: a plain "Settings" heading and rows, each showing its value and opening its
  own sheet (a Drawer on a phone, a Dialog on a wide screen; 2026-10-14), no section blurbs. "Payout" (the short
  address; the sheet shows it whole, tap to copy, then the new address and "Confirm with passkey", saying that links
  already made keep the old one), "Monero wallet" (only while a Monero chain is enabled, read from `GET
  /account-monero-wallet`'s `monero_chains`; the short address or "Off"; the sheet shows the address short, tap to
  copy, the lookahead line with "I set it" until confirmed, and "Set"/"Change": the primary address, the secret view
  key in a password field kept only in the form, a network picker when more than one Monero chain is enabled, the
  three receiver notes of section 14 and "Confirm with passkey"; 2026-10-14), "Coins" (the picker; its "Save" sends only `accepted_assets_by_chain` to
  `PATCH /accounts/current`), "Support contact" (the contact or "Off"; its "Save" sends only `support_contact`, empty
  turns it off) and "Passkeys" (the count; the list with the one this session signed in with marked, rename, add,
  delete after a confirm; the last one cannot go) (2026-10-11). Then "Connect": "Discord" (the webhook's id, "Off" or
  "Gone in Discord"; the sheet shows the id and its thread's when the URL named one, "Set"/"Change" behind the passkey
  with the URL pasted from Discord's "Copy Webhook URL" and never shown back, "Send test message", the last message's
  status, "Remove" after a confirm, and an alert once Discord deleted it; 2026-10-13, section 8.4), "Webhook" (the
  URL's host, "Off" or "Stopped"; the sheet shows the URL with "Set URL"/"Change URL" behind the passkey, the secret
  shown once on first set or "New secret", "Remove", the test event's type (`webhook.test` first, then every EVM
  invoice event type; 2026-10-21) with "Send test", and the last 20 deliveries with status and
  "Resend"; an alert once the receiver answered 410 Gone, with test and resend off until the URL is set again) and
  "API tokens" (the count; the sheet lists name, made and last used, revoke after a confirm, and "Make an API token"
  asks a name and the passkey and shows the token once) (2026-10-12, section 8.3). Then "Records": "Invoice export"
  ("CSV"; the sheet takes an optional from and to date, whole UTC days, and "Download CSV" is a plain link to `GET
  /invoices/export.csv`, so the browser streams it to a file; 2026-10-21). Each sheet acts at once.

## 11. Layout

```
pay-7331/
  README.md PLAN.md SECURITY.md docker-compose.yml .env.example .pre-commit-config.yaml
  contracts/          Foundry: src/, test/, script/DeployDepositForwarderFactory.s.sol
  frontend/           Vite + React app: src/ (pages/, components/ with shadcn's ui/, hooks/, lib/), public/
                      (coin_logos/), built into dist/ (ignored) that the API serves
  xmr-scanner/        the Rust Monero sidecar (section 14)
  sdk/python/         the Python client for the API
  deploy/             production and sandbox Compose files, deploy scripts, Caddy blocks, backups and their systemd units
  e2e/                the fork stack (fork_stack.py) and its flows: polygon_fork_flow.py with its scenario modules,
                      browser_flow.py, mixed_network_flow.py, arbitrum_sequencer_flow.py, python_sdk_flow.py; the
                      live runs polygon_live_flow.py and arbitrum_live_flow.py with their
                      run_*.sh wrappers (section 12)
  backend/
    chains.toml       public chain facts: ids, RPC URLs, assets, feeds, factory, finality, poll interval
    migrations/       Alembic, forward only
    app/
      main.py watcher.py cli.py config.py log_events.py rate_limit_middleware.py request_log_middleware.py
      lifespan/       Resources per process role, composition, request dependencies
      payments/       domains, top imports down:
                      chain_watcher | pages | account_webhooks (the account's webhook and its delivery)
                      payment_links (quantity links; each payer's slot is an invoice)
                      | invoice_schedules (a new invoice each period, made by the watcher; section 18)
                      invoices
                      account_events (each account's event feed and its SSE stream)
                      accounts (accounts, passkeys, sessions, the session check)
                      monero_wallets (an account's Monero wallet and its subaddress minors, section 14)
                      settlements (sweeps, refunds, nonces) | price_quotes | sign_up_screening
                      chains (the registry read from chains.toml)
      infrastructure/ deposit_factory_api | chainlink_api -> evm_rpc_api (AsyncWeb3 with failover, Multicall3)
                      monero_sidecar_api (the xmr-scanner sidecar's HTTP API, section 14)
                      ip_api | rate_limit -> redis | postgres
      shared/         logging, errors, request helpers
```

Tables (each owned by one domain): `invoices` (with `account_id`, null for a signed-out invoice, `client_ip_hash`
and `payment_link_id`); `payment_links` (account, chain, coins, fiat amount and currency, destination, note, quantity,
lifetime, accuracy, closed); `invoice_schedules` (account, status, amount and currency, the chains, coins and
destination fixed at creation, the invoice fields, interval, start, end, next period index and attempt, counts,
last failure; invoices carry `invoice_schedule_id` and `invoice_schedule_period_start`, unique together; section
18); `price_quotes`; `invoice_payments` (tx hash, log index, sender, amount, block number and hash,
final); `invoice_stray_payments` (the same per row plus asset and whether the sender runs code, 2026-10-11);
`deposit_sweep_events` (chain, tx hash, log index, deposit, token, amount, block); `outgoing_operations` (kind
sweep, refund, or the invoice-less key operations `top_up`, `treasury_return`, `treasury_safe_creation`; invoice,
chain, operator key, nonce, token, amount, recipient, status `pending`, `broadcast`, `mined`, `final`,
`failed`, `waiting_for_float`) and `outgoing_attempts` (operation, raw transaction, hash, fee caps, receipt);
`operator_nonces` (chain, operator address, next nonce); `operator_key_states` (chain, operator address, role
`refund` or `sweeper`, status `active`, `frozen` or `retired`, reason, changed at; 2026-10-13, section 5.1); `refund_offers` (nonce, sender, amount, text, expires,
consumed); `account_events`; `accounts` (username unique, payout address, WebAuthn user handle);
`account_passkeys` (account, credential id unique, COSE public key, sign count, transports, label, last used);
`account_sessions` (SHA-256 of the cookie token, passkey, expiry); `passkey_challenges` (SHA-256 of the challenge,
purpose, account or the pending sign-up, expiry); `chain_cursors` (last block read per chain for logs and native
attribution); `account_monero_wallets` (account, chain, primary address unique per chain, sealed private view key,
network, major index, next minor index, confirmed lookahead, replaced at; section 14) and `monero_payments` (chain,
invoice, txid, output public key unique per chain, piconero, height, block hash, pool and cross-check state) and
`monero_scan_cursors` (the sidecar's last scanned height and block hash per chain), with
`invoices.monero_wallet_id` and `subaddress_minor` (both set on an XMR invoice, neither on any other).

Environment (`.env.example` is the full list): `DATABASE_URL`, `REDIS_URL`, `LOG_LEVEL`, `PAY_PUBLIC_BASE_URL`,
`PAY_ENABLED_CHAINS`, `PAY_MONERO_NODES_<CHAIN>` (comma-separated `url=operator` pairs replacing a Monero chain's nodes, operator named
explicitly; https only on mainnet), `PAY_MONERO_SIDECAR_URL` (the xmr-scanner sidecar's wallet checks; empty: no
Monero wallet can be set), `PAY_OPERATOR_PRIVATE_KEY_<CHAIN>` (SecretStr; the refund key: gas and the refund float only),
`PAY_SWEEPER_PRIVATE_KEYS_<CHAIN>` (comma-separated SecretStr; gas only; empty: the refund key sweeps),
`PAY_TREASURY_SAFE_ADDRESS_<CHAIN>` (the treasury Safe; empty: no top-ups),
`PAY_DEFAULT_DESTINATION_ADDRESS` (the operator's payout address, the only refundable destination),
`PAY_OPERATOR_ACCOUNT_IDS` (the operator's own accounts, comma-separated: the first gets operator alerts, and only
their invoices get refunds),
`PAY_MINIMUM_REFUND_USD`, `PAY_MAXIMUM_OPEN_INVOICES_PER_IP`, `PAY_INVOICE_SALT_SEED` (SecretStr, required),
`PAY_IP_API_KEY`, `PAY_IP_HASH_SALT`, the
`PAY_SCREENING_*` flags.

## 12. Verification

- Fast checks, all in `.pre-commit-config.yaml` (`uvx pre-commit run --hook-stage pre-push --all-files`): ruff format
  and check (backend, e2e, sdk/python), ty (backend, sdk/python), vulture, xenon, semgrep house rules,
  import-linter, deptry, `forge fmt --check`, `forge build` and `forge test`, the frontend's `tsc -b`, oxlint and knip,
  the frontend's generated API types (`pnpm generate:api-types`) checked against the backend's OpenAPI document,
  zizmor for the workflows, and a check that the docs use plain ASCII dashes and dots.
- `e2e/polygon_fork_flow.py`, run by hand when a change needs it: starts anvil in a container (`--fork-url
  https://polygon.drpc.org --block-time 1 --slots-in-an-epoch 1`, pinned 20 blocks behind the head; drpc because a fork
  needs archive state and publicnode is pruned; anvil's `finalized` then trails `latest` by 2 blocks, like Polygon;
  checked 2026-10-09), deploys the factory, brings up Postgres, Redis, api and watcher against it, signs up an account
  on `/` whose payout address is the stack's `PAY_DEFAULT_DESTINATION_ADDRESS` and whose id is the stack's
  `PAY_OPERATOR_ACCOUNT_IDS` (the stack's first account, so 1) (Chromium's CDP virtual authenticator,
  `e2e/account_passkey_session.py`, which the browser and live runs use too), and drives: USDC invoice paid exactly;
  POL invoice in EUR underpaid then topped up; POL overpaid, the excess refunded by itself once final (from the
  float); an invoice left to expire, then paid late and swept by the rescan; a third party sweeping a deposit in the
  payment's block (the payment still attributed); a payment seen at `latest` and reorged out before finality
  (`anvil_reorg`), then paid again; a refund attempt abandoned because another operator transaction took its nonce
  (mining paused, the nonce taken at higher fees), which must end `needs_owner` with its amount reserved and no
  endless bumps, then the operator's `refunds mark-unpaid` (refused without `--nonce-taken-by` or with the attempt's
  own hash, accepted with the nonce-taking transaction), no automatic second refund, and the payer's signed request; a
  refund beyond the float waiting while later sweeps go out; a refund to an EIP-7702 account; no operator key with gas
  (the sweepers and the refund key drained: the sweep waits); an underpaid refund refused inside the payment window and signed after it; three inputs from one
  address past the amount, the rest refunded by itself; a payment mined inside its quote but final after it lapsed,
  credited at the quote; a USDC payment re-mined by `anvil_reorg` behind another transfer at a new log index (one
  final row); the POL feed made to revert (`anvil_setCode`) while wrong-coin POL and a POL overpay go final (no
  refund, a WARNING deferral, the overpaid invoice swept and closed), then restored (each refunded once); wrong-coin
  POL sent while the watcher is stopped and 300 blocks mined (a no-sender row for the owner), then attributed by
  `record-stray-payment-from-transaction` and refunded once; the watcher made to fail between an operation's new
  status and its invoice effect (`PAY_E2E_FAIL_AFTER_OPERATION_RESOLVE`, read on `local_fork` only) for a refund
  going final, a refund going `needs_owner` and a failing sweep (nothing recorded), then restarted without it (each
  event once, `sweep_blocked` set); a paid deposit whose invoice rows are deleted before the sweep, found again by
  `recover-deposit --from-log` from its `invoice_create` line with no reachable database and swept with the call it
  prints (section 3); and the operator keys (2026-10-13, section 5.1, `e2e/operator_key_scenarios.py`, three fresh
  sweeper keys, dev account #0 the refund key with its mainnet EIP-7702 delegation cleared on the fork): three sweeps
  paid in one block go to three sweepers; `treasury create-safe`, `setup-transactions` and `execute-setup` make and
  set up a funded Safe that tops a drained refund key back to its target; an allowance cut below what was spent
  pulls nothing and alerts `allowance_exhausted` once; a sweeper used outside the gateway is frozen, alerted and
  skipped, then `unfreeze-key` makes it active for good; `retire-key` stops a sweeper's sweeps and returns its USDC,
  then its POL, to the Safe; and merchant operations (2026-10-21, `e2e/merchant_operations_scenarios.py`): `GET /rates`
  (USDC at 1, its `Cache-Control`), a fresh account's API token and webhook made in Settings, a USDC invoice paid and
  swept, then the CSV export by token and by session (the same bytes; header, invoice, payment and sweep rows; a
  formula note neutralised; an empty future window), every sample test event signed, marked and in its state (the
  sample id reads 404, an unknown type is 400), and the docs' example responses captured, anonymised and compared in
  shape with `frontend/src/pages/developer_docs/developer_docs_responses.json` (`PAY_E2E_WRITE_DOCS_RESPONSES=1`
  rewrites it); and invoice schedules (2026-10-21, `e2e/invoice_schedule_scenarios.py`, section 18): a daily
  schedule moved back a day by SQL makes exactly one invoice with `schedule.invoice_created` signed to the webhook,
  a rewound schedule makes none again, the next day's invoice comes once the first is paid, pause makes none,
  resume bills no period missed while paused, cancel ends it (resume then 409), and a one-invoice schedule
  completes; its docs responses go into the same JSON. Scenarios with refunds pay the operator's destination
  (section 6). It checks destination balances,
  statuses and the recorded `account_events`, and writes its log to `--scratch-folder`. First, while the
  stack is clean (2026-10-12): `reconcile` exits 0; a sweep reverting (the destination rejects POL) leaves a paid
  deposit unswept, `reconcile` exits 1 naming it and one `operator.alert` lands on the account, not repeated a check
  later; swept by hand it exits 0 again; the watcher's process frozen, its container turns unhealthy, then healthy.
- `e2e/browser_flow.py`, run by hand when a page or the flow behind it changes: the same stack (`e2e/fork_stack.py`),
  driven through the pages in headless Chromium (Playwright): the signed-up account fills / and its Options, a signed-out visitor
  on `/` is refused without a receiver and below $1 and then makes a $1 invoice to the operator's address, which,
  overpaid, gets no float refund (`refund_offer.reason` is `receiver_settles`), the payer (390 px, an injected
  EIP-1193 wallet holding a local key) pays a USDC invoice from the wallet button, pays a EUR invoice half by a direct
  send and the rest from the page, picks POL on the account's $1 invoice to the operator's address and sees an
  overpay refunded by itself with nothing to sign, and after an
  underpaid invoice's window asks for the money back by signing on the page; the account makes a quantity-2 link on
  /, two payers each pick a coin and pay, a third sees "Sold out", two slot requests at once for a quantity-1 link
  get exactly one invoice, and the account closes that link from /activity. Last, the watcher is stopped
  and the API records one `watcher_not_ticking` operator alert, then the watcher starts again. It checks what the pages show (the QR
  decoded with OpenCV), the API, balances and events, and screenshots each step to the scratch folder.
- `e2e/run_live_flow.sh` runs `e2e/polygon_live_flow.py` on live Polygon mainnet against production, by hand: it
  signs up a throwaway `live_e2e_...` account and deletes it at the end. `e2e/run_live_flow.sh pay_invoice <invoice
  id> <amount> [symbol]` (2026-10-15) signs nobody up: it pays an existing invoice that amount of USDC (default), USDT
  or POL from a derived payer the float funds, picking the coin through the public route first when the invoice
  waits for one, and returns the payer's leftovers; run twice for an underpay then a top-up. Float refunds are only
  for the operator's own accounts' invoices, so the refund scenarios make theirs with an API token of one, read from
  the 0600 file `PAY_LIVE_OPERATOR_API_TOKEN_FILE` names (never a command line, never printed); without it they are
  skipped. Mint that token on the first account `PAY_OPERATOR_ACCOUNT_IDS` lists (or another listed account with no
  webhook endpoint), never on an integrator's account: its invoices fire that account's webhooks and outlive the run.
- Amoy run once: needs Amoy POL for the operator and payer from a faucet claimed by hand (QuickNode, one claim per
  12 h, https://faucet.quicknode.com/polygon/amoy) and Circle test USDC.

## 13. Build order

1. Skeleton on `main` (this file, layout).
2. In parallel, each on its own branch from `main`: contracts (section 3); backend (sections 2, 4-9, 11);
   frontend (section 10, against the section 7 API).
3. Integrate, run section 12 on the fork, fix forward.
4. Deploy at pay.7331.org (a Compose project behind a reverse proxy), deploy the factory on Polygon mainnet and fund
   the operator with gas and a small refund float.

Not in version 1: one factory address across chains, batch sweeps, EIP-1271 refunds for
contract wallets, Cloudflare Turnstile, email notices, a dashboard beyond `GET /invoices`.

## 14. Monero chain kind (2026-10-14)

A Monero (XMR) variant (2026-10-14); free public nodes, no own node; iterate on AcceptXMR's ideas, not its code. The
numbers below come from a stagenet spike on 2026-10-14 (faucet coins, nothing real spent) and read-only mainnet
measurements.

- Shape: a second chain kind beside EVM, `kind = "monero"` in chains.toml (`evm` is the default and everything
  before this section). The receiver's own wallet is the destination: each invoice gets a fresh subaddress of it,
  derived from the account's primary address and private view key (https://docs.getmonero.org/public-address/subaddress/),
  and the payer pays the seller directly. No deposit contract, no sweep, no gas, no float, no Safe, no operator keys
  and no treasury on this kind; the server holds view keys only (watch, never spend). Everything else (accounts,
  quantity links, `/activity`, webhooks, Discord, API tokens) carries over: an XMR invoice is an invoice.
- Account-only: deriving and watching needs the private view key, so XMR invoices exist only for accounts that set a
  Monero wallet in Settings. Signed-out `/` stays EVM.
- Wallet setup (Settings row "Monero wallet", passkey-gated like the payout address): the primary address (95
  characters; mainnet prefix 4, stagenet 5) and the private view key. Checks before anything is saved: address
  checksum and network, and `a·G == A` against the public view key inside the address (a wrong view key derives
  subaddresses nobody can spend, so payers' money would burn); refused otherwise. Sealed at rest like webhook secrets
  (section 8.3: AES-256-GCM under an HKDF key from `PAY_INVOICE_SALT_SEED` with its own `info`, and
  `account_id|chain_key|primary_address` as associated data, so a sealed view key opens only on its own row), so a
  database dump alone reveals no receiver's history. The sheet tells receivers: use a wallet dedicated to this, the
  gateway uses account (major) index 1, and widen the lookahead when `/activity` asks. Changing the wallet keeps
  existing invoices on the old one, as the payout address does. A quantity link is not an invoice (2026-10-14): each
  payer's invoice takes the wallet active at the tap, so a link made before a change pays the new wallet, and the
  link's account view shows the current wallet. A fresh passkey check for a change is bound to the wallet it approves
  and spent once it verifies, even when the change is then refused; every refusal that needs no race (the sidecar's
  checks, unchanged, taken) runs before it. One row per address and chain, across accounts and
  replaced rows: going back to an address the account held before reactivates its row with its minor counter, and an
  address another account holds is refused (`monero_address_taken`), so no minor is ever derived twice.
- Subaddresses: major index 1, minors from 1 upward, one per invoice, never reused, `subaddress_minor` on the invoice;
  `deposit_address` holds the subaddress (the 42-character address columns widen to 106). Lookahead gap guard: a
  receiver's own wallet scans its lookahead (200 minors by default, wallet2 `SUBADDRESS_LOOKAHEAD_MINOR 200`,
  https://github.com/monero-project/monero/blob/release-v0.18/src/wallet/wallet2.cpp) past its highest minor with a
  received output and expands as it finds outputs (`expand_subaddresses`; widening the lookahead does not move that
  base), so a run of unpaid invoices can blind it to the next paid one. wallet2 matches a refresh batch's outputs
  against the subaddress table as it stood when the batch started and only expands it while processing the batch in
  order (`process_parsed_blocks`, wallet2.cpp:3272 and :3354 on release-v0.18, expansion at :2576 and :2663), so two
  payments past the lookahead in one batch can still be missed until a rescan. `set_subaddress_lookahead` expands the
  table at once only from v0.18.4.1 (commit 1da19dac54, PR https://github.com/monero-project/monero/pull/9954,
  merged 2025-07-10; before it the wider lookahead took effect only after the next found output): receivers need
  wallet v0.18.4.1 or newer, and `rescan_bc` when a paid invoice does not show. Per wallet, the gap is the
  allocated minors past the highest minor with an output in a block that a second operator confirmed (not an
  invoice in status `paid`, not a pool row), and the
  limits follow the receiver's confirmed lookahead (`confirmed_lookahead_minor_count`, 200 until confirmed): 50 below
  it (150 of 200) `/activity`'s Needs you shows the exact `set subaddress-lookahead 50:1000` line; at the lookahead
  less one (199 of 200) new XMR invoices are refused (`monero_lookahead_exhausted`) until an output lands or the
  receiver confirms the wider lookahead in the sheet, which sets it to 1000 (warning at 950, refusal at 999).
  Refusing beats burning.
- Scanner: a Rust sidecar, `xmr-scanner/`, on monero-oxide's `monero-wallet` (MIT; Cypher Stack audit May 2025,
  https://github.com/monero-oxide/monero-oxide/tree/main/audits), in its own container beside the watcher. AcceptXMR
  (MIT/Apache, https://github.com/busyboredom/acceptxmr) is the reference for the scanner loop and caches only: it
  holds one view pair per gateway, one node, its own store, and crashes on current public nodes. The sidecar follows
  the tip from the chain's node list, one connection per node and sequential (public nodes allow 3 per IP,
  `DEFAULT_RPC_MAX_CONNECTIONS_PER_PUBLIC_IP` in cryptonote_config.h), fetches each block once as pruned data (mainnet:
  719 blocks a day, blocks about 95 kB, about 35 MB a day in all) and the pool through `get_transaction_pool_hashes`
  plus `get_transactions` (`/get_transaction_pool` is off in restricted mode since monerod v0.18.5.3, 2026-10-06,
  https://github.com/monero-project/monero/releases/tag/v0.18.5.3), checks the outputs of every active view pair and
  of every replaced one that still has an open invoice (view tag first, then the subaddress table, minors up to the
  next unused plus 500: the cost does not grow with the table, 162 µs per output per account measured), decodes amounts and verifies commitments, and writes found outputs to
  Postgres (`monero_payments`: txid, output public key, piconero, height, block hash, first seen, credited,
  cross-check state). Restricted RPC chunks: 100 transactions and 1,000 headers a call. The network (mainnet or
  stagenet) is explicit in every derivation and address (AcceptXMR shipped mainnet prefixes on stagenet).
- Trust: wallets don't verify proof of work (wallet2 has no such check), so a remote node can lie or withhold. Before
  a payment is credited, and again at its 10th confirmation, it is checked on a second node of another operator
  (`nodes = [{ url, operator }]`; count operators, not hosts: node, node2 and node3.monerodevs.org are one): the tx is
  in that block's `tx_hashes`, the block hash at that height matches, and the amount re-decoded from that node's copy
  matches. Every other operator's nodes are asked until one confirms: confirmed if any does, a mismatch only when one
  disagrees and none confirms, and a mismatch is asked again each tick until final (2026-10-14 audit). The final
  check also needs the block `confirmations_final` deep on that operator's own tip. A mismatch holds the invoice at
  `confirming`, records `invoice.payment_held` on the account and an `operator.alert` (section 8.2) for the operator;
  nothing credits, from that row or any other. With fewer than two healthy operators `reconcile` warns and credits
  wait.
- Readings, mapped onto section 4 with the state machine unchanged: seen (a pool transaction paying the subaddress;
  never credited, cleared by `double_spend_seen`; the payer page says "Seen"), `latest` (in a block, 1 confirmation
  or more) drives `confirming`, and final (`confirmations_final`, mainnet 10, the consensus spend lock,
  https://web.getmonero.org/resources/moneropedia/unlocktime.html; stagenet 2 for the e2e) is where money moves: gross
  received at final is the sum of credited outputs with that many confirmations. Credit comes from block data only,
  never from the pool (AcceptXMR reported paid, then 0, then paid again as a tx moved from the pool into a block).
  Reorgs: a credited block's hash is re-read each tick until final; a vanished block uncredits (even when the
  transaction is mined again before the watcher looks: a credited row that is no longer creditable is uncredited)
  and puts the row back in the pool, so the invoice waits instead of expiring. The scan cursor is
  `monero_scan_cursors` (last scanned height and hash per chain), read on start (else the tip), written each tick and
  rewound with a vanished block, so a sidecar restart rescans the blocks it missed. The cursor never passes a block a
  wallet failed to scan, nor any block while a watched wallet's key does not open (it holds; `/healthz` counts them). Burning bug: outputs
  are deduplicated by output public key; outputs with `unlock_time != 0` are ignored.
- Split between the sidecar and the API (built 2026-10-14): the sidecar writes `monero_payments` rows (pool, block,
  both cross-check states) and nothing else; the watcher's Monero loop (`chain_watcher/monero_service.py`) reads the
  tip from the sidecar's `GET /healthz` (no tip, or no completed sidecar tick within 3 poll intervals: it logs and
  writes no heartbeat; final counting reads `confirmed_tip_height`, the lower of the sidecar's tip and a second
  operator's, so one node's tip never makes money final) and owns `credited_at` and
  the invoice: an output in a block whose first cross-check is `confirmed` is credited and the invoice goes
  `confirming` (`invoice.payment_detected`); at `confirmations_final` with the final cross-check `confirmed` it counts
  toward gross received, through the same paid, underpaid and overpaid decisions as EVM, valued at the price when the
  output was first seen (the pool sighting, else its block's timestamp, so a scan catching up after an outage does
  not value it later); a credited output that is no longer creditable is uncredited and the invoice drops back to
  `awaiting_payment`. A pool row records `invoice.seen` once per transaction. Expiry, and the close of an underpaid
  invoice, wait while a row is in the pool or in a block and not yet counted at final, so a top-up in flight is
  never dropped. A row that left the pool unmined still counts as in it (waiting, and "Seen" on the payer page) for
  `MONERO_POOL_DEPARTURE_GRACE_SECONDS` (600 s) after it left, by its `updated_at`, and the sidecar marks a row out
  of the pool only in a tick whose block scan reached the tip the pool's node reported, so a payment mined between a
  scan and a pool read, or read from a node that lacks it, neither expires its invoice nor frees a quantity link's
  slot (2026-10-14 audit). While a watched wallet is unusable (its sealed key does not open) the sidecar holds its
  scan, and the watcher credits but neither expires nor closes the chain's invoices and writes no heartbeat, so the
  API's `watcher_not_ticking` fires after 120 s; the tick also records an `operator.alert`
  `monero_wallets_unusable`. A confirmed tip more than 3 blocks under the tip, or unknown, for over 3 poll intervals
  is an `operator.alert` `monero_confirmed_tip_lagging` (the sidecar asks other operators' nodes in turn until one
  is within a block of its tip, else takes the highest, so one stalled node does not freeze finality). The invoice's
  `latest_reading_block_number` is the confirmed tip, so the payer page's "n of 10" and "I paid" count from the tip
  final counting reads. A late row whose block left the chain is uncredited; the invoice keeps `late_payment`, as on
  EVM. A resumed scanner restores a pool carrier block even before a new block arrives. Cancellation refuses a
  Monero payment in the pool, within the departure grace, or in a block, so it keeps a quantity slot held.
  Late payments: the sidecar keeps watching a replaced wallet while one of its invoices closed within
  `recent_deposit_window_seconds` (default 86400), and an output reaching an invoice closed within that window is
  credited as a row and marks it `late_payment` with `invoice.late_payment`, as on EVM; nothing is swept. The watcher container's healthcheck passes while a
  Monero tick completed within 3 poll intervals; `reconcile --chain <monero chain>` reads the sidecar's health
  (`monero_scanner`: sidecar unreadable, fewer than 2 healthy operators, watcher silent past 3 poll intervals,
  unusable wallets, a lagging confirmed tip). `deploy/deploy.sh` builds and starts `xmr-scanner` (the `monero`
  profile), before the watcher, whenever the deploy env file sets a non-empty `PAY_MONERO_SIDECAR_URL`.
  Subaddresses are derived by the sidecar (`POST /v1/subaddresses`); with `PAY_MONERO_SIDECAR_URL` empty every XMR
  invoice and XMR link is refused with `monero_sidecar_unavailable` (503), the watcher leaves Monero chains idle (one
  log line at start, no loop), its healthcheck skips them and `reconcile` warns "no sidecar configured".
- Money rules on this kind: no sweeps, no float, no refund path at all (ring signatures hide the sender, nothing can
  go "back"), so overpay and underpay are the seller's to settle, as on a non-operator receiver's EVM invoice today
  (`refund_offer` says `receiver_settles`); `minimum_refund_usd` only splits `overpaid` from `paid`, as on EVM; stray
  money cannot exist (one coin). With no minimum sweep, the 1 USD invoice minimum also holds for an XMR-priced amount,
  valued at the live XMR/USD price when the invoice is made (2026-10-14). A paid or overpaid XMR invoice closes at once, and accepting an underpaid one closes
  it with no sweep. Quote: XMR/USD from Chainlink on Polygon
  (`0xBE6FB0AB6302B693368D0E9001fAF77ecc6571db`, feed category low, from Chainlink's feed directory) through
  `polygon`'s own RPC client when it is enabled (`price_chain_key`; never a fork, whose feeds stand still), else
  `price_rpc_urls` with `price_chain_id = 137`, as Amoy reads mainnet feeds; `quote_decimals = 6`; amounts in piconero
  (12 decimals) as `numeric(78,0)`.
- Payer page: coin fixed (XMR), network Monero, the one-tap confirm stays ("Pay 0.031250 XMR on Monero"); the QR
  holds `monero:<subaddress>?tx_amount=<xmr>&tx_description=<note>`
  (https://github.com/monero-project/monero/wiki/URI-Formatting), an address-only QR a tap away; "Open in wallet" on
  the URI and no "Pay with wallet" (no injected wallet); then "Seen" within about 10 to 20 s at 0-conf, "Confirming
  n of 10" over about 20 minutes on a 10-step ring, and "Paid". "I paid" without a txid rereads the invoice's
  `monero_payments` rows; with a 64-hex txid it asks the sidecar (`POST /v1/transaction-checks`, which fetches the
  transaction and decodes its outputs with the view key, no tx key needed) and answers counted only when a credited
  row for that txid is final. When the transaction pays the invoice and no row holds it yet, the sidecar writes the
  row the scan loop would have written (pool sighting, or block row after the same cross-check), so the watcher
  credits it on its next tick; the watcher's credit is still the only credit. The outcome is `unavailable` (503,
  retry) whenever nothing was proven (no node answered, out of time, a wallet key that does not open) and `failed`
  only for a cross-check mismatch or a flagged double spend. No refund button, no
  stray-money alert. As built (2026-10-14): "Open in wallet" is the one primary on every device (Monero's desktop
  wallets take `monero:` links too), the subaddress shows as its first and last six characters and copies whole, and
  "Seen" and "Confirming n of 10" are one screen (the ring fills n of 10), shown too for a top-up on an underpaid
  invoice while it is in the pool or short of final, so the payer does not send twice. Underpaid says "send the rest,
  or ask the seller to accept what arrived"; an overpay "Ask the seller for the extra". The "I've paid" field reads
  "Transaction id" with an example placeholder.
- Node list: curated in chains.toml from https://monero.fail and the official list
  (https://docs.getmonero.org/infrastructure/networks/), an operator per entry, two or more operators on mainnet and
  on stagenet, failover like EVM `rpc_urls`, health in `reconcile`. Stagenet nodes that answered on 2026-10-14:
  node3.monerodevs.org:38089, stagenet.xmr.kernal.eu:38089 (https), stagenet.xmr-tw.org:38081,
  xmr-lux.boldsuck.org:38081. Privacy of our own traffic: bulk block fetches reveal nothing; txid lookups
  (cross-checks, "I paid") reveal interest only in txids we already hold.
- Ruled out: monero-wallet-rpc (one wallet a process, 0.2 to 0.6 GB and about 23 MB a day each, every one fetching
  the same blocks: 1,000 accounts would pull 23 GB a day through a 3-connection cap); monero-lws (needs monerod's
  ZMQ, which public nodes don't expose); the archived `monero` PyPI package (2022, crashes on a real mainnet extra
  field); own Python crypto (works in 120 lines, but hand-rolled is the opposite of the point).
- Not in version 1: Tor to `.onion` nodes, integrated addresses (superseded by subaddresses), payment proofs, mixed
  EVM-and-XMR invoices (an XMR invoice is its own chain; "the payer picks the network" across chains is a separate
  design), an own node.
- Later fork: Carrot and FCMP++ keep existing addresses working
  (https://ccs.getmonero.org/proposals/cypherstack-carrot-spec-review.html) and land in monero-oxide; the sidecar's
  library is upgraded before the fork activates.
- Verification: a stagenet e2e (the compose stack plus the sidecar, coins from https://stagenet-faucet.xmr-tw.org/,
  0.1 XMR a request, `confirmations_final = 2`), a security review of the sidecar and the credit path, then one live
  mainnet invoice paid by hand (about 0.01 XMR, inside the 5 USD cap).

## 15. Arbitrum, and the payer picks the network (2026-10-14)

Arbitrum One next (2026-10-14), and the other chains that share the 0x payout address; Ethereum L1 is left
out (a sweep there costs more than a 1 to 5 USD invoice earns).

- Arbitrum One (chain id 42161) is an EVM chain entry as section 1 foresaw: `[chains.arbitrum]` with ETH (native,
  18), native USDC `0xaf88d065e77c8cC2239327C5EDb3A432268e5831`
  (https://docs.arbitrum.io/arbitrum-bridge/usdc-arbitrum-one) and USDT0 `0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9`
  first; ARB, WBTC, LINK and DAI once their feeds pass the same checks (live, fast heartbeat, section 1). Chainlink proxies on Arbitrum
  from Chainlink's feed directory (2026-10-14; heartbeats verified on chain before they ship): ETH/USD
  `0x639Fe6ab55C921f74e7fac1ee960C0B6293ba612`, USDC/USD `0x50834F3163758fcC1Df9973b6e91f0F0F0434aD3`, USDT/USD
  `0x3f3f5dF88dC9F13eac63DF89EC16ef6e7E25DdE7`. Rollup rule from section 1: the Chainlink client also reads the L2
  Sequencer Uptime Feed `0xFdB631F5EE196F0ed6FAa767959853A9F217697D` and refuses a quote while it answers 1 (down) or
  for one hour after `startedAt` once it is back (https://docs.chain.link/data-feeds/l2-sequencer-feeds); a chain
  names its uptime feed in chains.toml (`sequencer_uptime_feed`, empty on Polygon).
- Finality stays the `finalized` tag (section 1): on Arbitrum that is the batch posted to Ethereum and
  Ethereum-finalized, about 13 to 15 minutes ("approximate hard-finality latency: time to post the batch, plus
  Ethereum finality", https://docs.arbitrum.io/how-arbitrum-works/reference/finality-and-reorgs), so the payer's
  confirming screen says so (about 17 minutes, from the chain's `expected_finality_seconds`, never a
  hard-coded minute). `safe` (batch posted, L1 safe) is faster and reorgs only with a deep L1 reorg; it is not
  used.
- Block-count thresholds become time: Arbitrum blocks are about 0.25 s and `finalized` advances in jumps, so
  `WATCHER_BEHIND_MAXIMUM_BLOCKS` (150, five Polygon minutes), `NATIVE_ATTRIBUTION_MAXIMUM_BLOCKS` (200), the
  operator-alert tick counts and every other "about N blocks" constant become seconds per chain
  (`seconds_per_block` in chains.toml, Polygon 2, Arbitrum 0.25), with the same meaning on Polygon.
- No mempool: the sequencer orders first come, first served, and the gas price does not order
  (https://docs.arbitrum.io/learn-more/faq); transactions are included within seconds or refused. The settlements
  runner's fee bumps and replacements (section 5) are kept as they are (a replacement by nonce with a higher fee is
  still accepted) but expected to be idle there; the review before Arbitrum ships checks that a refused or
  dropped send on Arbitrum ends in the same states as on Polygon and never double-sends. Gas is paid in ETH; an
  L1 data fee rides on each transaction (anvil forks do not emulate it, so fork e2e gas figures are indicative only).
- RPC: public endpoints in failover order, measured as in section 1's table before they ship (log range, history,
  rate limits): `arb1.arbitrum.io/rpc` (official), `arbitrum-one-rpc.publicnode.com`, `arbitrum.drpc.org`; the
  QuickNode and 429/503 rules of section 1 apply unchanged.
- Factory: deployed on Arbitrum at `0x91749BD4e0Db4050D4F0B07f0E8f929Da552a661` with the refund key on
  2026-10-14; its address is in `factory_address`. Treasury Safe: `0x9cf3d24C1CbbcBA45AfB2c2092BcF94F79EBDB0d`,
  deployed with the same owner and threshold 1 as the Polygon Safe. Its allowance module is enabled for the
  refund key and both sweepers; the watcher refills their floats within the daily caps below. Safe 1.4.1 and the Allowance
  Module v0.1.1 at `0xAA46724893dedD72658219405185Fb0Fc91e091C`, the same as on Polygon
  (https://github.com/safe-global/safe-modules-deployments), so `treasury create-safe --chain arbitrum` and
  `execute-setup` work as they do there. Top-ups per key role (section 5.1): refund key ETH low 0.0015, target
  0.003, daily 0.005; USDC and USDT 8 / 10 / 10 as on Polygon; sweepers ETH 0.0005 / 0.001 / 0.002.
- The payer picks the network. An invoice offers coins across EVM chains, stays
  `awaiting_asset` until the payer's one tap picks a (chain, asset) pair, and derives its deposit address then (the
  salt is HMAC of the invoice id, chain-independent, section 4; the address is that chain's factory plus the salt and
  the destination). Creation takes `chains` (default: every enabled EVM chain with a factory) beside `assets`;
  signed-out `/` offers them all; the coin rows read "USDC · Arbitrum" and "USDC · Polygon" grouped by coin, each with
  its own estimate, and the forward button already names both ("Pay 5.00 USDC on Arbitrum"). Quantity links the
  same. A one-coin, one-chain invoice still needs the tap (the confirmation rule, 2026-10-12). `deposit_address`
  stays null until the tap; legacy `chain_key` names the first offered chain until the tap, then the selected one.
  `assets_by_chain` preserves the maker's exact ticks on each network; a coin-priced invoice offers one chain.
  The per-IP open-invoice cap is
  unchanged. Monero stays its own chain kind (a view key is needed, so account-only); offering "XMR · Monero" as a
  row on an account's mixed invoice is possible after version 1, since the subaddress is allocated at the tap too.
- Verification: the fork e2e against an anvil fork of Arbitrum One (the same scenarios, gas figures indicative),
  a review of settlements on a no-mempool chain, then one live mainnet run paid from the Arbitrum float.
  `e2e/mixed_network_flow.py` checks both independent VMs and the shared invoice and quantity-link flows.
  `e2e/arbitrum_sequencer_flow.py` checks outages and recovery against a healthy fallback.
  `e2e/run_arbitrum_live_flow.sh` pays ETH, USDT0 and USDC on production, verifies sweeps and the excess refund,
  and returns the payer's leftovers to the float. Its overpaid invoice is made with the same operator account token
  file as the Polygon live run (section 12), with the same rule on which account mints it; without it the overpayment
  is skipped.

## 16. Testnet sandbox (2026-10-21)

Developers could only test with real 1 USD invoices. The sandbox is a second instance of the same app on testnets at
`https://pay-sandbox.7331.org`, so an integration (API tokens, invoices, webhooks, the payer page) can be built with
free test coins.

- One codebase, one setting: `PAY_SANDBOX=true` makes the API mark every page's `index.html` with
  `<meta name="pay-deployment" content="sandbox">` (app/payments/pages/router.py), and the app then shows the `sandboxBanner`
  label (testnet money only) at the top of every page (frontend `SandboxBanner`, in `PageColumn` and on `/docs`), with no
  extra request and no inline script (the CSP allows none). On the sandbox the docs' examples call the sandbox origin;
  `/docs` has a Sandbox section (base URL, faucets, what is separate).
- Its own deployment: Compose project `pay-7331-sandbox` (deploy/docker-compose.sandbox.yml, containers
  `pay-7331-sandbox-*`, image `pay-7331-backend:sandbox`, its own Postgres volume, Redis and networks), settings in
  the sandbox's own env file (mode 600, outside the checkout), started by `deploy/deploy_sandbox.sh` (refuses an env
  file without `PAY_SANDBOX=true`); Caddy block `pay-sandbox.7331.org` (reference copy deploy/Caddyfile.pay-sandbox),
  behind the same proxy and TLS setup as production. No
  WAL archive and no backups: testnet data, and the docs say so. Accounts, passkeys (the RP id is the sandbox host),
  API tokens, webhook secrets and invoices are separate from production.
- Chains: `polygon_amoy` (POL, Circle test USDC `0x41E94Eb019C0762f9Bfcf9Fb1E58725BfB0e7582`) and `arbitrum_sepolia`
  (chain id 421614; ETH, Circle test USDC `0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d`; both from
  https://developers.circle.com/stablecoins/usdc-contract-addresses, `symbol()` "USDC" and 6 decimals on chain
  2026-10-21). Arbitrum Sepolia endpoints: the official `sepolia-rollup.arbitrum.io/rpc`
  (https://docs.arbitrum.io/for-devs/dev-tools-and-resources/chain-info), publicnode, dRPC. Its `finalized` trailed
  `latest` by about 1,240 s on 2026-10-21, so `expected_finality_seconds = 1260`.
- Pricing on testnets: both read mainnet Chainlink feeds over mainnet endpoints (`price_rpc_urls`), prices only, as
  Amoy already did. Chainlink's Arbitrum Sepolia directory
  (https://reference-data-directory.vercel.app/feeds-ethereum-testnet-sepolia-arbitrum-1.json, from
  https://docs.chain.link/data-feeds/price-feeds/addresses, 2026-10-21) has ETH/USD (120 s heartbeat), USDC/USD
  (86,400 s) and USDT/USD, but no EUR/USD and no L2 Sequencer Uptime Feed; Amoy has no POL/USD. So testnet feeds
  cannot price every coin and EUR, and the sandbox quotes with the same feeds production uses, including Arbitrum
  One's sequencer uptime feed on `arbitrum_sepolia`. A 5 USD invoice asks for 5 test USDC or 5 USD of test POL/ETH.
- Keys: one fresh testnet-only refund key, the same on both testnets (it also sweeps; no sweeper keys, no treasury
  Safe: no Allowance Module on either testnet), only in the sandbox env file. `PAY_DEFAULT_DESTINATION_ADDRESS` is
  empty, so the sandbox pays no float refunds: every receiver settles excess with its payer, as any non-operator
  receiver does on production. No ip-api key: sign-up screening is off on the sandbox.
- Factories: `deploy/deploy_sandbox_factories.sh` deploys the factory with the refund key on each testnet that has
  none (forge script, key through `DEPLOYER_PRIVATE_KEY` in the environment, never a command line), writes
  `PAY_FACTORY_ADDRESS_<CHAIN>` into the sandbox env file and redeploys. Until a chain has a factory the watcher skips
  it and invoice creation there answers `factory_not_deployed`. The refund key needs testnet gas first, from the
  official faucets (each needs a captcha or a login, so it is claimed by hand): Amoy POL from
  https://faucet.polygon.technology/, Arbitrum Sepolia ETH from a faucet Arbitrum's chain info page lists; test USDC
  for payers from https://faucet.circle.com/ (20 USDC a request, every 2 hours per address and network).
  The sandbox refund key is `0x2eA4f83cAF999D8eeD184E96D1C80485Dbd5aF8a` (made 2026-10-21); its nonce-0 factory will
  be `0x2380880BC5814e98af240BD68422200e97864B7E` on both testnets. A dry run on 2026-10-21 put the deploy at about
  1.16M gas: 0.035 POL on Amoy (30 gwei) and 0.00005 ETH on Arbitrum Sepolia; a sweep is about 0.003 POL or 0.000004
  ETH, so 0.5 POL and 0.01 ETH last a long time.
- Verification: the sandbox itself, from outside: `/docs` and `/api/v1/chains` answer from the public internet, a sign-up
  through the public host, and an invoice made on a testnet once a factory is deployed.

## 17. Static deposit addresses (2026-10-21)

An account makes a permanent deposit address for one of its customers, like Cryptomus static wallets
(https://doc.cryptomus.com/merchant-api/payments/creating-static): any amount, any of the address's coins, no expiry.
Every final payment into it is credited at its USD value, swept to the destination and told to the account
(`/activity`, its webhook). Domain `payments/static_deposit_addresses`, beside `payment_links` above `invoices`.

- Identity: `static_deposit_addresses` row per (account, chain, `customer_reference`), unique; `customer_reference`
  is the merchant's own string, 1-128 of `[A-Za-z0-9_.:@+-]`. Public id `sda_` plus 22 base58. EVM chains with a
  factory only (a Monero chain is 400 `chain_kind_not_supported`). The coins are fixed at creation: the request's
  `assets` or else the account's ticked coins on that chain. The destination is the account's payout address at
  creation, checked as an invoice's (section 3; `allow_contract_destination` as there) and baked into the address:
  a later payout address change does not move an existing static address, as with invoices (section 7).
  Repeating the create with the same `customer_reference` and chain returns the address already made, unchanged
  (201, as a repeated `order_id`), under the account's advisory lock so two at once make one. At most
  `PAY_MAXIMUM_STATIC_DEPOSIT_ADDRESSES_PER_ACCOUNT` (1,000) per account (409 `static_deposit_address_limit_reached`):
  every address is scanned for good, so the cap bounds what one account adds to the scan.
- Destination guard: a gateway deposit address (any invoice's or static address's) is refused as an invoice
  destination and as a payout address (sign-up and the payout address change), checked through
  `InvoiceWiring.gateway_deposit_address_check`; money swept into another deposit would be counted twice.
- Salt: HMAC-SHA256 under `PAY_INVOICE_SALT_SEED` of `pay-7331 static deposit address v1`, a zero byte, then the
  account id, chain key and customer reference each as a 2-byte big-endian length and its UTF-8 bytes
  (`static_deposit_addresses/deposit_salts.py`). An invoice's message is its id (`inv_` + base58), so the two
  messages never coincide and neither do the salts; the length prefixes keep (account, chain, reference) triples
  apart. The row stores the salt; the API's `static_deposit_address_create` log line names the id, chain, deposit and
  destination (never the salt, seed or customer reference).
- Money rules: no float refunds ever (there is no amount due, so no excess; a static address is never an
  operator-destination invoice). Money in a configured coin the address does not take is the stray rule for a
  receiver's invoice (section 6: on any other receiver's invoice it is swept to them): never
  credited, recorded as an uncredited payment (`wrong_coin`) with a `static_address.stray_payment` event, and swept
  to the destination with the rest. A token that is not configured is never seen (as for invoices).
- Accounting (watcher, section 5): what a static address ever received in a coin at the finalized block B is its
  balance at B plus every stored `DepositSwept` of it in that coin at or before B (the same gross as section 4); the
  rise over what its payment rows already hold is new money. Each rise becomes payment rows: the coin's `Transfer`
  logs into the address since its last scanned block (one `eth_getLogs` per slice with money, best effort: a refused
  or failed read, a native coin or a contract's internal transfer leaves one row with no sender or hash for the
  rest), each valued in USD at the chain's price when the watcher credits it (Chainlink, a pegged stablecoin at 1
  USD within 0.5% and never above 1 USD, section 4) and rounded half up to the cent. A credited payment records
  `static_address.payment_received`. No price is no verdict: an address whose credited coin has no price is left
  for the next pass whole (nothing recorded, its scanned block kept), never credited at zero (section 6).
  There is no off switch: every final rise is credited and evented. Rows are written under the address row's lock
  (`FOR UPDATE`); a Transfer at or below the address's last scanned block is never attributed again, and the swept
  amounts are summed in SQL. A gross below what the rows hold and a credit waiting for a price are logged once when
  an address enters that state, not every pass.
- Sweeps: once a slice is applied, an address whose finalized balance in some coin is at least that coin's
  `minimum_sweep_amount` and that has no active sweep and is not `sweep_blocked` gets a `static_deposit_sweep`
  operation (settlements; `outgoing_operations.static_deposit_address_id`, no invoice), signed by a sweeper as an
  invoice sweep is. Its outcome commits with its effect (section 5, step 6): `final` records
  `static_address.swept`; `failed` (not a frozen key's, which only asks again) sets `sweep_blocked` and records
  `static_address.sweep_failed`, and the money waits for the owner's `sweep-static-deposit-address <id>
  [--skip-token SYMBOL] [--clear-skipped-tokens]`. A skipped token is kept on the address
  (`skipped_sweep_token_symbols`): its later automatic sweeps leave it out and only money in the other coins asks for
  one, so one unsweepable token does not block every later payment; `--clear-skipped-tokens` forgets the list.
- Operator checks (section 5; chain_watcher/static_deposit_reconciliation.py): a static sweep stuck past its deadline
  (`operation_stuck:{operation id}`) and a `sweep_blocked` address (`static_deposit_sweep_failed:{id}`, alert kind
  `sweep_failed`) are in the outgoing operations check, so the watcher alerts them, naming
  `sweep-static-deposit-address`. A finalized balance left on an address with no active sweep, in a coin its sweeps
  move (`static_deposit_unswept:{id}`), and a balance that could not be read (`static_deposit_unreadable:{id}`) are
  `reconcile`-only findings with no alert kind: that check reads every address. A scan tier whose pass ran over its
  interval is a `static_deposit_scan_slow` alert until one of its passes ends in time.
- Scan budget (no unbounded tick): static addresses are not watched every tick and are not in
  the invoices' Transfer filters. They are scanned in two tiers, each a pass in (created_at, id) order reading a slice
  a tick, all configured coins of each address in one Multicall3 `aggregate3` at the finalized block hash, paced to
  end in about half its interval. Hot: an address made or paid within `static_deposit_hot_window_seconds` (86,400, a
  day), a pass every `static_deposit_hot_scan_interval_seconds` (60). Cold: every other address, a pass every
  `static_deposit_cold_scan_interval_seconds` (1,200, 20 minutes; the fork sets 20 s). All three are per chain in
  chains.toml; a payment sets `last_received_at` and so puts its address back in the hot tier. Both tiers share
  `STATIC_DEPOSIT_MAXIMUM_BALANCE_CALLS_PER_TICK` (1,000: two `eth_call`s, 100 addresses with Polygon's 10 coins), the
  hot tier first; while a cold pass is running or due the hot tier gets at most 75% of it, so thousands of hot
  addresses cannot stall the cold pass (at least 25 cold addresses a tick on Polygon, a 30,000-address cold pass within
  about 40 minutes even then). More addresses lengthen a pass, never the tick (30,000 cold addresses with no hot ones:
  about 10 minutes of a 20-minute interval). The passes live in memory; a restarted watcher starts both afresh.
- Attribution reads: only a slice with new token money reads `Transfer` logs, from its oldest address's last scanned
  block, at most two cold intervals or 15 minutes back, whichever is longer (Polygon: 1,200 blocks; Arbitrum: 9,600),
  to the finalized block. Per 100 such addresses, one `eth_getLogs` per 2,000 blocks of that window (Polygon: 1,
  Arbitrum: 5); once an endpoint refuses a range, the rest of that read goes 100 blocks a call (Polygon: 12 per 100
  addresses). One slice's read never makes more than 50 calls and a tick reads two slices, so a tick makes at most
  two `eth_call`s and 100 `eth_getLogs` for static addresses; a read that fails or would need more records its money
  with no sender (credited all the same). On Arbitrum a refusing endpoint would need 96 calls per 100 addresses, past
  that cap, so there such a slice's money has no sender until an endpoint takes 2,000-block ranges again.
- API (session or the account's API token, section 8.3; every route scoped to the account, another account's id is
  404 `static_deposit_address_not_found`): `POST /static-deposit-addresses` (`customer_reference`, `chain` (default
  the first chain `GET /chains` lists), `assets`, `allow_contract_destination`; a public write, and by token it
  shares the token's create budget), `GET /static-deposit-addresses/{id}` (with its newest 100 payments),
  `GET /static-deposit-addresses?customer_reference=&customer_reference_prefix=&chain=&cursor=` (newest first, 50
  a page, `{"static_deposit_addresses": [...], "next_cursor"}`; the prefix matched literally over
  `ix_static_deposit_addresses_account_id_reference_pattern`; both at once is 400 `validation_error`).
- The resource: `{"id", "customer_reference", "chain": {chain object}, "assets": ["USDC", "USDT"],
  "deposit_address", "destination_address", "sweep_blocked",
  "received_usd": "12.00" (credited payments), "payment_count", "last_payment_at", "created_at", "payments": [...]}`;
  `payments` only on `GET /static-deposit-addresses/{id}` (else `[]`). A payment: `{"id", "asset_symbol", "amount"
  (decimal string), "amount_usd" (null when unpriced: uncredited money is not waited for), "asset_price_usd",
  "credited", "uncredited_reason": null | "wrong_coin", "tx_hash", "sender", "block_number",
  "created_at"}`.
- Events (section 8), account events with no invoice: `static_address.payment_received` and
  `static_address.stray_payment` (`data` `{"static_deposit_address": resource without payments, "payment": payment}`),
  `static_address.swept` and `static_address.sweep_failed` (`{"static_deposit_address", "sweep": {"tx_hash",
  "status"}}`). Their type prefix `static_address.` is in `WEBHOOK_EVENT_TYPE_PREFIXES_WITHOUT_INVOICE`
  (account_events/webhook_event_scope.py), so the webhook (section 8.3) sends them and an API token's `GET /events`
  lists them; Discord does not (yet). "Send test event" has a sample of each.
- Verification: the fork e2e scenario `scenario_static_deposit_address` (section 12,
  e2e/static_deposit_address_scenarios.py): created twice, the same address, its salt the HMAC; a USDC payment
  credited once at its USD value, swept to the destination, the webhook delivered and listed by the token's
  `GET /events`; moved to the cold tier, a second payment credited and the address hot again.
  `scenario_merchant_operations` captures the docs' customer address responses and checks the samples.

## 18. Invoice schedules (2026-10-21)

Recurring billing for an account's own customers (a monthly plan, say), push only: a schedule makes a new invoice
each period and the account sends its link to the payer, who pays it like any other. No allowances, no stored payer
authorisations, nothing that moves money the payer did not send; the gateway stays non-custodial (section 3).

- Prior art. Cryptomus' `POST recurrence/create` takes `period` `weekly`, `monthly` or `three_month`, an `order_id`
  and a `url_callback`, and then "payments will be made automatically according to the plan", pulled from the payer's
  Cryptomus wallet (https://doc.cryptomus.com/business/recurring/creating): custodial, which this design is not.
  BTCPay Server's Subscriptions (2.3.0) have a manual mode where "the customer pays each billing period after
  receiving a reminder", with a grace period (https://docs.btcpayserver.org/Subscriptions); its Payment Requests are
  long-lived invoices, not recurring (https://docs.btcpayserver.org/PaymentRequests). NOWPayments' plans repeat by
  day, week, month or year and email each invoice; a plain wallet pays each one by hand
  (https://nowpayments.io/blog/how-to-create-recurring-crypto-payments; its `interval_day` API field is not documented
  in the official sources checked). The manual modes are the model here; the reminder is the account's own (the
  webhook event), since pay keeps no payer emails.
- Domain `payments/invoice_schedules` beside `payment_links` (above `invoices`); table `invoice_schedules`, ids
  `sch_` plus 22 base58. A schedule holds `POST /invoices`' fields for every invoice (amount, currency, the chains and
  coins resolved once at creation, destination, note, metadata, lifetime, accuracy, return, success and callback URLs),
  an `order_id_prefix`, `interval` (`daily`, `weekly`, `monthly`), `starts_at`, and at most one end:
  `ends_after_invoice_count` (invoices made) or `ends_at` (no period starting at or after it). The destination and the
  coins are fixed at creation like a quantity link's (section 7); a change is a new schedule. Made in the browser at a fiat
  amount, its coins become the account's starting ticks, as a fiat invoice's do. The destination is checked on every
  offered chain, a chain whose RPC is down refused rather than skipped, since the runner skips that check. EVM chains
  only (a Monero invoice needs the wallet's next subaddress and lookahead, section 14): `chain_kind_not_supported`.
- Periods are computed from the anchor, never by adding to the last one: period n starts at `starts_at` plus n days or
  n weeks; monthly keeps the anchor's day of the month and time, clamped to the month's last day (31 January bills on
  28 February, 31 March, 30 April; the month's length from `calendar.monthrange`,
  https://docs.python.org/3/library/calendar.html#calendar.monthrange). All in UTC.
- The watcher makes the invoices (`ScheduledInvoiceRunner`, a loop beside the chain loops under the watcher's advisory
  lock, section 2): every 15 s a pass takes at most 20 schedules whose `next_attempt_at` has come (partial index on
  active ones), each in its own transaction: `SELECT ... FOR UPDATE SKIP LOCKED` on the schedule row
  (https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE), then the account's creation lock
  inside `create_invoice`. More due than taken runs the next pass at once. The newest started period is billed:
  periods nobody billed in time (the watcher down for days, a failure lasting past the next start) are skipped and
  logged, never billed late, so a customer never gets a backlog of invoices at once.
- Exactly once per period: the invoice carries `invoice_schedule_id` and `invoice_schedule_period_start`, unique
  together (`ux_invoices_invoice_schedule_id_period_start`), and the runner checks for the period's invoice before
  making one, so a restart, a second runner or a schedule moved back by hand finds it and moves on. Its `order_id` is
  `<order_id_prefix>-<YYYYMMDD>` of the period's start (the schedule id when no prefix), so the merchant's
  `GET /invoices?order_id_prefix=` finds them and `refresh_expired` works per period. An order id the account already
  used for another invoice is a failure (`order_id_taken`), never that invoice adopted. Two live schedules of an
  account can't share a prefix (409 `order_id_prefix_taken`), and a new schedule whose first period's order id an
  invoice of the account already has (a cancelled schedule's, made the same day) is refused with the same code
  rather than losing its first period. A hit on the unique index (another runner was first) is logged and backs off
  like a refusal (`period_already_billed`).
- Each invoice is made through the normal creation (channel `schedule`: no IP, capped per account like an API token's,
  `PAY_MAXIMUM_OPEN_INVOICES_PER_ACCOUNT`), so it sends the usual events, and records `schedule.invoice_created` with
  the invoice as its data in the same transaction; the webhook, the invoice's `callback_url`, a token's `GET /events`
  and Discord get it (it has an `invoice_id`, section 8.3). Default lifetime 12 h, the longest (section 4): the link
  reaches the payer later, through the account's server.
- A refusal (prices down, a factory missing) rolls the period's work back, notes `last_failure_code` and
  `last_failure_at` on the schedule and retries after 60 s, 2, 5, 15, 30 then every 60 minutes. No account event:
  the schedule's view shows it.
- Pause stops new invoices; resume bills from the first period starting at or after now, never one that started while
  paused, and clears `last_failure_code`. Cancel ends it for good; a schedule whose end is reached becomes `completed`. Invoices already made run their
  course. At most `PAY_MAXIMUM_INVOICE_SCHEDULES_PER_ACCOUNT` (100) active or paused schedules per account (409
  `too_many_invoice_schedules`), counted under a per-account advisory lock. A deleted account's schedules (account
  null) are never due.
- Frontend: Options' "Repeat" on `/` and "Repeating invoices" under `/activity`'s Links tab (section 10); `/docs` has "Scheduled
  invoices" with captured example responses (section 12).
