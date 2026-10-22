//! The second-operator check (PLAN.md section 14): wallets don't verify proof of work, so a node can lie or withhold.
//! On nodes of operators other than the one the block came from: the block hash at that height matches, the
//! transaction is in that block's `tx_hashes`, and the amount re-decoded from that node's copy of the transaction
//! matches. Operators are counted, not hosts.
//!
//! Every node of another operator is asked until one confirms: `confirmed` as soon as one does, `mismatch` only when
//! at least one disagreed and none confirmed, else undecided (stays `pending` for the next tick). One lagging or lying
//! operator therefore cannot hold a payment that another operator vouches for.
//!
//! The final check (at `confirmations_final`) also reads that node's own tip (`get_height`) and stays undecided until
//! the block is `confirmations_final` deep there too, so finality never rests on the source node's tip alone.
//!
//! A block hash that differs is a disagreement only once our block has `HASH_MISMATCH_SETTLE_CONFIRMATIONS`
//! confirmations: before that the other node may just sit on the other side of a fork race at the tip. A missing
//! transaction or a different amount is a disagreement at once.

use monero_wallet::ViewPair;

use crate::node_pool::{
    fetch_block_by_hash, fetch_block_hash_by_height, fetch_pruned_transactions, fetch_tip_height, NodePool,
};
use crate::wallet_scanners::{scan_for_single_minor, wrap_transactions_as_scannable_block};

pub const HASH_MISMATCH_SETTLE_CONFIRMATIONS: u64 = 2;

pub enum CrossCheckOutcome {
    Confirmed {
        operator: String,
    },
    Mismatch {
        operator: String,
        reason: &'static str,
    },
    /// No node of another operator answered, or no answer is conclusive yet: stays `pending`.
    Undecided {
        reason: &'static str,
    },
}

pub struct PaymentToCrossCheck<'payment> {
    pub block_height: u64,
    pub block_hash: [u8; 32],
    pub block_confirmations: u64,
    pub transaction_hash: [u8; 32],
    pub output_public_key: [u8; 32],
    pub amount_piconero: u64,
    pub wallet_id: i64,
    pub wallet_view_pair: &'payment ViewPair,
    pub major_index: u32,
    pub minor_index: u32,
    /// The final check: the depth the block must have on the other operator's own tip.
    pub required_confirmations_on_other_operator: Option<u64>,
}

enum NodeVerdict {
    Confirmed,
    Mismatch(&'static str),
    Inconclusive(&'static str),
}

pub async fn cross_check_payment_on_another_operator(
    node_pool: &mut NodePool,
    source_operator: &str,
    payment: &PaymentToCrossCheck<'_>,
) -> CrossCheckOutcome {
    let other_operator_node_indexes = node_pool.node_indexes_of_other_operators(source_operator);
    let mut first_mismatch: Option<(String, &'static str)> = None;
    let mut last_reason = "no node of another operator is configured";
    for node_index in other_operator_node_indexes {
        let operator = node_pool.node_operator(node_index).to_owned();
        match check_payment_on_node(node_pool, node_index, payment).await {
            Ok(NodeVerdict::Confirmed) => return CrossCheckOutcome::Confirmed { operator },
            Ok(NodeVerdict::Mismatch(reason)) => {
                tracing::warn!(
                    event = "monero_cross_check_node_disagreed",
                    node_index,
                    operator = %operator,
                    block_height = payment.block_height,
                    transaction_hash = %hex::encode(payment.transaction_hash),
                    reason,
                );
                first_mismatch.get_or_insert((operator, reason));
            }
            Ok(NodeVerdict::Inconclusive(reason)) => last_reason = reason,
            Err(_) => last_reason = "no node of another operator answered",
        }
    }
    match first_mismatch {
        Some((operator, reason)) => CrossCheckOutcome::Mismatch { operator, reason },
        None => CrossCheckOutcome::Undecided { reason: last_reason },
    }
}

async fn check_payment_on_node(
    node_pool: &mut NodePool,
    node_index: usize,
    payment: &PaymentToCrossCheck<'_>,
) -> Result<NodeVerdict, String> {
    let block_height = payment.block_height;
    if let Some(required_confirmations) = payment.required_confirmations_on_other_operator {
        let other_tip_height = node_pool.call_on_node(node_index, fetch_tip_height).await?;
        if (other_tip_height + 1).saturating_sub(block_height) < required_confirmations {
            return Ok(NodeVerdict::Inconclusive(
                "the block is not final on that operator's tip yet",
            ));
        }
    }
    let other_block_hash = node_pool
        .call_on_node(node_index, |daemon| fetch_block_hash_by_height(daemon, block_height))
        .await?;
    if other_block_hash != payment.block_hash {
        return Ok(if payment.block_confirmations >= HASH_MISMATCH_SETTLE_CONFIRMATIONS {
            NodeVerdict::Mismatch("block hash at that height differs")
        } else {
            NodeVerdict::Inconclusive("block hash differs at the tip, waiting a block")
        });
    }
    let block_hash = payment.block_hash;
    let other_block = node_pool
        .call_on_node(node_index, |daemon| fetch_block_by_hash(daemon, block_hash))
        .await?;
    if !other_block.transactions.contains(&payment.transaction_hash) {
        return Ok(NodeVerdict::Mismatch("transaction is not in that block"));
    }
    let transaction_hash = payment.transaction_hash;
    let mut other_transactions = node_pool
        .call_on_node(node_index, |daemon| {
            fetch_pruned_transactions(daemon, vec![transaction_hash])
        })
        .await?;
    let Some(other_transaction) = other_transactions.pop() else {
        return Err("no transaction returned".to_owned());
    };
    let Some(scannable_block) =
        wrap_transactions_as_scannable_block(&other_block, vec![(transaction_hash, other_transaction)])
    else {
        return Err("block could not be rebuilt around the transaction".to_owned());
    };
    let received_outputs = scan_for_single_minor(
        payment.wallet_view_pair,
        payment.wallet_id,
        payment.major_index,
        payment.minor_index,
        &scannable_block,
    )?;
    let redecoded_output = received_outputs.iter().find(|received_output| {
        received_output.transaction_hash == payment.transaction_hash
            && received_output.output_public_key == payment.output_public_key
    });
    Ok(match redecoded_output {
        Some(received_output) if received_output.amount_piconero == payment.amount_piconero => NodeVerdict::Confirmed,
        Some(_) => NodeVerdict::Mismatch("amount re-decoded from that node differs"),
        None => NodeVerdict::Mismatch("output not found in that node's copy of the transaction"),
    })
}
