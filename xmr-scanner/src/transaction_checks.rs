//! `POST /v1/transaction-checks` (PLAN.md section 14): the payer's "I paid" with a txid. The transaction is fetched
//! by hash, its outputs decoded with the invoice wallet's view key (opened as the scan loop opens it), and compared
//! against the invoice's subaddress minor:
//!
//! - `counted`: a `monero_payments` row of that txid and invoice is already in a block;
//! - `confirming`: it pays the invoice's subaddress, in the pool or in a block not recorded yet. The row is written
//!   then, exactly as the scan loop would (pool sighting, or block row after the second-operator cross-check), so the
//!   watcher credits it from there; nothing else is ever written;
//! - `other_address`: it pays another minor, or nothing of this wallet;
//! - `not_found`: every node that answered does not know it, and none that knew it failed to show its block;
//! - `unavailable`: the check could not be made: no node answered, a node that holds the transaction could not show
//!   its block (on any node), the time limit or the wait for the chain's lock ran out, or the invoice wallet's sealed
//!   key does not open (an operator problem, logged as such). The API answers 503 and the payer retries;
//! - `failed`: only what is proven: the second operator disagrees and none confirms (the row is written with
//!   `mismatch`, as the scan loop writes it), or monerod flags a double spend.
//!
//! Each chain has its own checker with its own node connections and database connection (one per node beside the scan
//! loop's, inside the 3 a public node allows per IP) behind a lock, so checks run one at a time per chain, each within
//! `TRANSACTION_CHECK_TIME_LIMIT`. Its node calls time out after `TRANSACTION_CHECK_NODE_REQUEST_TIMEOUT`, so one
//! slow node cannot eat the whole check, and the lookup starts at the node that answered the checker last.
//! The invoice's outputs are found with a scanner of its one subaddress (no table to build per call).

use std::sync::Arc;
use std::time::Duration;

use monero_wallet::block::Block;
use serde::Serialize;

use crate::configuration::MoneroChainConfiguration;
use crate::cross_check::{cross_check_payment_on_another_operator, CrossCheckOutcome, PaymentToCrossCheck};
use crate::node_pool::{
    fetch_block_by_hash, fetch_block_hash_by_height, fetch_tip_height, fetch_transactions_with_status,
    FetchedTransaction, NodePool,
};
use crate::payments_store::{NewPaymentRow, PaymentBlock, PaymentsStore};
use crate::view_key_sealing::PrivateViewKeyOpener;
use crate::wallet_scanners::{
    open_wallet_view_pair, scan_for_single_minor, wrap_transactions_as_scannable_block, ReceivedOutput,
};

/// Inside the Python client's 25 s timeout (`app/infrastructure/monero_sidecar_api/client.py`).
pub const TRANSACTION_CHECK_TIME_LIMIT: Duration = Duration::from_secs(20);
/// One node call of a check: well under the check's limit, so a slow node leaves time for the next.
const TRANSACTION_CHECK_NODE_REQUEST_TIMEOUT: Duration = Duration::from_secs(6);

#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum TransactionCheckOutcome {
    Counted,
    Confirming,
    OtherAddress,
    NotFound,
    Failed,
    /// The check could not be made (no node answered, out of time, an unopenable wallet key): the payer is asked to
    /// retry, never told it failed.
    Unavailable,
}

/// The wire answer, the shape of the Python `MoneroTransactionCheck`.
#[derive(Debug, Serialize)]
pub struct TransactionCheckAnswer {
    pub outcome: TransactionCheckOutcome,
    pub amount_piconero: Option<u64>,
    pub confirmations: Option<u64>,
}

impl TransactionCheckAnswer {
    pub fn without_amount(outcome: TransactionCheckOutcome) -> Self {
        Self {
            outcome,
            amount_piconero: None,
            confirmations: None,
        }
    }
}

pub struct TransactionChecker {
    chain: MoneroChainConfiguration,
    node_pool: NodePool,
    payments_store: PaymentsStore,
    view_key_opener: Arc<PrivateViewKeyOpener>,
}

/// The transaction as one node holds it, with the block (or, for a pool transaction, the tip block) to carry it.
struct LocatedTransaction {
    fetched_transaction: FetchedTransaction,
    carrier_block: Block,
    tip_height: u64,
    node_index: usize,
}

enum TransactionLookup {
    Located(Box<LocatedTransaction>),
    Answered(TransactionCheckOutcome),
}

impl TransactionChecker {
    pub fn new(
        chain: MoneroChainConfiguration,
        payments_store: PaymentsStore,
        view_key_opener: Arc<PrivateViewKeyOpener>,
    ) -> Self {
        let node_pool = NodePool::new(&chain.chain_key, &chain.nodes, TRANSACTION_CHECK_NODE_REQUEST_TIMEOUT);
        Self {
            chain,
            node_pool,
            payments_store,
            view_key_opener,
        }
    }

    /// Called after a check ran out of time midway: a node connection may hold half a response.
    pub fn reset_after_time_limit(&mut self) {
        self.node_pool.drop_connections();
    }

    /// `Ok(None)` when the invoice is not a Monero invoice of this chain; `Err` on a database error.
    pub async fn check_transaction(
        &mut self,
        invoice_id: &str,
        transaction_hash: [u8; 32],
    ) -> Result<Option<TransactionCheckAnswer>, String> {
        let chain_key = self.chain.chain_key.clone();
        let Some(invoice_wallet) = self.payments_store.find_invoice_wallet(&chain_key, invoice_id).await? else {
            return Ok(None);
        };
        let transaction_hash_hex = hex::encode(transaction_hash);
        let recorded_payments = self
            .payments_store
            .read_invoice_transaction_payments(&chain_key, invoice_id, &transaction_hash_hex)
            .await?;
        if let Some(recorded_height) = recorded_payments.iter().filter_map(|(height, _)| *height).min() {
            let tip_height = self.node_pool.call_with_failover(fetch_tip_height).await.ok();
            return Ok(Some(TransactionCheckAnswer {
                outcome: TransactionCheckOutcome::Counted,
                amount_piconero: Some(recorded_payments.iter().map(|(_, amount)| amount).sum()),
                confirmations: tip_height.map(|(tip_height, _)| tip_height.saturating_sub(recorded_height) + 1),
            }));
        }

        let located_transaction = match self.locate_transaction(transaction_hash).await {
            TransactionLookup::Located(located_transaction) => located_transaction,
            TransactionLookup::Answered(outcome) => return Ok(Some(TransactionCheckAnswer::without_amount(outcome))),
        };
        let wallet_id = invoice_wallet.wallet.wallet_id;
        let (wallet_view_pair, major_index) = match open_wallet_view_pair(
            &invoice_wallet.wallet,
            &chain_key,
            self.chain.network,
            &self.view_key_opener,
        ) {
            Ok(view_pair_and_major_index) => view_pair_and_major_index,
            Err(reason) => {
                // An operator problem (the seed changed, or the row is damaged), not the payer's: retry later.
                tracing::error!(
                    event = "monero_wallet_unusable",
                    chain_key = %chain_key,
                    wallet_id,
                    account_id = invoice_wallet.wallet.account_id,
                    invoice_id,
                    reason,
                    source = "transaction_check",
                );
                return Ok(Some(TransactionCheckAnswer::without_amount(
                    TransactionCheckOutcome::Unavailable,
                )));
            }
        };
        let fetched_transaction = &located_transaction.fetched_transaction;
        let Some(scannable_block) = wrap_transactions_as_scannable_block(
            &located_transaction.carrier_block,
            vec![(transaction_hash, fetched_transaction.transaction.clone())],
        ) else {
            return Ok(Some(TransactionCheckAnswer::without_amount(
                TransactionCheckOutcome::Unavailable,
            )));
        };
        let Ok(scanned_outputs) = scan_for_single_minor(
            &wallet_view_pair,
            wallet_id,
            major_index,
            invoice_wallet.subaddress_minor,
            &scannable_block,
        ) else {
            return Ok(Some(TransactionCheckAnswer::without_amount(
                TransactionCheckOutcome::Unavailable,
            )));
        };
        let invoice_outputs: Vec<ReceivedOutput> = scanned_outputs
            .into_iter()
            .filter(|received_output| received_output.transaction_hash == transaction_hash)
            .collect();
        if invoice_outputs.is_empty() {
            return Ok(Some(TransactionCheckAnswer::without_amount(
                TransactionCheckOutcome::OtherAddress,
            )));
        }
        let amount_piconero = invoice_outputs.iter().map(|output| output.amount_piconero).sum();
        let answer = match fetched_transaction.block_height {
            None => {
                self.record_pool_outputs(invoice_id, &invoice_outputs, fetched_transaction.double_spend_seen)
                    .await?;
                TransactionCheckAnswer {
                    outcome: if fetched_transaction.double_spend_seen {
                        TransactionCheckOutcome::Failed
                    } else {
                        TransactionCheckOutcome::Confirming
                    },
                    amount_piconero: Some(amount_piconero),
                    confirmations: Some(0),
                }
            }
            Some(block_height) => {
                let any_mismatch = self
                    .record_block_outputs(
                        invoice_id,
                        &invoice_outputs,
                        &located_transaction,
                        (&wallet_view_pair, major_index, invoice_wallet.subaddress_minor),
                    )
                    .await?;
                TransactionCheckAnswer {
                    outcome: if any_mismatch {
                        TransactionCheckOutcome::Failed
                    } else {
                        TransactionCheckOutcome::Confirming
                    },
                    amount_piconero: Some(amount_piconero),
                    confirmations: Some(located_transaction.tip_height.saturating_sub(block_height) + 1),
                }
            }
        };
        Ok(Some(answer))
    }

    /// Asks each node in failover order; the first that holds the transaction and shows the block that carries it
    /// (and its tip) wins. A node whose block read fails, or whose block does not list the transaction (a reorg
    /// between two reads, or a node contradicting itself), proves nothing: the next node is asked.
    async fn locate_transaction(&mut self, transaction_hash: [u8; 32]) -> TransactionLookup {
        let mut some_node_answered = false;
        let mut some_node_held_it_unshown = false;
        for node_index in self.node_pool.node_indexes_in_failover_order() {
            let Ok(mut fetched_transactions) = self
                .node_pool
                .call_on_node(node_index, |daemon| {
                    fetch_transactions_with_status(daemon, vec![transaction_hash])
                })
                .await
            else {
                continue;
            };
            some_node_answered = true;
            let Some(fetched_transaction) = fetched_transactions.pop() else {
                continue;
            };
            match self.read_carrier_block(node_index, &fetched_transaction).await {
                Ok(Some((carrier_block, tip_height))) => {
                    self.node_pool.prefer_node(node_index);
                    return TransactionLookup::Located(Box::new(LocatedTransaction {
                        fetched_transaction,
                        carrier_block,
                        tip_height,
                        node_index,
                    }));
                }
                Ok(None) | Err(_) => some_node_held_it_unshown = true,
            }
        }
        TransactionLookup::Answered(if some_node_answered && !some_node_held_it_unshown {
            TransactionCheckOutcome::NotFound
        } else {
            TransactionCheckOutcome::Unavailable
        })
    }

    /// The including block (checked to list the transaction), or the tip block for a pool transaction, and the tip.
    async fn read_carrier_block(
        &mut self,
        node_index: usize,
        fetched_transaction: &FetchedTransaction,
    ) -> Result<Option<(Block, u64)>, String> {
        let tip_height = self.node_pool.call_on_node(node_index, fetch_tip_height).await?;
        let carrier_height = fetched_transaction.block_height.unwrap_or(tip_height);
        let carrier_block_hash = self
            .node_pool
            .call_on_node(node_index, |daemon| fetch_block_hash_by_height(daemon, carrier_height))
            .await?;
        let carrier_block = self
            .node_pool
            .call_on_node(node_index, |daemon| fetch_block_by_hash(daemon, carrier_block_hash))
            .await?;
        if fetched_transaction.block_height.is_some()
            && !carrier_block
                .transactions
                .contains(&fetched_transaction.transaction_hash)
        {
            return Ok(None);
        }
        Ok(Some((carrier_block, tip_height)))
    }

    async fn record_pool_outputs(
        &mut self,
        invoice_id: &str,
        invoice_outputs: &[ReceivedOutput],
        double_spend_seen: bool,
    ) -> Result<(), String> {
        for received_output in invoice_outputs {
            let transaction_hash_hex = hex::encode(received_output.transaction_hash);
            let output_public_key_hex = hex::encode(received_output.output_public_key);
            let new_payment = NewPaymentRow {
                chain_key: &self.chain.chain_key,
                invoice_id,
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
                    invoice_id,
                    wallet_id = received_output.wallet_id,
                    minor_index = received_output.minor_index,
                    transaction_hash = %transaction_hash_hex,
                    double_spend_seen,
                    source = "transaction_check",
                );
            }
        }
        Ok(())
    }

    /// Writes the outputs with height and hash after the second-operator cross-check, as the scan loop does; an output
    /// key already held by another transaction is never written. True when a cross-check found a mismatch.
    async fn record_block_outputs(
        &mut self,
        invoice_id: &str,
        invoice_outputs: &[ReceivedOutput],
        located_transaction: &LocatedTransaction,
        invoice_subaddress: (&monero_wallet::ViewPair, u32, u32),
    ) -> Result<bool, String> {
        let (wallet_view_pair, major_index, minor_index) = invoice_subaddress;
        let block_height = located_transaction
            .fetched_transaction
            .block_height
            .expect("only called for a transaction in a block");
        let block_hash = located_transaction.carrier_block.hash();
        let block_hash_hex = hex::encode(block_hash);
        let source_operator = self.node_pool.node_operator(located_transaction.node_index).to_owned();
        let mut any_mismatch = false;
        for received_output in invoice_outputs {
            let transaction_hash_hex = hex::encode(received_output.transaction_hash);
            let output_public_key_hex = hex::encode(received_output.output_public_key);
            if let Some(existing_payment) = self
                .payments_store
                .find_payment_by_output_public_key(&self.chain.chain_key, &output_public_key_hex)
                .await?
            {
                if existing_payment.transaction_hash_hex != transaction_hash_hex {
                    tracing::error!(
                        event = "monero_duplicate_output_public_key",
                        chain_key = %self.chain.chain_key,
                        invoice_id,
                        wallet_id = received_output.wallet_id,
                        transaction_hash = %transaction_hash_hex,
                    );
                    continue;
                }
            }
            let payment_to_cross_check = PaymentToCrossCheck {
                block_height,
                block_hash,
                block_confirmations: located_transaction.tip_height.saturating_sub(block_height) + 1,
                transaction_hash: received_output.transaction_hash,
                output_public_key: received_output.output_public_key,
                amount_piconero: received_output.amount_piconero,
                wallet_id: received_output.wallet_id,
                wallet_view_pair,
                major_index,
                minor_index,
                required_confirmations_on_other_operator: None,
            };
            let cross_check_outcome =
                cross_check_payment_on_another_operator(&mut self.node_pool, &source_operator, &payment_to_cross_check)
                    .await;
            let (cross_check_state, cross_check_operator) = match &cross_check_outcome {
                CrossCheckOutcome::Confirmed { operator } => ("confirmed", Some(operator.clone())),
                CrossCheckOutcome::Mismatch { operator, .. } => {
                    any_mismatch = true;
                    ("mismatch", Some(operator.clone()))
                }
                CrossCheckOutcome::Undecided { .. } => ("pending", None),
            };
            let new_payment = NewPaymentRow {
                chain_key: &self.chain.chain_key,
                invoice_id,
                transaction_hash_hex: &transaction_hash_hex,
                output_public_key_hex: &output_public_key_hex,
                output_index: received_output.output_index as i32,
                amount_piconero: received_output.amount_piconero,
            };
            if self
                .payments_store
                .write_block_payment(
                    &new_payment,
                    &PaymentBlock {
                        block_height,
                        block_hash_hex: &block_hash_hex,
                        block_timestamp: located_transaction.carrier_block.header.timestamp,
                    },
                    cross_check_state,
                    cross_check_operator.as_deref(),
                )
                .await?
            {
                tracing::info!(
                    event = "monero_payment_in_block",
                    chain_key = %self.chain.chain_key,
                    invoice_id,
                    wallet_id = received_output.wallet_id,
                    minor_index = received_output.minor_index,
                    transaction_hash = %transaction_hash_hex,
                    block_height,
                    cross_check_state,
                    source = "transaction_check",
                );
            }
        }
        Ok(any_mismatch)
    }
}
