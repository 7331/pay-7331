//! Step 5 of the scan loop: rows in a block until they are settled. Each tick, for every row up to its invoice's
//! `confirmations_final` deep (and any row whose cross-check is still pending): the block hash at its height is read
//! again (a vanished block clears the rows and rewinds the cursor), a pending first cross-check is retried, and at
//! `confirmations_final` the final cross-check runs (`required_confirmations_on_other_operator`: the other operator's
//! own tip must be that deep too). An invoice carries its own `confirmations_final` (the receiver's choice, owner
//! 2026-10-15); the chain's is the default. A check that disagreed is asked again each tick until the row is past
//! final, so an operator that was behind or on a fork can still confirm it; a repeat of the same disagreement writes
//! nothing.

use std::collections::{BTreeMap, HashSet};

use super::{cross_check_state_and_operator, decode_hash_hex, ChainScanner};
use crate::cross_check::{cross_check_payment_on_another_operator, PaymentToCrossCheck};
use crate::node_pool::fetch_block_hash_by_height;

/// Which of a row's two checks to run this tick: whether it is the final one, and the state it has now.
fn select_checks_to_run(
    cross_check_state: &str,
    final_cross_check_state: &str,
    block_confirmations: u64,
    confirmations_final: u64,
) -> Vec<(bool, String)> {
    let mut checks_to_run = Vec::new();
    let first_check_due = cross_check_state == "pending"
        || (cross_check_state == "mismatch" && block_confirmations <= confirmations_final);
    if first_check_due {
        checks_to_run.push((false, cross_check_state.to_owned()));
    }
    let final_check_due = block_confirmations >= confirmations_final
        && (final_cross_check_state == "pending" || final_cross_check_state == "mismatch");
    if final_check_due {
        checks_to_run.push((true, final_cross_check_state.to_owned()));
    }
    checks_to_run
}

impl ChainScanner {
    pub(super) async fn settle_block_payments(&mut self, tip_height: u64) -> Result<(), String> {
        let unsettled_payments = self
            .payments_store
            .read_unsettled_block_payments(&self.chain.chain_key, tip_height, self.chain.confirmations_final)
            .await?;
        let mut current_hash_and_operator_by_height: BTreeMap<u64, ([u8; 32], String)> = BTreeMap::new();
        let mut vanished_heights: HashSet<u64> = HashSet::new();
        for payment in &unsettled_payments {
            let block_height = payment.block_height;
            if vanished_heights.contains(&block_height) {
                continue;
            }
            let Some(recorded_block_hash) = decode_hash_hex(&payment.block_hash_hex) else {
                continue;
            };
            let (current_hash, source_operator) = match current_hash_and_operator_by_height.get(&block_height) {
                Some(current_hash_and_operator) => current_hash_and_operator.clone(),
                None => {
                    let (current_hash, node_index) = self
                        .node_pool
                        .call_with_failover(|daemon| fetch_block_hash_by_height(daemon, block_height))
                        .await?;
                    let current_hash_and_operator = (current_hash, self.node_pool.node_operator(node_index).to_owned());
                    current_hash_and_operator_by_height.insert(block_height, current_hash_and_operator.clone());
                    current_hash_and_operator
                }
            };
            if current_hash != recorded_block_hash {
                let cleared_payment_ids = self
                    .payments_store
                    .clear_vanished_block(&self.chain.chain_key, block_height, &payment.block_hash_hex)
                    .await?;
                vanished_heights.insert(block_height);
                self.recent_block_hashes
                    .retain(|&remembered_height, _| remembered_height < block_height);
                let next_height = self.next_height_to_scan.unwrap_or(block_height);
                self.next_height_to_scan = Some(next_height.min(block_height));
                self.persist_cursor().await?;
                tracing::warn!(
                    event = "monero_payment_block_vanished",
                    chain_key = %self.chain.chain_key,
                    block_height,
                    payment_ids = ?cleared_payment_ids,
                );
                continue;
            }

            let block_confirmations = tip_height.saturating_sub(block_height) + 1;
            let checks_to_run = select_checks_to_run(
                &payment.cross_check_state,
                &payment.final_cross_check_state,
                block_confirmations,
                payment.confirmations_final,
            );
            if checks_to_run.is_empty() {
                continue;
            }
            let Some(wallet_scanner) = self.wallet_scanners.find_wallet_scanner(payment.monero_wallet_id) else {
                continue;
            };
            let Some(transaction_hash) = decode_hash_hex(&payment.transaction_hash_hex) else {
                continue;
            };
            let Some(output_public_key) = decode_hash_hex(&payment.output_public_key_hex) else {
                continue;
            };
            let wallet_view_pair = wallet_scanner.view_pair.clone();
            let major_index = wallet_scanner.major_index;
            for (final_check, current_state) in checks_to_run {
                let payment_to_cross_check = PaymentToCrossCheck {
                    block_height,
                    block_hash: recorded_block_hash,
                    block_confirmations,
                    transaction_hash,
                    output_public_key,
                    amount_piconero: payment.amount_piconero,
                    wallet_id: payment.monero_wallet_id,
                    wallet_view_pair: &wallet_view_pair,
                    major_index,
                    minor_index: payment.subaddress_minor,
                    required_confirmations_on_other_operator: final_check.then_some(payment.confirmations_final),
                };
                let outcome = cross_check_payment_on_another_operator(
                    &mut self.node_pool,
                    &source_operator,
                    &payment_to_cross_check,
                )
                .await;
                let (state, operator) = cross_check_state_and_operator(&outcome);
                if current_state == "mismatch" && state != "confirmed" {
                    // Still no operator vouches for it: the row stays held, nothing new to write or log.
                    continue;
                }
                self.log_cross_check(
                    (&payment.invoice_id, Some(payment.payment_id)),
                    block_height,
                    final_check,
                    &outcome,
                );
                if let Some(operator) = operator {
                    self.payments_store
                        .record_cross_check(
                            payment.payment_id,
                            &payment.block_hash_hex,
                            final_check,
                            state,
                            &operator,
                        )
                        .await?;
                }
            }
        }
        Ok(())
    }
}
