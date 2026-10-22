//! One monero-wallet `Scanner` per watched wallet of a chain (active, replaced with an open invoice, or replaced with
//! an invoice closed within the chain's `recent_deposit_window_seconds`), with major index `major_index` (1) and minors
//! 1 up to `next_minor_index + 500` registered (PLAN.md section 14: the lookup cost does not grow with the table). A
//! scanner checks the view tag first, then the subaddress table, decrypts the amount and verifies it against the
//! output's commitment, all inside monero-wallet (`scan.rs`). Outputs with an additional timelock (`unlock_time != 0`)
//! are dropped here.
//!
//! Scanning is CPU work, so whole batches run on tokio's blocking pool (`scan_blocks`), never on the async workers.
//! A wallet whose row cannot be turned into a scanner is unusable: it is logged each tick and counted, and the scan
//! loop holds its cursor while there is one, so no block is skipped for it.
//! The cross-check and the txid check scan one transaction for one known minor, so they build a scanner with that
//! one subaddress (`scan_for_single_minor`) instead of copying the table.

use std::collections::{BTreeMap, BTreeSet};

use monero_simple_request_rpc::prelude::ScannableBlock;
use monero_wallet::address::SubaddressIndex;
use monero_wallet::block::Block;
use monero_wallet::transaction::{Pruned, Transaction};
use monero_wallet::{Scanner, ViewPair};
use sha2::{Digest, Sha256};

use crate::monero_keys::{build_matching_view_pair, decode_private_view_key_hex, parse_primary_address, MoneroNetwork};
use crate::payments_store::WatchedWalletRow;
use crate::view_key_sealing::PrivateViewKeyOpener;

/// Minors registered past the wallet's next unused one.
pub const SUBADDRESS_LOOKAHEAD_PAST_NEXT_MINOR: u32 = 500;

pub struct WalletScanner {
    pub wallet_id: i64,
    pub major_index: u32,
    /// The wallet's view pair, for the one-minor scanners of the cross-check.
    pub view_pair: ViewPair,
    registered_through_minor: u32,
    /// SHA-256 of the sealed key and the address, so a changed row rebuilds its scanner. Never logged.
    row_fingerprint: [u8; 32],
    scanner: Scanner,
}

/// An output a wallet received: on subaddress (`major_index`, `minor_index`), no additional timelock.
#[derive(Clone, Debug)]
pub struct ReceivedOutput {
    pub wallet_id: i64,
    pub major_index: u32,
    /// `None` when the output paid the primary address or another major index.
    pub minor_index: Option<u32>,
    pub transaction_hash: [u8; 32],
    pub output_public_key: [u8; 32],
    pub output_index: u64,
    pub amount_piconero: u64,
}

fn fingerprint_wallet_row(wallet_row: &WatchedWalletRow) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(wallet_row.sealed_private_view_key.as_slice());
    hasher.update(wallet_row.primary_address.as_bytes());
    hasher.update(wallet_row.major_index.to_le_bytes());
    hasher.finalize().into()
}

/// The wallet row's view pair and major index: the sealed key opened under this chain and row, checked against the
/// address. `Err` names why the row is unusable (a fixed text, never a key or an address).
pub fn open_wallet_view_pair(
    wallet_row: &WatchedWalletRow,
    chain_key: &str,
    network: MoneroNetwork,
    view_key_opener: &PrivateViewKeyOpener,
) -> Result<(ViewPair, u32), &'static str> {
    if wallet_row.network_name != network.name() {
        return Err("wallet network differs from the chain's");
    }
    let major_index = u32::try_from(wallet_row.major_index).map_err(|_| "negative major index")?;
    let primary_address = parse_primary_address(network, &wallet_row.primary_address)
        .ok_or("primary address is not a standard address of the chain's network")?;
    let private_view_key_hex = view_key_opener
        .open_private_view_key_hex(
            &wallet_row.sealed_private_view_key,
            wallet_row.account_id,
            chain_key,
            &wallet_row.primary_address,
        )
        .ok_or("sealed view key does not open under this seed and row")?;
    let private_view_key_bytes =
        decode_private_view_key_hex(&private_view_key_hex).ok_or("opened view key is not 64 hex characters")?;
    let view_pair = build_matching_view_pair(&primary_address, &private_view_key_bytes)
        .ok_or("view key does not match the primary address")?;
    Ok((view_pair, major_index))
}

/// The outputs `scanner` finds in `scannable_block`, unlocked ones only, labelled with the wallet and its major index.
fn collect_received_outputs(
    scanner: &mut Scanner,
    wallet_id: i64,
    major_index: u32,
    scannable_block: &ScannableBlock,
) -> Result<Vec<ReceivedOutput>, String> {
    let timelocked_outputs = scanner
        .scan(scannable_block.clone())
        .map_err(|error| error.to_string())?;
    Ok(timelocked_outputs
        .not_additionally_locked()
        .iter()
        .map(|wallet_output| {
            let minor_index = wallet_output
                .subaddress()
                .filter(|subaddress_index| subaddress_index.account() == major_index)
                .map(|subaddress_index| subaddress_index.address());
            ReceivedOutput {
                wallet_id,
                major_index,
                minor_index,
                transaction_hash: wallet_output.transaction(),
                output_public_key: wallet_output.key().compress().to_bytes(),
                output_index: wallet_output.index_in_transaction(),
                amount_piconero: wallet_output.commitment().amount,
            }
        })
        .collect())
}

/// The outputs of `scannable_block` on subaddress (`major_index`, `minor_index`) of `view_pair`'s wallet, with a
/// scanner that knows that one subaddress (a transaction check or a cross-check; the block holds one transaction).
pub fn scan_for_single_minor(
    view_pair: &ViewPair,
    wallet_id: i64,
    major_index: u32,
    minor_index: u32,
    scannable_block: &ScannableBlock,
) -> Result<Vec<ReceivedOutput>, String> {
    let subaddress_index =
        SubaddressIndex::new(major_index, minor_index).ok_or_else(|| "(0, 0) is not a subaddress".to_owned())?;
    let mut scanner = Scanner::new(view_pair.clone());
    scanner.register_subaddress(subaddress_index);
    let received_outputs = collect_received_outputs(&mut scanner, wallet_id, major_index, scannable_block)?;
    Ok(received_outputs
        .into_iter()
        .filter(|received_output| received_output.minor_index == Some(minor_index))
        .collect())
}

pub struct WalletScanners {
    chain_key: String,
    network: MoneroNetwork,
    scanners_by_wallet_id: BTreeMap<i64, WalletScanner>,
    unusable_wallet_ids: BTreeSet<i64>,
}

impl WalletScanners {
    pub fn new(chain_key: &str, network: MoneroNetwork) -> Self {
        Self {
            chain_key: chain_key.to_owned(),
            network,
            scanners_by_wallet_id: BTreeMap::new(),
            unusable_wallet_ids: BTreeSet::new(),
        }
    }

    pub fn wallet_count(&self) -> usize {
        self.scanners_by_wallet_id.len()
    }

    /// Watched wallets the last refresh could not scan for.
    pub fn unusable_wallet_count(&self) -> usize {
        self.unusable_wallet_ids.len()
    }

    pub fn find_wallet_scanner(&self, wallet_id: i64) -> Option<&WalletScanner> {
        self.scanners_by_wallet_id.get(&wallet_id)
    }

    /// Brings the scanners in line with the watched rows: new or changed rows get a scanner (the sealed key opened and
    /// checked against the address), grown `next_minor_index` registers more minors, unwatched wallets are dropped.
    /// A row that cannot become a scanner is retried, logged (`monero_wallet_unusable`) and counted on every refresh.
    pub fn refresh_from_watched_wallets(
        &mut self,
        wallet_rows: &[WatchedWalletRow],
        view_key_opener: &PrivateViewKeyOpener,
    ) {
        let watched_wallet_ids: BTreeSet<i64> = wallet_rows.iter().map(|wallet_row| wallet_row.wallet_id).collect();
        self.scanners_by_wallet_id
            .retain(|wallet_id, _| watched_wallet_ids.contains(wallet_id));
        self.unusable_wallet_ids.clear();
        for wallet_row in wallet_rows {
            let row_fingerprint = fingerprint_wallet_row(wallet_row);
            let wanted_through_minor =
                (wallet_row.next_minor_index.max(1) as u32).saturating_add(SUBADDRESS_LOOKAHEAD_PAST_NEXT_MINOR);
            if let Some(wallet_scanner) = self.scanners_by_wallet_id.get_mut(&wallet_row.wallet_id) {
                if wallet_scanner.row_fingerprint == row_fingerprint {
                    wallet_scanner.register_minors_through(wanted_through_minor);
                    continue;
                }
            }
            self.scanners_by_wallet_id.remove(&wallet_row.wallet_id);
            match open_wallet_view_pair(wallet_row, &self.chain_key, self.network, view_key_opener) {
                Ok((view_pair, major_index)) => {
                    let mut wallet_scanner = WalletScanner {
                        wallet_id: wallet_row.wallet_id,
                        major_index,
                        scanner: Scanner::new(view_pair.clone()),
                        view_pair,
                        registered_through_minor: 0,
                        row_fingerprint,
                    };
                    wallet_scanner.register_minors_through(wanted_through_minor);
                    tracing::info!(
                        event = "monero_wallet_scanner_ready",
                        chain_key = %self.chain_key,
                        wallet_id = wallet_row.wallet_id,
                        account_id = wallet_row.account_id,
                        registered_through_minor = wanted_through_minor,
                    );
                    self.scanners_by_wallet_id.insert(wallet_row.wallet_id, wallet_scanner);
                }
                Err(reason) => {
                    tracing::error!(
                        event = "monero_wallet_unusable",
                        chain_key = %self.chain_key,
                        wallet_id = wallet_row.wallet_id,
                        account_id = wallet_row.account_id,
                        reason,
                    );
                    self.unusable_wallet_ids.insert(wallet_row.wallet_id);
                }
            }
        }
    }

    /// Every wallet's received outputs in each block, in block order, on tokio's blocking pool. Stops at the first
    /// block a wallet fails to scan: the outputs of the blocks before it, and the error. A failed block is never
    /// half-scanned for the caller (monero-wallet 0.2.0 refuses blocks past hard fork 16, for one).
    pub async fn scan_blocks(
        &mut self,
        scannable_blocks: Vec<ScannableBlock>,
    ) -> (Vec<(ScannableBlock, Vec<ReceivedOutput>)>, Option<String>) {
        let mut scanners_by_wallet_id = std::mem::take(&mut self.scanners_by_wallet_id);
        let chain_key = self.chain_key.clone();
        let scan_job = tokio::task::spawn_blocking(move || {
            let mut scanned_blocks = Vec::with_capacity(scannable_blocks.len());
            let mut scan_error = None;
            'blocks: for scannable_block in scannable_blocks {
                let mut block_outputs = Vec::new();
                for wallet_scanner in scanners_by_wallet_id.values_mut() {
                    match collect_received_outputs(
                        &mut wallet_scanner.scanner,
                        wallet_scanner.wallet_id,
                        wallet_scanner.major_index,
                        &scannable_block,
                    ) {
                        Ok(outputs) => block_outputs.extend(outputs),
                        Err(error) => {
                            tracing::error!(
                                event = "monero_block_scan_failed",
                                chain_key = %chain_key,
                                wallet_id = wallet_scanner.wallet_id,
                                block_height = scannable_block.block.number() as u64,
                                error = %error,
                            );
                            scan_error = Some(format!(
                                "wallet {} could not scan block {}: {error}",
                                wallet_scanner.wallet_id,
                                scannable_block.block.number()
                            ));
                            break 'blocks;
                        }
                    }
                }
                scanned_blocks.push((scannable_block, block_outputs));
            }
            (scanners_by_wallet_id, scanned_blocks, scan_error)
        });
        match scan_job.await {
            Ok((scanners_by_wallet_id, scanned_blocks, scan_error)) => {
                self.scanners_by_wallet_id = scanners_by_wallet_id;
                (scanned_blocks, scan_error)
            }
            // Unreachable with `panic = "abort"`; the next refresh rebuilds the scanners.
            Err(join_error) => (Vec::new(), Some(format!("the scan task ended: {join_error}"))),
        }
    }
}

impl WalletScanner {
    fn register_minors_through(&mut self, through_minor: u32) {
        while self.registered_through_minor < through_minor {
            self.registered_through_minor += 1;
            let subaddress_index = SubaddressIndex::new(self.major_index, self.registered_through_minor)
                .expect("minor index 1 or more is never (0, 0)");
            self.scanner.register_subaddress(subaddress_index);
        }
    }
}

/// A scannable block of `transactions` only, under a real block's header and miner transaction (monero-wallet scans
/// whole blocks; its per-transaction scan is private). Used for pool transactions and for re-decoding one
/// transaction from another operator's copy. The miner transaction's outputs are scanned too, so callers keep only
/// outputs of the transactions they passed. The output index on the chain is not known here and not used.
pub fn wrap_transactions_as_scannable_block(
    carrier_block: &Block,
    transactions: Vec<([u8; 32], Transaction<Pruned>)>,
) -> Option<ScannableBlock> {
    let (transaction_hashes, pruned_transactions): (Vec<[u8; 32]>, Vec<Transaction<Pruned>>) =
        transactions.into_iter().unzip();
    let block = Block::new(
        carrier_block.header.clone(),
        carrier_block.miner_transaction().clone(),
        transaction_hashes,
    )?;
    Some(ScannableBlock {
        block,
        transactions: pruned_transactions,
        output_index_for_first_ringct_output: Some(0),
    })
}
