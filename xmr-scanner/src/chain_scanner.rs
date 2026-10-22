//! The scan loop of one Monero chain (PLAN.md section 14). Each tick:
//!
//! 1. read the chain's watched wallets (active; replaced with an open invoice or one closed within
//!    `recent_deposit_window_seconds`) and refresh their scanners; a wallet that cannot be scanned is unusable;
//! 2. read the tip from the node list (failover), and the tip a node of another operator sees (the first that keeps
//!    within a block of it, else the highest): the lower of the two is the confirmed tip `/healthz` gives final
//!    counting;
//! 3. unless a watched wallet is unusable (then the cursor holds: no block is passed over for it), fetch each new
//!    block once as pruned data, scan it, and write found outputs with height and hash after the second-operator
//!    cross-check; a block a wallet fails to scan fails the tick before the cursor passes it;
//! 4. read the pool (`get_transaction_pool_hashes`, then `get_transactions` for new hashes), write pool sightings
//!    (`in_pool`, no height) and keep `in_pool` / `double_spend_seen` of unmined rows current (`pool_tracking.rs`);
//! 5. re-read the block hash of every row in a block until `confirmations_final` (a vanished block clears height and
//!    hash), retry pending and disagreeing cross-checks, and run the final cross-check at `confirmations_final`
//!    (`block_settlement.rs`). Runs even when step 3 or 4 failed.
//!
//! Every node is probed for its tip each 60 s on a task of its own (`spawn_node_probe_loop`), never inside a tick.
//!
//! A tick is completed when every step succeeded; `/healthz` publishes the tip, the confirmed tip and the time of the
//! last completed tick only, so a stalled loop shows as stale rather than as a fresh tip.
//!
//! The cursor is `monero_scan_cursors` (migration 0024): on start the loop resumes after the stored block (its hash is
//! the parent the next block must name), else from the tip; it is written after each tick and rewound with a vanished
//! block, so blocks mined while the sidecar was down are scanned when it comes back.

mod block_settlement;
mod pool_tracking;

use std::collections::{BTreeMap, HashSet};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime};

use monero_wallet::block::Block;

use crate::chain_health::{ChainHealthRegistry, ChainHealthSnapshot};
use crate::configuration::MoneroChainConfiguration;
use crate::cross_check::{cross_check_payment_on_another_operator, CrossCheckOutcome, PaymentToCrossCheck};
use crate::node_pool::{
    fetch_block_hash_by_height, fetch_scannable_blocks, fetch_tip_height, probe_node_tips, NodePool, BLOCKS_PER_FETCH,
    NODE_PROBE_REQUEST_TIMEOUT, SCANNER_NODE_REQUEST_TIMEOUT,
};
use crate::payments_store::{NewPaymentRow, PaymentBlock, PaymentsStore};
use crate::view_key_sealing::PrivateViewKeyOpener;
use crate::wallet_scanners::{ReceivedOutput, WalletScanners};

const NODE_PROBE_INTERVAL: Duration = Duration::from_secs(60);
/// The confirmed tip lags when it trails the tip by more than this many blocks (or no other operator answered);
/// `/healthz` publishes since when, so the Python side can alert once that lasts.
const CONFIRMED_TIP_LAG_LIMIT_BLOCKS: u64 = 3;
const REMEMBERED_BLOCK_HASH_COUNT: usize = 200;

pub struct ChainScanner {
    chain: MoneroChainConfiguration,
    node_pool: NodePool,
    payments_store: PaymentsStore,
    wallet_scanners: WalletScanners,
    view_key_opener: Arc<PrivateViewKeyOpener>,
    chain_health_registry: Arc<ChainHealthRegistry>,
    next_height_to_scan: Option<u64>,
    /// Whether the last refresh read any watched wallet row (usable or not).
    has_watched_wallet_rows: bool,
    recent_block_hashes: BTreeMap<u64, [u8; 32]>,
    /// The latest scanned block: its header and miner transaction carry pool transactions through monero-wallet.
    latest_scanned_block: Option<Block>,
    scanned_pool_transaction_hashes: HashSet<[u8; 32]>,
    /// Each node's answer to the last every-node probe, from the probe task (`spawn_node_probe_loop`).
    node_probe_outcomes: tokio::sync::watch::Receiver<Vec<bool>>,
    scanner_started_at: Instant,
    completed_tick: Option<CompletedTick>,
    /// Since when completed ticks have seen the confirmed tip lag (`CONFIRMED_TIP_LAG_LIMIT_BLOCKS`); `None` while it
    /// keeps up.
    confirmed_tip_lagging_since: Option<SystemTime>,
}

/// What the last completed tick saw, as `/healthz` publishes it.
struct CompletedTick {
    tip_height: u64,
    confirmed_tip_height: Option<u64>,
    completed_at: (Instant, SystemTime),
}

/// The block an output was found in, as the scan loop and the cross-check need it.
struct FoundBlock {
    block_height: u64,
    block_hash: [u8; 32],
    block_timestamp: u64,
    block_confirmations: u64,
}

fn cross_check_state_and_operator(outcome: &CrossCheckOutcome) -> (&'static str, Option<String>) {
    match outcome {
        CrossCheckOutcome::Confirmed { operator } => ("confirmed", Some(operator.clone())),
        CrossCheckOutcome::Mismatch { operator, .. } => ("mismatch", Some(operator.clone())),
        CrossCheckOutcome::Undecided { .. } => ("pending", None),
    }
}

fn decode_hash_hex(hash_hex: &str) -> Option<[u8; 32]> {
    let mut hash = [0u8; 32];
    hex::decode_to_slice(hash_hex, &mut hash).ok()?;
    Some(hash)
}

impl ChainScanner {
    pub fn new(
        chain: MoneroChainConfiguration,
        payments_store: PaymentsStore,
        view_key_opener: Arc<PrivateViewKeyOpener>,
        chain_health_registry: Arc<ChainHealthRegistry>,
    ) -> Self {
        let node_pool = NodePool::new(&chain.chain_key, &chain.nodes, SCANNER_NODE_REQUEST_TIMEOUT);
        let wallet_scanners = WalletScanners::new(&chain.chain_key, chain.network);
        Self {
            chain,
            node_pool,
            payments_store,
            wallet_scanners,
            view_key_opener,
            chain_health_registry,
            next_height_to_scan: None,
            has_watched_wallet_rows: false,
            recent_block_hashes: BTreeMap::new(),
            latest_scanned_block: None,
            scanned_pool_transaction_hashes: HashSet::new(),
            // Replaced by the probe task's receiver when the loop starts (`run_forever`).
            node_probe_outcomes: tokio::sync::watch::channel(Vec::new()).1,
            scanner_started_at: Instant::now(),
            completed_tick: None,
            confirmed_tip_lagging_since: None,
        }
    }

    pub async fn run_forever(mut self) {
        tracing::info!(
            event = "monero_chain_scanner_started",
            chain_key = %self.chain.chain_key,
            network = self.chain.network.name(),
            node_count = self.node_pool.node_count(),
            confirmations_final = self.chain.confirmations_final,
        );
        self.node_probe_outcomes = self.spawn_node_probe_loop();
        self.publish_health();
        loop {
            let tick_started_at = Instant::now();
            match self.run_tick().await {
                Ok(completed_tick) => {
                    let confirmed_tip_lags = completed_tick.confirmed_tip_height.is_none_or(|confirmed_tip_height| {
                        completed_tick.tip_height.saturating_sub(confirmed_tip_height) > CONFIRMED_TIP_LAG_LIMIT_BLOCKS
                    });
                    self.confirmed_tip_lagging_since = if confirmed_tip_lags {
                        Some(
                            self.confirmed_tip_lagging_since
                                .unwrap_or(completed_tick.completed_at.1),
                        )
                    } else {
                        None
                    };
                    self.completed_tick = Some(completed_tick);
                }
                Err(tick_error) => {
                    tracing::error!(event = "monero_scan_tick_failed", chain_key = %self.chain.chain_key, error = %tick_error);
                }
            }
            self.publish_health();
            let poll_interval = Duration::from_secs(self.chain.poll_interval_seconds);
            tokio::time::sleep(poll_interval.saturating_sub(tick_started_at.elapsed())).await;
        }
    }

    /// Probes every node each `NODE_PROBE_INTERVAL` on a task of its own, outside the scan loop, within one poll
    /// interval and `NODE_PROBE_REQUEST_TIMEOUT` at most; the loop takes the outcomes at its next tick.
    fn spawn_node_probe_loop(&self) -> tokio::sync::watch::Receiver<Vec<bool>> {
        let (outcome_sender, outcome_receiver) = tokio::sync::watch::channel(Vec::new());
        let chain_key = self.chain.chain_key.clone();
        let node_configurations = self.chain.nodes.clone();
        let time_limit = NODE_PROBE_REQUEST_TIMEOUT.min(Duration::from_secs(self.chain.poll_interval_seconds));
        tokio::spawn(async move {
            let mut probe_interval = tokio::time::interval(NODE_PROBE_INTERVAL);
            probe_interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
            loop {
                probe_interval.tick().await;
                let node_answered = probe_node_tips(&chain_key, &node_configurations, time_limit).await;
                if outcome_sender.send(node_answered).is_err() {
                    return;
                }
            }
        });
        outcome_receiver
    }

    fn publish_health(&self) {
        self.chain_health_registry.store_snapshot(ChainHealthSnapshot {
            chain_key: self.chain.chain_key.clone(),
            poll_interval: Duration::from_secs(self.chain.poll_interval_seconds),
            scanner_started_at: self.scanner_started_at,
            node_count: self.node_pool.node_count(),
            healthy_operator_count: self.node_pool.healthy_operator_count(),
            tip_height: self.completed_tick.as_ref().map(|tick| tick.tip_height),
            confirmed_tip_height: self.completed_tick.as_ref().and_then(|tick| tick.confirmed_tip_height),
            scanned_height: self.next_height_to_scan.and_then(|height| height.checked_sub(1)),
            last_completed_tick_at: self.completed_tick.as_ref().map(|tick| tick.completed_at),
            unusable_wallet_count: self.wallet_scanners.unusable_wallet_count(),
            confirmed_tip_lagging_since: self.confirmed_tip_lagging_since,
        });
    }

    async fn run_tick(&mut self) -> Result<CompletedTick, String> {
        if self.node_probe_outcomes.has_changed().unwrap_or(false) {
            let node_answered = self.node_probe_outcomes.borrow_and_update().clone();
            self.node_pool.apply_probe_outcomes(&node_answered);
            self.publish_health();
        }

        let wallet_rows = self
            .payments_store
            .read_watched_wallets(&self.chain.chain_key, self.chain.recent_deposit_window_seconds)
            .await?;
        self.has_watched_wallet_rows = !wallet_rows.is_empty();
        self.wallet_scanners
            .refresh_from_watched_wallets(&wallet_rows, &self.view_key_opener);

        let (tip_height, tip_node_index) = self.node_pool.call_with_failover(fetch_tip_height).await?;
        let tip_operator = self.node_pool.node_operator(tip_node_index).to_owned();
        let confirmed_tip_height = match self
            .node_pool
            .fetch_tip_from_another_operator(&tip_operator, tip_height)
            .await
        {
            Some((other_tip_height, _)) => Some(tip_height.min(other_tip_height)),
            None => {
                tracing::warn!(
                    event = "monero_confirmed_tip_unknown",
                    chain_key = %self.chain.chain_key,
                    tip_height,
                    operator = %tip_operator,
                    healthy_operator_count = self.node_pool.healthy_operator_count(),
                );
                None
            }
        };
        if self.next_height_to_scan.is_none() {
            self.start_cursor(tip_height).await?;
        }

        let unusable_wallet_count = self.wallet_scanners.unusable_wallet_count();
        let (scan_result, scanned_through_height) = if unusable_wallet_count > 0 {
            tracing::error!(
                event = "monero_scan_held_for_unusable_wallets",
                chain_key = %self.chain.chain_key,
                unusable_wallet_count,
                next_height_to_scan = self.next_height_to_scan,
                tip_height,
            );
            (Ok(()), None)
        } else {
            let scan_result = self.scan_new_blocks(tip_height).await;
            let scanned_through_height = match scan_result {
                Ok(()) => self.next_height_to_scan.and_then(|height| height.checked_sub(1)),
                Err(_) => None,
            };
            // What was scanned before a failure stays scanned.
            let persist_result = self.persist_cursor().await;
            (scan_result.and(persist_result), scanned_through_height)
        };
        let pool_result = self.scan_pool(scanned_through_height).await;
        let settle_result = self.settle_block_payments(tip_height).await;
        scan_result.and(pool_result).and(settle_result)?;
        Ok(CompletedTick {
            tip_height,
            confirmed_tip_height,
            completed_at: (Instant::now(), SystemTime::now()),
        })
    }

    /// Resumes after the stored cursor (its hash becomes the parent the next block must name), else at the tip.
    async fn start_cursor(&mut self, tip_height: u64) -> Result<(), String> {
        let scan_cursor = self.payments_store.read_scan_cursor(&self.chain.chain_key).await?;
        let start_height = match &scan_cursor {
            Some(scan_cursor) => {
                if let Some(scanned_block_hash) =
                    scan_cursor.scanned_block_hash_hex.as_deref().and_then(decode_hash_hex)
                {
                    self.recent_block_hashes
                        .insert(scan_cursor.scanned_height, scanned_block_hash);
                }
                scan_cursor.scanned_height + 1
            }
            None => tip_height,
        };
        tracing::info!(
            event = "monero_scan_cursor_started",
            chain_key = %self.chain.chain_key,
            start_height,
            tip_height,
            resumed_from_stored_cursor = scan_cursor.is_some(),
        );
        self.next_height_to_scan = Some(start_height);
        Ok(())
    }

    /// Stores the last scanned block (the one before the cursor) and its hash when it was fetched.
    async fn persist_cursor(&mut self) -> Result<(), String> {
        let Some(scanned_height) = self.next_height_to_scan.and_then(|height| height.checked_sub(1)) else {
            return Ok(());
        };
        let scanned_block_hash_hex = self.recent_block_hashes.get(&scanned_height).map(hex::encode);
        self.payments_store
            .write_scan_cursor(&self.chain.chain_key, scanned_height, scanned_block_hash_hex.as_deref())
            .await
    }

    async fn scan_new_blocks(&mut self, tip_height: u64) -> Result<(), String> {
        loop {
            let next_height = self.next_height_to_scan.expect("set before the first scan");
            if next_height > tip_height {
                return Ok(());
            }
            if !self.has_watched_wallet_rows {
                // Nothing to look for: follow the tip without fetching blocks.
                self.next_height_to_scan = Some(tip_height + 1);
                self.recent_block_hashes.clear();
                self.latest_scanned_block = None;
                return Ok(());
            }
            let last_height = (next_height + BLOCKS_PER_FETCH - 1).min(tip_height);
            let (scannable_blocks, node_index) = self
                .node_pool
                .call_with_failover(|daemon| fetch_scannable_blocks(daemon, next_height, last_height))
                .await?;
            let source_operator = self.node_pool.node_operator(node_index).to_owned();

            if let (Some(first_block), Some(remembered_previous_hash)) = (
                scannable_blocks.first(),
                next_height
                    .checked_sub(1)
                    .and_then(|h| self.recent_block_hashes.get(&h)),
            ) {
                if first_block.block.header.previous != *remembered_previous_hash {
                    self.rewind_after_reorg(next_height - 1).await?;
                    continue;
                }
            }

            let fetched_block_count = scannable_blocks.len();
            let (scanned_blocks, scan_error) = self.wallet_scanners.scan_blocks(scannable_blocks).await;
            let mut found_output_count = 0usize;
            for (scannable_block, received_outputs) in &scanned_blocks {
                let found_block = FoundBlock {
                    block_height: scannable_block.block.number() as u64,
                    block_hash: scannable_block.block.hash(),
                    block_timestamp: scannable_block.block.header.timestamp,
                    block_confirmations: tip_height - scannable_block.block.number() as u64 + 1,
                };
                found_output_count += received_outputs.len();
                for received_output in received_outputs {
                    self.record_block_output(received_output, &found_block, &source_operator)
                        .await?;
                }
                self.recent_block_hashes
                    .insert(found_block.block_height, found_block.block_hash);
                while self.recent_block_hashes.len() > REMEMBERED_BLOCK_HASH_COUNT {
                    self.recent_block_hashes.pop_first();
                }
                self.next_height_to_scan = Some(found_block.block_height + 1);
            }
            if let Some((scannable_block, _)) = scanned_blocks.into_iter().last() {
                self.latest_scanned_block = Some(scannable_block.block);
            }
            if let Some(scan_error) = scan_error {
                // The cursor stays on the block that failed: it is scanned again next tick, never passed over.
                return Err(scan_error);
            }
            tracing::info!(
                event = "monero_blocks_scanned",
                chain_key = %self.chain.chain_key,
                first_height = next_height,
                last_height,
                block_count = fetched_block_count,
                wallet_count = self.wallet_scanners.wallet_count(),
                found_output_count,
                operator = %source_operator,
            );
        }
    }

    /// Walks back from `from_height` until the node's hash matches the remembered one, clears rows in the blocks that
    /// left the chain, and sets the cursor after the common block. A parent mismatch with no block gone (the nodes
    /// answering the two reads disagree) fails the tick instead of looping on it.
    async fn rewind_after_reorg(&mut self, from_height: u64) -> Result<(), String> {
        let mut height = from_height;
        let mut vanished_block_count = 0usize;
        while let Some(remembered_hash) = self.recent_block_hashes.get(&height).copied() {
            let (current_hash, _) = self
                .node_pool
                .call_with_failover(|daemon| fetch_block_hash_by_height(daemon, height))
                .await?;
            if current_hash == remembered_hash {
                break;
            }
            self.recent_block_hashes.remove(&height);
            self.payments_store
                .clear_vanished_block(&self.chain.chain_key, height, &hex::encode(remembered_hash))
                .await?;
            vanished_block_count += 1;
            match height.checked_sub(1) {
                Some(lower_height) => height = lower_height,
                None => break,
            }
        }
        if vanished_block_count == 0 {
            return Err(format!(
                "block {} names a parent other than the remembered block {from_height}, yet that block is still on \
                 the chain: the nodes disagree, retrying next tick",
                from_height + 1
            ));
        }
        let resume_height = if self.recent_block_hashes.contains_key(&height) {
            height + 1
        } else {
            height
        };
        self.recent_block_hashes
            .retain(|&remembered_height, _| remembered_height < resume_height);
        self.next_height_to_scan = Some(resume_height);
        self.persist_cursor().await?;
        tracing::warn!(
            event = "monero_reorg_detected",
            chain_key = %self.chain.chain_key,
            from_height,
            resume_height,
            vanished_block_count,
        );
        Ok(())
    }

    /// The invoice an output pays: the wallet's invoice on that minor. Logs and returns `None` otherwise.
    async fn find_invoice_for_output(&mut self, received_output: &ReceivedOutput) -> Result<Option<String>, String> {
        let Some(minor_index) = received_output.minor_index else {
            tracing::warn!(
                event = "monero_output_on_unwatched_index",
                chain_key = %self.chain.chain_key,
                wallet_id = received_output.wallet_id,
                transaction_hash = %hex::encode(received_output.transaction_hash),
            );
            return Ok(None);
        };
        let invoice_id = self
            .payments_store
            .find_invoice_id_by_subaddress(received_output.wallet_id, minor_index)
            .await?;
        if invoice_id.is_none() {
            tracing::warn!(
                event = "monero_output_without_invoice",
                chain_key = %self.chain.chain_key,
                wallet_id = received_output.wallet_id,
                major_index = received_output.major_index,
                minor_index,
                transaction_hash = %hex::encode(received_output.transaction_hash),
            );
        }
        Ok(invoice_id)
    }

    async fn record_block_output(
        &mut self,
        received_output: &ReceivedOutput,
        found_block: &FoundBlock,
        source_operator: &str,
    ) -> Result<(), String> {
        let Some(invoice_id) = self.find_invoice_for_output(received_output).await? else {
            return Ok(());
        };
        let Some(minor_index) = received_output.minor_index else {
            return Ok(());
        };
        let transaction_hash_hex = hex::encode(received_output.transaction_hash);
        let output_public_key_hex = hex::encode(received_output.output_public_key);
        if let Some(existing_payment) = self
            .payments_store
            .find_payment_by_output_public_key(&self.chain.chain_key, &output_public_key_hex)
            .await?
        {
            if existing_payment.transaction_hash_hex != transaction_hash_hex {
                // The burning bug: a second output with a key already received is never counted.
                tracing::error!(
                    event = "monero_duplicate_output_public_key",
                    chain_key = %self.chain.chain_key,
                    invoice_id = %invoice_id,
                    wallet_id = received_output.wallet_id,
                    transaction_hash = %transaction_hash_hex,
                );
                return Ok(());
            }
            if existing_payment.block_height.is_some() {
                return Ok(());
            }
        }

        let Some(wallet_scanner) = self.wallet_scanners.find_wallet_scanner(received_output.wallet_id) else {
            return Ok(());
        };
        let payment_to_cross_check = PaymentToCrossCheck {
            block_height: found_block.block_height,
            block_hash: found_block.block_hash,
            block_confirmations: found_block.block_confirmations,
            transaction_hash: received_output.transaction_hash,
            output_public_key: received_output.output_public_key,
            amount_piconero: received_output.amount_piconero,
            wallet_id: received_output.wallet_id,
            wallet_view_pair: &wallet_scanner.view_pair,
            major_index: wallet_scanner.major_index,
            minor_index,
            required_confirmations_on_other_operator: None,
        };
        let cross_check_outcome =
            cross_check_payment_on_another_operator(&mut self.node_pool, source_operator, &payment_to_cross_check)
                .await;
        self.log_cross_check(
            (&invoice_id, None),
            found_block.block_height,
            false,
            &cross_check_outcome,
        );
        let (cross_check_state, cross_check_operator) = cross_check_state_and_operator(&cross_check_outcome);

        let new_payment = NewPaymentRow {
            chain_key: &self.chain.chain_key,
            invoice_id: &invoice_id,
            transaction_hash_hex: &transaction_hash_hex,
            output_public_key_hex: &output_public_key_hex,
            output_index: received_output.output_index as i32,
            amount_piconero: received_output.amount_piconero,
        };
        let block_hash_hex = hex::encode(found_block.block_hash);
        let written = self
            .payments_store
            .write_block_payment(
                &new_payment,
                &PaymentBlock {
                    block_height: found_block.block_height,
                    block_hash_hex: &block_hash_hex,
                    block_timestamp: found_block.block_timestamp,
                },
                cross_check_state,
                cross_check_operator.as_deref(),
            )
            .await?;
        if written {
            tracing::info!(
                event = "monero_payment_in_block",
                chain_key = %self.chain.chain_key,
                invoice_id = %invoice_id,
                wallet_id = received_output.wallet_id,
                minor_index = received_output.minor_index,
                transaction_hash = %transaction_hash_hex,
                block_height = found_block.block_height,
                cross_check_state,
            );
        }
        Ok(())
    }

    fn log_cross_check(
        &self,
        payment_reference: (&str, Option<i64>),
        block_height: u64,
        final_check: bool,
        outcome: &CrossCheckOutcome,
    ) {
        let (invoice_id, payment_id) = payment_reference;
        match outcome {
            CrossCheckOutcome::Confirmed { operator } => tracing::info!(
                event = "monero_cross_check_confirmed",
                chain_key = %self.chain.chain_key,
                invoice_id,
                payment_id,
                block_height,
                final_check,
                operator = %operator,
            ),
            CrossCheckOutcome::Mismatch { operator, reason } => tracing::error!(
                event = "monero_cross_check_mismatch",
                chain_key = %self.chain.chain_key,
                invoice_id,
                payment_id,
                block_height,
                final_check,
                operator = %operator,
                reason,
            ),
            CrossCheckOutcome::Undecided { reason } => tracing::warn!(
                event = "monero_cross_check_pending",
                chain_key = %self.chain.chain_key,
                invoice_id,
                payment_id,
                block_height,
                final_check,
                healthy_operator_count = self.node_pool.healthy_operator_count(),
                reason,
            ),
        }
    }
}
