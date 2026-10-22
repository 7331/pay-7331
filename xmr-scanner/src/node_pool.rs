//! A chain's restricted-RPC nodes: one connection per node (monero-simple-request-rpc keeps no pool and serialises
//! requests on a node), used from the chain's one scanner task, so calls are sequential (public nodes allow 3
//! connections per IP, `DEFAULT_RPC_MAX_CONNECTIONS_PER_PUBLIC_IP` in cryptonote_config.h). Failover goes through the
//! list in order, starting at the node that answered last. A node is logged by its index in the list and its operator,
//! never its URL (a URL may carry credentials), and the URL is cut out of every error text before it leaves here.

use std::collections::BTreeSet;
use std::future::Future;
use std::time::Duration;

use monero_interface::PrunedTransactionWithPrunableHash;
use monero_simple_request_rpc::prelude::{
    InterfaceError, MoneroDaemon, ProvidesBlockchain, ProvidesBlockchainMeta, ProvidesScannableBlocks,
    ProvidesTransactions, ScannableBlock,
};
use monero_simple_request_rpc::SimpleRequestTransport;
use monero_wallet::block::Block;
use monero_wallet::transaction::{Pruned, Transaction};
use serde::Deserialize;

use crate::configuration::MoneroNodeConfiguration;

pub type DaemonConnection = MoneroDaemon<SimpleRequestTransport>;

/// The scan loop's per-call limit: a 100-block `get_blocks.bin` round can take a while on a slow public node.
pub const SCANNER_NODE_REQUEST_TIMEOUT: Duration = Duration::from_secs(30);
/// The every-node probe's per-node limit: the transaction checker's 6 s, since a tip read is one small call.
pub const NODE_PROBE_REQUEST_TIMEOUT: Duration = Duration::from_secs(6);
/// Restricted RPC: `get_transactions` takes at most 100 hashes a call (`RESTRICTED_TRANSACTIONS_COUNT` in
/// core_rpc_server.cpp).
pub const RESTRICTED_TRANSACTIONS_PER_CALL: usize = 100;
/// Blocks fetched per `get_blocks.bin` round; well inside the restricted 1,000 headers a call.
pub const BLOCKS_PER_FETCH: u64 = 100;
const POOL_HASHES_RESPONSE_SIZE_LIMIT: usize = 4 * 1024 * 1024;
const TRANSACTION_SIZE_ESTIMATE: usize = 200 * 1024;

struct NodeConnection {
    configuration: MoneroNodeConfiguration,
    daemon: Option<DaemonConnection>,
    last_call_succeeded: Option<bool>,
}

pub struct NodePool {
    chain_key: String,
    nodes: Vec<NodeConnection>,
    preferred_node_index: usize,
    request_timeout: Duration,
}

/// A transaction as `get_transactions` returns it, its hash checked against the requested one.
pub struct FetchedTransaction {
    pub transaction_hash: [u8; 32],
    pub transaction: Transaction<Pruned>,
    pub in_pool: bool,
    /// The block that includes it; `None` while it is in the pool.
    pub block_height: Option<u64>,
    pub double_spend_seen: bool,
}

#[derive(Deserialize)]
struct PoolHashesResponse {
    #[serde(default)]
    tx_hashes: Vec<String>,
}

#[derive(Deserialize)]
struct TransactionsResponse {
    #[serde(default)]
    txs: Vec<TransactionEntry>,
}

#[derive(Deserialize)]
struct TransactionEntry {
    tx_hash: String,
    #[serde(default)]
    pruned_as_hex: String,
    #[serde(default)]
    prunable_hash: String,
    #[serde(default)]
    in_pool: bool,
    #[serde(default)]
    block_height: u64,
    #[serde(default)]
    double_spend_seen: bool,
}

fn decode_hash_hex(hash_hex: &str) -> Option<[u8; 32]> {
    let mut hash = [0u8; 32];
    hex::decode_to_slice(hash_hex, &mut hash).ok()?;
    Some(hash)
}

/// The error text with the node's URL, its host and its credentials (when the URL carries any) cut out: transport
/// errors may quote the URL they were given, or only its host and port. Longer pieces are cut first, so a credentialed
/// authority goes whole before its host part is looked for on its own.
fn redact_node_url(error_text: &str, node_url: &str) -> String {
    let mut redacted_text = error_text.replace(node_url, "<node url>");
    let Some((_, after_scheme)) = node_url.split_once("://") else {
        return redacted_text;
    };
    let authority = after_scheme.split('/').next().unwrap_or("");
    let (user_information, host_and_port) = match authority.rsplit_once('@') {
        Some((user_information, host_and_port)) => (Some(user_information), host_and_port),
        None => (None, authority),
    };
    let host = match host_and_port.rsplit_once(':') {
        // An IPv6 literal keeps its colons inside brackets.
        Some((host, port)) if !host.is_empty() && port.chars().all(|character| character.is_ascii_digit()) => host,
        _ => host_and_port,
    };
    for (piece, replacement) in [
        (authority, "<node>"),
        (host_and_port, "<node>"),
        (host, "<node>"),
        (user_information.unwrap_or(""), "<credentials>"),
    ] {
        if !piece.is_empty() {
            redacted_text = redacted_text.replace(piece, replacement);
        }
    }
    redacted_text
}

impl NodePool {
    pub fn new(chain_key: &str, node_configurations: &[MoneroNodeConfiguration], request_timeout: Duration) -> Self {
        Self {
            chain_key: chain_key.to_owned(),
            nodes: node_configurations
                .iter()
                .cloned()
                .map(|configuration| NodeConnection {
                    configuration,
                    daemon: None,
                    last_call_succeeded: None,
                })
                .collect(),
            preferred_node_index: 0,
            request_timeout,
        }
    }

    /// Drops every connection, so the next call on each node reconnects (after a call was abandoned midway).
    pub fn drop_connections(&mut self) {
        for node in &mut self.nodes {
            node.daemon = None;
        }
    }

    pub fn node_count(&self) -> usize {
        self.nodes.len()
    }

    pub fn node_operator(&self, node_index: usize) -> &str {
        &self.nodes[node_index].configuration.operator
    }

    pub fn healthy_operator_count(&self) -> usize {
        self.nodes
            .iter()
            .filter(|node| node.last_call_succeeded == Some(true))
            .map(|node| node.configuration.operator.as_str())
            .collect::<BTreeSet<_>>()
            .len()
    }

    /// Every node in failover order: the preferred node (the one that answered last), then nodes whose last call
    /// succeeded, then the rest, each group in list order.
    pub fn node_indexes_in_failover_order(&self) -> Vec<usize> {
        let node_count = self.nodes.len();
        let mut node_indexes: Vec<usize> = (0..node_count)
            .map(|offset| (self.preferred_node_index + offset) % node_count)
            .collect();
        // Stable: the preferred node stays first among the healthy ones.
        node_indexes.sort_by_key(|&node_index| {
            node_index != self.preferred_node_index && self.nodes[node_index].last_call_succeeded != Some(true)
        });
        node_indexes
    }

    /// Makes `node_index` the first node the next failover tries.
    pub fn prefer_node(&mut self, node_index: usize) {
        self.preferred_node_index = node_index;
    }

    /// Nodes of operators other than `excluded_operator`, nodes whose last call succeeded first.
    pub fn node_indexes_of_other_operators(&self, excluded_operator: &str) -> Vec<usize> {
        let mut node_indexes: Vec<usize> = (0..self.nodes.len())
            .filter(|&node_index| self.nodes[node_index].configuration.operator != excluded_operator)
            .collect();
        node_indexes.sort_by_key(|&node_index| self.nodes[node_index].last_call_succeeded != Some(true));
        node_indexes
    }

    async fn connect_node(&mut self, node_index: usize) -> Result<DaemonConnection, InterfaceError> {
        if let Some(daemon) = &self.nodes[node_index].daemon {
            return Ok(daemon.clone());
        }
        let node_url = self.nodes[node_index].configuration.url.clone();
        let daemon = SimpleRequestTransport::with_custom_timeout(node_url, self.request_timeout).await?;
        self.nodes[node_index].daemon = Some(daemon.clone());
        Ok(daemon)
    }

    /// One call on one node; a failure drops the connection so the next call reconnects. The error text never holds
    /// the node's URL.
    pub async fn call_on_node<Output, Call, CallFuture>(
        &mut self,
        node_index: usize,
        call: Call,
    ) -> Result<Output, String>
    where
        Call: FnOnce(DaemonConnection) -> CallFuture,
        CallFuture: Future<Output = Result<Output, String>>,
    {
        let outcome = match self.connect_node(node_index).await {
            Ok(daemon) => call(daemon).await,
            Err(connect_error) => Err(format!("connect: {connect_error}")),
        };
        let node = &mut self.nodes[node_index];
        let outcome = outcome.map_err(|call_error| redact_node_url(&call_error, &node.configuration.url));
        match &outcome {
            Ok(_) => node.last_call_succeeded = Some(true),
            Err(call_error) => {
                node.last_call_succeeded = Some(false);
                node.daemon = None;
                tracing::warn!(
                    event = "monero_node_call_failed",
                    chain_key = %self.chain_key,
                    node_index,
                    operator = %node.configuration.operator,
                    error = %call_error,
                );
            }
        }
        outcome
    }

    /// The call on each node in failover order until one answers; the index of the node that answered.
    pub async fn call_with_failover<Output, Call, CallFuture>(&mut self, call: Call) -> Result<(Output, usize), String>
    where
        Call: Fn(DaemonConnection) -> CallFuture,
        CallFuture: Future<Output = Result<Output, String>>,
    {
        let mut last_error = "no nodes configured".to_owned();
        for node_index in self.node_indexes_in_failover_order() {
            match self.call_on_node(node_index, &call).await {
                Ok(output) => {
                    self.preferred_node_index = node_index;
                    return Ok((output, node_index));
                }
                Err(call_error) => last_error = call_error,
            }
        }
        Err(format!("every node failed, last: {last_error}"))
    }

    /// The tip as a node of an operator other than `excluded_operator` sees it, and that operator: nodes whose last
    /// call succeeded first, asked in turn until one reports at least `tip_height - 1` (one block of propagation
    /// delay); when none does, the highest tip any of them reported. A node far behind (monerod's `get_height`
    /// answers whether or not it is synced) is then passed over as long as another operator's node keeps up. `None`
    /// when no node of another operator answers.
    pub async fn fetch_tip_from_another_operator(
        &mut self,
        excluded_operator: &str,
        tip_height: u64,
    ) -> Option<(u64, String)> {
        let mut highest_other_tip: Option<(u64, String)> = None;
        for node_index in self.node_indexes_of_other_operators(excluded_operator) {
            let Ok(other_tip_height) = self.call_on_node(node_index, fetch_tip_height).await else {
                continue;
            };
            let other_operator = self.node_operator(node_index).to_owned();
            if other_tip_height.saturating_add(1) >= tip_height {
                return Some((other_tip_height, other_operator));
            }
            tracing::warn!(
                event = "monero_node_tip_behind",
                chain_key = %self.chain_key,
                node_index,
                operator = %other_operator,
                tip_height,
                other_tip_height,
            );
            if highest_other_tip
                .as_ref()
                .is_none_or(|(highest_tip_height, _)| other_tip_height > *highest_tip_height)
            {
                highest_other_tip = Some((other_tip_height, other_operator));
            }
        }
        highest_other_tip
    }

    /// Takes a probe's outcome per node (`probe_node_tips`), so failover order and `/healthz` count what answered.
    pub fn apply_probe_outcomes(&mut self, node_answered: &[bool]) {
        for (node, answered) in self.nodes.iter_mut().zip(node_answered) {
            node.last_call_succeeded = Some(*answered);
        }
    }
}

/// Asks every node for its tip at once, each on a fresh connection of its own that is dropped afterwards, so the probe
/// never waits on the scan loop's connections and holds a node's third connection only for the moment it asks (public
/// nodes allow 3 per IP). Each node gets `NODE_PROBE_REQUEST_TIMEOUT`, the probe as a whole `time_limit`; a node that
/// did not answer within it counts as failed. One outcome per node, in list order.
pub async fn probe_node_tips(
    chain_key: &str,
    node_configurations: &[MoneroNodeConfiguration],
    time_limit: Duration,
) -> Vec<bool> {
    let mut probes = tokio::task::JoinSet::new();
    for (node_index, configuration) in node_configurations.iter().cloned().enumerate() {
        probes.spawn(async move {
            let probe = async {
                let daemon =
                    SimpleRequestTransport::with_custom_timeout(configuration.url.clone(), NODE_PROBE_REQUEST_TIMEOUT)
                        .await
                        .map_err(|connect_error| format!("connect: {connect_error}"))?;
                fetch_tip_height(daemon).await
            };
            let outcome = match tokio::time::timeout(time_limit, probe).await {
                Ok(outcome) => outcome.map_err(|probe_error| redact_node_url(&probe_error, &configuration.url)),
                Err(_) => Err("no answer within the probe's time limit".to_owned()),
            };
            (node_index, configuration.operator, outcome)
        });
    }
    let mut node_answered = vec![false; node_configurations.len()];
    while let Some(joined) = probes.join_next().await {
        let Ok((node_index, operator, outcome)) = joined else {
            continue;
        };
        match outcome {
            Ok(_) => node_answered[node_index] = true,
            Err(probe_error) => tracing::warn!(
                event = "monero_node_probe_failed",
                chain_key,
                node_index,
                operator = %operator,
                error = %probe_error,
            ),
        }
    }
    node_answered
}

pub async fn fetch_tip_height(daemon: DaemonConnection) -> Result<u64, String> {
    daemon
        .latest_block_number()
        .await
        .map(|number| number as u64)
        .map_err(|error| error.to_string())
}

/// Blocks `first_height..=last_height` as pruned data through `get_blocks.bin` (one fetch per block), validated by
/// monero-interface: contiguous, the requested numbers, and transactions that hash to the block's list.
pub async fn fetch_scannable_blocks(
    daemon: DaemonConnection,
    first_height: u64,
    last_height: u64,
) -> Result<Vec<ScannableBlock>, String> {
    ProvidesScannableBlocks::contiguous_scannable_blocks(&daemon, (first_height as usize)..=(last_height as usize))
        .await
        .map_err(|error| error.to_string())
}

pub async fn fetch_block_hash_by_height(daemon: DaemonConnection, height: u64) -> Result<[u8; 32], String> {
    ProvidesBlockchain::block_hash(&daemon, height as usize)
        .await
        .map_err(|error| error.to_string())
}

/// The block with this hash (checked to hash to it): header, miner transaction and the list of transaction hashes.
pub async fn fetch_block_by_hash(daemon: DaemonConnection, block_hash: [u8; 32]) -> Result<Block, String> {
    ProvidesBlockchain::block(&daemon, block_hash)
        .await
        .map_err(|error| error.to_string())
}

/// Pruned transactions, each checked to hash to its requested hash.
pub async fn fetch_pruned_transactions(
    daemon: DaemonConnection,
    transaction_hashes: Vec<[u8; 32]>,
) -> Result<Vec<Transaction<Pruned>>, String> {
    ProvidesTransactions::pruned_transactions(&daemon, &transaction_hashes)
        .await
        .map_err(|error| error.to_string())
}

/// `get_transaction_pool_hashes` (`/get_transaction_pool` is off in restricted mode since monerod v0.18.5.3).
pub async fn fetch_pool_transaction_hashes(daemon: DaemonConnection) -> Result<Vec<[u8; 32]>, String> {
    let response_text = daemon
        .rpc_call("get_transaction_pool_hashes", None, POOL_HASHES_RESPONSE_SIZE_LIMIT)
        .await
        .map_err(|error| error.to_string())?;
    let response: PoolHashesResponse =
        serde_json::from_str(&response_text).map_err(|_| "get_transaction_pool_hashes: unexpected JSON".to_owned())?;
    response
        .tx_hashes
        .iter()
        .map(|hash_hex| decode_hash_hex(hash_hex).ok_or_else(|| "get_transaction_pool_hashes: bad hash".to_owned()))
        .collect()
}

/// The pool's transaction hashes, then the tip, on the same node: a transaction missing from that pool was mined in
/// a block at or below that tip, or dropped. The scan loop lets a row leave the pool only once its scan reached that
/// tip, so a transaction mined between the scan and the pool read is never taken for one that left unmined.
pub async fn fetch_pool_transaction_hashes_and_tip(daemon: DaemonConnection) -> Result<(Vec<[u8; 32]>, u64), String> {
    let pool_transaction_hashes = fetch_pool_transaction_hashes(daemon.clone()).await?;
    let tip_height = fetch_tip_height(daemon).await?;
    Ok((pool_transaction_hashes, tip_height))
}

/// Pool transactions through `get_transactions` with `prune`, 100 a call, keeping monerod's `double_spend_seen` flag
/// (which monero-daemon-rpc's typed call drops). A transaction that left the pool between the two calls is skipped,
/// and one whose bytes do not hash to its requested hash fails the call.
pub async fn fetch_pool_transactions(
    daemon: DaemonConnection,
    transaction_hashes: Vec<[u8; 32]>,
) -> Result<Vec<FetchedTransaction>, String> {
    let mut fetched_transactions = fetch_transactions_with_status(daemon, transaction_hashes).await?;
    fetched_transactions.retain(|fetched_transaction| fetched_transaction.in_pool);
    Ok(fetched_transactions)
}

/// Transactions through `get_transactions` with `prune`, 100 a call, in the pool or in a block (with its height and
/// monerod's `double_spend_seen`). A hash the node does not know is left out (monerod lists it in `missed_tx`); one
/// whose bytes do not hash to its requested hash fails the call.
pub async fn fetch_transactions_with_status(
    daemon: DaemonConnection,
    transaction_hashes: Vec<[u8; 32]>,
) -> Result<Vec<FetchedTransaction>, String> {
    let mut fetched_transactions = Vec::with_capacity(transaction_hashes.len());
    for hash_chunk in transaction_hashes.chunks(RESTRICTED_TRANSACTIONS_PER_CALL) {
        let hashes_hex: Vec<String> = hash_chunk.iter().map(hex::encode).collect();
        let request = serde_json::json!({ "txs_hashes": hashes_hex, "prune": true }).to_string();
        // `rpc_call` is monero-daemon-rpc's raw route call (pinned at =0.2.0 in Cargo.toml).
        let response_text = daemon
            .rpc_call(
                "get_transactions",
                Some(request),
                hash_chunk.len() * TRANSACTION_SIZE_ESTIMATE,
            )
            .await
            .map_err(|error| error.to_string())?;
        let response: TransactionsResponse =
            serde_json::from_str(&response_text).map_err(|_| "get_transactions: unexpected JSON".to_owned())?;
        for entry in response.txs {
            fetched_transactions.push(read_transaction_entry(entry, hash_chunk)?);
        }
    }
    Ok(fetched_transactions)
}

fn read_transaction_entry(
    entry: TransactionEntry,
    requested_hashes: &[[u8; 32]],
) -> Result<FetchedTransaction, String> {
    let transaction_hash = decode_hash_hex(&entry.tx_hash).ok_or_else(|| "get_transactions: bad tx_hash".to_owned())?;
    if !requested_hashes.contains(&transaction_hash) {
        return Err("get_transactions: answered a transaction that was not asked for".to_owned());
    }
    let pruned_bytes =
        hex::decode(&entry.pruned_as_hex).map_err(|_| "get_transactions: bad pruned_as_hex".to_owned())?;
    let mut pruned_reader = pruned_bytes.as_slice();
    let transaction = Transaction::<Pruned>::read(&mut pruned_reader)
        .map_err(|_| "get_transactions: unreadable pruned transaction".to_owned())?;
    if !pruned_reader.is_empty() {
        return Err("get_transactions: bytes after a pruned transaction".to_owned());
    }
    let prunable_hash = match &transaction {
        Transaction::V1 { .. } => None,
        Transaction::V2 { .. } => Some(
            decode_hash_hex(&entry.prunable_hash).ok_or_else(|| "get_transactions: bad prunable_hash".to_owned())?,
        ),
    };
    let transaction = PrunedTransactionWithPrunableHash::new(transaction, prunable_hash)
        .ok_or_else(|| "get_transactions: prunable hash inconsistent with the version".to_owned())?
        .verify_as_possible(transaction_hash)
        .map_err(|_| "get_transactions: transaction does not hash to its hash".to_owned())?;
    Ok(FetchedTransaction {
        transaction_hash,
        transaction,
        in_pool: entry.in_pool,
        block_height: (!entry.in_pool).then_some(entry.block_height),
        double_spend_seen: entry.double_spend_seen,
    })
}
