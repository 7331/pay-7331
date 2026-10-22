//! Step 4 of the scan loop: the pool. New pool transactions are fetched once (`get_transaction_pool_hashes`, then
//! `get_transactions` for unseen hashes), scanned on tokio's blocking pool and written as sightings (`in_pool`, no
//! height). Rows still without a block follow the pool each tick (`in_pool`, `double_spend_seen`) while they are in
//! it or left it less than a day ago. Every node call fails over through the node list.
//!
//! A row leaves the pool (`in_pool = false`) only in a tick whose block scan completed through the tip the pool's own
//! node reported right after its pool read (`fetch_pool_transaction_hashes_and_tip`): a transaction mined between the
//! tick's block scan and its pool read is gone from the pool before its block is scanned, and must not look like one
//! that left unmined until the next tick writes its block. A failed or held scan defers every departure.

use std::collections::{BTreeMap, HashSet};

use super::{decode_hash_hex, ChainScanner};
use crate::node_pool::{
    fetch_pool_transaction_hashes_and_tip, fetch_pool_transactions, fetch_scannable_blocks, fetch_tip_height,
};
use crate::payments_store::NewPaymentRow;
use crate::wallet_scanners::{wrap_transactions_as_scannable_block, ReceivedOutput};

impl ChainScanner {
    /// `scanned_through_height`: the last block this tick's block scan completed through, `None` when it failed or
    /// held.
    pub(super) async fn scan_pool(&mut self, scanned_through_height: Option<u64>) -> Result<(), String> {
        if self.wallet_scanners.wallet_count() == 0 {
            return Ok(());
        }
        // A resumed cursor can already be at the tip, and following with no wallets fetches no block. Restore the
        // pool's carrier before completing a tick with watched wallets, even when no new block was scanned.
        if self.latest_scanned_block.is_none() {
            let (carrier_block, _) = self
                .node_pool
                .call_with_failover(|daemon| async move {
                    let tip_height = fetch_tip_height(daemon.clone()).await?;
                    let mut blocks = fetch_scannable_blocks(daemon, tip_height, tip_height).await?;
                    blocks
                        .pop()
                        .map(|block| block.block)
                        .ok_or_else(|| "pool carrier block missing".to_owned())
                })
                .await?;
            self.latest_scanned_block = Some(carrier_block);
        }
        let carrier_block = self.latest_scanned_block.clone().expect("restored above");
        let ((pool_transaction_hashes, pool_tip_height), _) = self
            .node_pool
            .call_with_failover(fetch_pool_transaction_hashes_and_tip)
            .await?;
        let departures_allowed = scanned_through_height.is_some_and(|scanned_height| scanned_height >= pool_tip_height);
        let pool_transaction_hash_set: HashSet<[u8; 32]> = pool_transaction_hashes.iter().copied().collect();
        self.scanned_pool_transaction_hashes
            .retain(|hash| pool_transaction_hash_set.contains(hash));
        let new_transaction_hashes: Vec<[u8; 32]> = pool_transaction_hashes
            .iter()
            .filter(|hash| !self.scanned_pool_transaction_hashes.contains(*hash))
            .copied()
            .collect();

        if !new_transaction_hashes.is_empty() {
            let (pool_transactions, _) = self
                .node_pool
                .call_with_failover(|daemon| fetch_pool_transactions(daemon, new_transaction_hashes.clone()))
                .await?;
            let double_spend_seen_by_hash: BTreeMap<[u8; 32], bool> = pool_transactions
                .iter()
                .map(|pool_transaction| (pool_transaction.transaction_hash, pool_transaction.double_spend_seen))
                .collect();
            let scanned_count = pool_transactions.len();
            let carried_transactions = pool_transactions
                .into_iter()
                .map(|pool_transaction| (pool_transaction.transaction_hash, pool_transaction.transaction))
                .collect();
            let mut found_output_count = 0usize;
            if let Some(scannable_block) = wrap_transactions_as_scannable_block(&carrier_block, carried_transactions) {
                let (scanned_blocks, scan_error) = self.wallet_scanners.scan_blocks(vec![scannable_block]).await;
                if let Some(scan_error) = scan_error {
                    return Err(format!("pool scan: {scan_error}"));
                }
                let received_outputs: Vec<ReceivedOutput> = scanned_blocks
                    .into_iter()
                    .flat_map(|(_, received_outputs)| received_outputs)
                    .filter(|received_output| double_spend_seen_by_hash.contains_key(&received_output.transaction_hash))
                    .collect();
                found_output_count = received_outputs.len();
                for received_output in &received_outputs {
                    let double_spend_seen = double_spend_seen_by_hash[&received_output.transaction_hash];
                    self.record_pool_output(received_output, double_spend_seen).await?;
                }
            }
            // Only what was fetched counts as scanned; a hash the node no longer had is tried again next tick.
            self.scanned_pool_transaction_hashes
                .extend(double_spend_seen_by_hash.keys().copied());
            tracing::debug!(
                event = "monero_pool_scanned",
                chain_key = %self.chain.chain_key,
                pool_transaction_count = pool_transaction_hashes.len(),
                scanned_count,
                found_output_count,
            );
        }

        self.sync_unmined_payment_pool_state(&pool_transaction_hash_set, departures_allowed, pool_tip_height)
            .await
    }

    async fn record_pool_output(
        &mut self,
        received_output: &ReceivedOutput,
        double_spend_seen: bool,
    ) -> Result<(), String> {
        let Some(invoice_id) = self.find_invoice_for_output(received_output).await? else {
            return Ok(());
        };
        let transaction_hash_hex = hex::encode(received_output.transaction_hash);
        let output_public_key_hex = hex::encode(received_output.output_public_key);
        let new_payment = NewPaymentRow {
            chain_key: &self.chain.chain_key,
            invoice_id: &invoice_id,
            transaction_hash_hex: &transaction_hash_hex,
            output_public_key_hex: &output_public_key_hex,
            output_index: received_output.output_index as i32,
            amount_piconero: received_output.amount_piconero,
        };
        if self
            .payments_store
            .insert_pool_payment(&new_payment, double_spend_seen)
            .await?
        {
            tracing::info!(
                event = "monero_payment_seen_in_pool",
                chain_key = %self.chain.chain_key,
                invoice_id = %invoice_id,
                wallet_id = received_output.wallet_id,
                minor_index = received_output.minor_index,
                transaction_hash = %transaction_hash_hex,
                double_spend_seen,
            );
        }
        Ok(())
    }

    /// `in_pool` and `double_spend_seen` of rows without a block follow the pool (the flag only while in the pool). A
    /// row missing from the pool leaves it only when `departures_allowed` (module docs); else it waits a tick.
    async fn sync_unmined_payment_pool_state(
        &mut self,
        pool_transaction_hash_set: &HashSet<[u8; 32]>,
        departures_allowed: bool,
        pool_tip_height: u64,
    ) -> Result<(), String> {
        let unmined_transaction_hashes = self
            .payments_store
            .read_unmined_payment_transaction_hashes(&self.chain.chain_key)
            .await?;
        let mut still_in_pool_hashes = Vec::new();
        for transaction_hash_hex in &unmined_transaction_hashes {
            let Some(transaction_hash) = decode_hash_hex(transaction_hash_hex) else {
                continue;
            };
            if pool_transaction_hash_set.contains(&transaction_hash) {
                still_in_pool_hashes.push(transaction_hash);
            } else if !departures_allowed {
                tracing::debug!(
                    event = "monero_pool_departure_deferred",
                    chain_key = %self.chain.chain_key,
                    transaction_hash = %transaction_hash_hex,
                    pool_tip_height,
                    next_height_to_scan = self.next_height_to_scan,
                );
            } else {
                let changed = self
                    .payments_store
                    .update_unmined_payment_pool_state(&self.chain.chain_key, transaction_hash_hex, false, None)
                    .await?;
                if changed > 0 {
                    tracing::info!(
                        event = "monero_payment_left_pool",
                        chain_key = %self.chain.chain_key,
                        transaction_hash = %transaction_hash_hex,
                        row_count = changed,
                    );
                }
            }
        }
        if still_in_pool_hashes.is_empty() {
            return Ok(());
        }
        let (pool_transactions, _) = self
            .node_pool
            .call_with_failover(|daemon| fetch_pool_transactions(daemon, still_in_pool_hashes.clone()))
            .await?;
        for pool_transaction in pool_transactions {
            let transaction_hash_hex = hex::encode(pool_transaction.transaction_hash);
            let changed = self
                .payments_store
                .update_unmined_payment_pool_state(
                    &self.chain.chain_key,
                    &transaction_hash_hex,
                    true,
                    Some(pool_transaction.double_spend_seen),
                )
                .await?;
            if changed > 0 {
                tracing::info!(
                    event = "monero_payment_pool_state_changed",
                    chain_key = %self.chain.chain_key,
                    transaction_hash = %transaction_hash_hex,
                    double_spend_seen = pool_transaction.double_spend_seen,
                    row_count = changed,
                );
            }
        }
        Ok(())
    }
}
