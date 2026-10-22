//! Postgres reads and writes of the sidecar. The Python side owns the schema (migrations 0023 and 0024); this writes
//! `monero_payments` rows and `monero_scan_cursors`, and never `credited_at`, which is the Python watcher's. Amounts cross as text into
//! `numeric(78,0)`, cross-check states as text into the `monero_cross_check_state` enum.
//!
//! Database errors are logged by SQLSTATE only: a message can carry row values.

use tokio_postgres::{Client, NoTls};
use zeroize::Zeroizing;

pub struct WatchedWalletRow {
    pub wallet_id: i64,
    pub account_id: i64,
    pub primary_address: String,
    /// AES-GCM sealed (view_key_sealing.rs); never logged.
    pub sealed_private_view_key: Zeroizing<Vec<u8>>,
    pub network_name: String,
    pub major_index: i32,
    pub next_minor_index: i32,
}

/// An XMR invoice's wallet and subaddress minor, for a transaction check.
pub struct InvoiceWalletRow {
    pub wallet: WatchedWalletRow,
    pub subaddress_minor: u32,
}

/// Where the scan loop stands: the last scanned block, and its hash when a block was fetched.
pub struct ScanCursorRow {
    pub scanned_height: u64,
    pub scanned_block_hash_hex: Option<String>,
}

pub struct ExistingPaymentRow {
    pub transaction_hash_hex: String,
    pub block_height: Option<i64>,
}

pub struct UnsettledBlockPaymentRow {
    pub payment_id: i64,
    pub invoice_id: String,
    pub monero_wallet_id: i64,
    pub transaction_hash_hex: String,
    pub output_public_key_hex: String,
    pub amount_piconero: u64,
    pub block_height: u64,
    pub block_hash_hex: String,
    pub cross_check_state: String,
    pub final_cross_check_state: String,
    pub subaddress_minor: u32,
    /// The confirmations this invoice's output counts final at: the receiver's choice taken onto the invoice when it
    /// was made, else the chain's chains.toml default.
    pub confirmations_final: u64,
}

/// The block a payment was found in.
pub struct PaymentBlock<'block> {
    pub block_height: u64,
    pub block_hash_hex: &'block str,
    /// The block header's timestamp, Unix seconds.
    pub block_timestamp: u64,
}

/// How long a row without a block is still followed after it left the pool.
const UNMINED_PAYMENT_FOLLOW_SECONDS: i64 = 24 * 60 * 60;

pub struct NewPaymentRow<'row> {
    pub chain_key: &'row str,
    pub invoice_id: &'row str,
    pub transaction_hash_hex: &'row str,
    pub output_public_key_hex: &'row str,
    pub output_index: i32,
    pub amount_piconero: u64,
}

pub fn describe_database_error(error: &tokio_postgres::Error) -> String {
    match error.code() {
        Some(state) => format!("database error, SQLSTATE {}", state.code()),
        None if error.is_closed() => "database connection closed".to_owned(),
        None => "database error without SQLSTATE (connection or protocol)".to_owned(),
    }
}

fn read_wallet_columns(row: &tokio_postgres::Row) -> WatchedWalletRow {
    WatchedWalletRow {
        wallet_id: row.get(0),
        account_id: row.get(1),
        primary_address: row.get(2),
        sealed_private_view_key: Zeroizing::new(row.get(3)),
        network_name: row.get(4),
        major_index: row.get(5),
        next_minor_index: row.get(6),
    }
}

pub struct PaymentsStore {
    database_url: Zeroizing<String>,
    client: Option<Client>,
}

impl PaymentsStore {
    pub fn new(database_url: Zeroizing<String>) -> Self {
        Self {
            database_url,
            client: None,
        }
    }

    /// The connected client, reconnecting after a closed connection. The URL is never part of an error message.
    pub async fn connected_client(&mut self) -> Result<&Client, String> {
        if self.client.as_ref().is_some_and(|client| client.is_closed()) {
            self.client = None;
        }
        if self.client.is_none() {
            let (client, connection) = tokio_postgres::connect(self.database_url.as_str(), NoTls)
                .await
                .map_err(|error| describe_database_error(&error))?;
            tokio::spawn(async move {
                if let Err(error) = connection.await {
                    tracing::warn!(event = "database_connection_ended", error = %describe_database_error(&error));
                }
            });
            self.client = Some(client);
        }
        Ok(self.client.as_ref().expect("connected just above"))
    }

    /// The wallets to scan for: every active wallet of the chain, plus every replaced one that still has an open
    /// invoice (`closed_at IS NULL`), since invoices keep the wallet they were made with (PLAN.md section 14), or one
    /// closed within `recent_deposit_window_seconds`, so a late payment to it is still found.
    pub async fn read_watched_wallets(
        &mut self,
        chain_key: &str,
        recent_deposit_window_seconds: u64,
    ) -> Result<Vec<WatchedWalletRow>, String> {
        let rows = self
            .connected_client()
            .await?
            .query(
                "SELECT wallet.id, wallet.account_id, wallet.primary_address, wallet.sealed_private_view_key, \
                 wallet.network::text, wallet.major_index, wallet.next_minor_index \
                 FROM account_monero_wallets AS wallet WHERE wallet.chain_key = $1 AND (wallet.replaced_at IS NULL \
                 OR EXISTS (SELECT 1 FROM invoices AS invoice WHERE invoice.monero_wallet_id = wallet.id \
                 AND (invoice.closed_at IS NULL \
                 OR invoice.closed_at > now() - make_interval(secs => $2::bigint::double precision))))",
                &[&chain_key, &(recent_deposit_window_seconds as i64)],
            )
            .await
            .map_err(|error| describe_database_error(&error))?;
        Ok(rows.iter().map(read_wallet_columns).collect())
    }

    /// The wallet and minor of a Monero invoice of this chain; `None` for an unknown id or an invoice of another kind.
    pub async fn find_invoice_wallet(
        &mut self,
        chain_key: &str,
        invoice_id: &str,
    ) -> Result<Option<InvoiceWalletRow>, String> {
        let row = self
            .connected_client()
            .await?
            .query_opt(
                "SELECT wallet.id, wallet.account_id, wallet.primary_address, wallet.sealed_private_view_key, \
                 wallet.network::text, wallet.major_index, wallet.next_minor_index, invoice.subaddress_minor \
                 FROM invoices AS invoice JOIN account_monero_wallets AS wallet ON wallet.id = invoice.monero_wallet_id \
                 WHERE invoice.id = $1 AND invoice.chain_key = $2 AND wallet.chain_key = $2",
                &[&invoice_id, &chain_key],
            )
            .await
            .map_err(|error| describe_database_error(&error))?;
        Ok(row.and_then(|row| {
            let subaddress_minor = u32::try_from(row.get::<_, i32>(7)).ok()?;
            Some(InvoiceWalletRow {
                wallet: read_wallet_columns(&row),
                subaddress_minor,
            })
        }))
    }

    /// The heights (None: pool only) and amounts of this invoice's rows of one transaction.
    pub async fn read_invoice_transaction_payments(
        &mut self,
        chain_key: &str,
        invoice_id: &str,
        transaction_hash_hex: &str,
    ) -> Result<Vec<(Option<u64>, u64)>, String> {
        let rows = self
            .connected_client()
            .await?
            .query(
                "SELECT block_height, amount_piconero::text FROM monero_payments \
                 WHERE chain_key = $1 AND invoice_id = $2 AND txid = $3",
                &[&chain_key, &invoice_id, &transaction_hash_hex],
            )
            .await
            .map_err(|error| describe_database_error(&error))?;
        rows.iter()
            .map(|row| {
                let amount_text: String = row.get(1);
                let amount_piconero = amount_text
                    .parse()
                    .map_err(|_| "amount_piconero is not a u64".to_owned())?;
                Ok((
                    row.get::<_, Option<i64>>(0).map(|height| height as u64),
                    amount_piconero,
                ))
            })
            .collect()
    }

    pub async fn read_scan_cursor(&mut self, chain_key: &str) -> Result<Option<ScanCursorRow>, String> {
        let row = self
            .connected_client()
            .await?
            .query_opt(
                "SELECT scanned_height, scanned_block_hash FROM monero_scan_cursors WHERE chain_key = $1",
                &[&chain_key],
            )
            .await
            .map_err(|error| describe_database_error(&error))?;
        Ok(row.map(|row| ScanCursorRow {
            scanned_height: row.get::<_, i64>(0) as u64,
            scanned_block_hash_hex: row.get(1),
        }))
    }

    /// Upserts the cursor: forward each tick, back when a block vanished.
    pub async fn write_scan_cursor(
        &mut self,
        chain_key: &str,
        scanned_height: u64,
        scanned_block_hash_hex: Option<&str>,
    ) -> Result<(), String> {
        self.connected_client()
            .await?
            .execute(
                "INSERT INTO monero_scan_cursors (chain_key, scanned_height, scanned_block_hash, updated_at) \
                 VALUES ($1, $2, $3, now()) ON CONFLICT (chain_key) DO UPDATE SET \
                 scanned_height = EXCLUDED.scanned_height, scanned_block_hash = EXCLUDED.scanned_block_hash, \
                 updated_at = now()",
                &[&chain_key, &(scanned_height as i64), &scanned_block_hash_hex],
            )
            .await
            .map_err(|error| describe_database_error(&error))?;
        Ok(())
    }

    pub async fn find_invoice_id_by_subaddress(
        &mut self,
        monero_wallet_id: i64,
        subaddress_minor: u32,
    ) -> Result<Option<String>, String> {
        let row = self
            .connected_client()
            .await?
            .query_opt(
                "SELECT id FROM invoices WHERE monero_wallet_id = $1 AND subaddress_minor = $2",
                &[&monero_wallet_id, &(subaddress_minor as i32)],
            )
            .await
            .map_err(|error| describe_database_error(&error))?;
        Ok(row.map(|row| row.get(0)))
    }

    pub async fn find_payment_by_output_public_key(
        &mut self,
        chain_key: &str,
        output_public_key_hex: &str,
    ) -> Result<Option<ExistingPaymentRow>, String> {
        let row = self
            .connected_client()
            .await?
            .query_opt(
                "SELECT txid, block_height FROM monero_payments WHERE chain_key = $1 AND output_public_key = $2",
                &[&chain_key, &output_public_key_hex],
            )
            .await
            .map_err(|error| describe_database_error(&error))?;
        Ok(row.map(|row| ExistingPaymentRow {
            transaction_hash_hex: row.get(0),
            block_height: row.get(1),
        }))
    }

    /// A pool sighting: a new row with no height, `in_pool`. An output already recorded is left alone (dedup by output
    /// public key), except that a row of the same transaction still without a block that had left the pool is marked
    /// back in it (a transaction re-relayed, or back after a reorg). True when a row was written.
    pub async fn insert_pool_payment(
        &mut self,
        payment: &NewPaymentRow<'_>,
        double_spend_seen: bool,
    ) -> Result<bool, String> {
        let inserted_count = self
            .connected_client()
            .await?
            .execute(
                "INSERT INTO monero_payments (chain_key, invoice_id, txid, output_public_key, output_index, \
                 amount_piconero, block_height, block_hash, first_seen_at, in_pool, double_spend_seen) \
                 VALUES ($1, $2, $3, $4, $5, $6::text::numeric, NULL, NULL, now(), true, $7) \
                 ON CONFLICT (chain_key, output_public_key) DO UPDATE SET in_pool = true, \
                 double_spend_seen = EXCLUDED.double_spend_seen, updated_at = now() \
                 WHERE monero_payments.block_height IS NULL AND monero_payments.txid = EXCLUDED.txid \
                 AND NOT monero_payments.in_pool",
                &[
                    &payment.chain_key,
                    &payment.invoice_id,
                    &payment.transaction_hash_hex,
                    &payment.output_public_key_hex,
                    &payment.output_index,
                    &payment.amount_piconero.to_string(),
                    &double_spend_seen,
                ],
            )
            .await
            .map_err(|error| describe_database_error(&error))?;
        Ok(inserted_count == 1)
    }

    /// The output in a block: a new row, or the pool row of the same transaction moved into the block (`in_pool`
    /// cleared). A row that already has a height, or holds this output key under another transaction, is untouched.
    /// A new row's `first_seen_at` is the block's timestamp (the payment is valued at the quote live then, even when
    /// the scan catches up after an outage); a pool row keeps the time it was seen in the pool. True when a row was
    /// written.
    pub async fn write_block_payment(
        &mut self,
        payment: &NewPaymentRow<'_>,
        block: &PaymentBlock<'_>,
        cross_check_state: &str,
        cross_check_operator: Option<&str>,
    ) -> Result<bool, String> {
        let written_count = self
            .connected_client()
            .await?
            .execute(
                "INSERT INTO monero_payments (chain_key, invoice_id, txid, output_public_key, output_index, \
                 amount_piconero, block_height, block_hash, first_seen_at, in_pool, double_spend_seen, \
                 cross_check_state, cross_check_operator) \
                 VALUES ($1, $2, $3, $4, $5, $6::text::numeric, $7, $8, to_timestamp($11::bigint::double precision), \
                 false, false, $9::text::monero_cross_check_state, $10) \
                 ON CONFLICT (chain_key, output_public_key) DO UPDATE SET block_height = EXCLUDED.block_height, \
                 block_hash = EXCLUDED.block_hash, in_pool = false, cross_check_state = EXCLUDED.cross_check_state, \
                 cross_check_operator = EXCLUDED.cross_check_operator, updated_at = now() \
                 WHERE monero_payments.block_height IS NULL AND monero_payments.txid = EXCLUDED.txid",
                &[
                    &payment.chain_key,
                    &payment.invoice_id,
                    &payment.transaction_hash_hex,
                    &payment.output_public_key_hex,
                    &payment.output_index,
                    &payment.amount_piconero.to_string(),
                    &(block.block_height as i64),
                    &block.block_hash_hex,
                    &cross_check_state,
                    &cross_check_operator,
                    &(block.block_timestamp as i64),
                ],
            )
            .await
            .map_err(|error| describe_database_error(&error))?;
        Ok(written_count == 1)
    }

    /// Transaction hashes of the chain's rows that have no block yet and are in the pool, or left it less than
    /// `UNMINED_PAYMENT_FOLLOW_SECONDS` ago (so one node's pool missing it does not end the follow). A row that left the
    /// pool longer ago is no longer read; it keeps `in_pool = false` and is written again if its transaction is seen
    /// in the pool or in a block.
    pub async fn read_unmined_payment_transaction_hashes(&mut self, chain_key: &str) -> Result<Vec<String>, String> {
        let rows = self
            .connected_client()
            .await?
            .query(
                "SELECT DISTINCT txid FROM monero_payments WHERE chain_key = $1 AND block_height IS NULL \
                 AND (in_pool OR updated_at > now() - make_interval(secs => $2::bigint::double precision))",
                &[&chain_key, &UNMINED_PAYMENT_FOLLOW_SECONDS],
            )
            .await
            .map_err(|error| describe_database_error(&error))?;
        Ok(rows.iter().map(|row| row.get(0)).collect())
    }

    /// The pool's view of an unmined transaction: in the pool with this double-spend flag, or gone from it.
    pub async fn update_unmined_payment_pool_state(
        &mut self,
        chain_key: &str,
        transaction_hash_hex: &str,
        in_pool: bool,
        double_spend_seen: Option<bool>,
    ) -> Result<u64, String> {
        self.connected_client()
            .await?
            .execute(
                "UPDATE monero_payments SET in_pool = $3, double_spend_seen = coalesce($4, double_spend_seen), \
                 updated_at = now() WHERE chain_key = $1 AND txid = $2 AND block_height IS NULL \
                 AND (in_pool <> $3 OR double_spend_seen <> coalesce($4, double_spend_seen))",
                &[&chain_key, &transaction_hash_hex, &in_pool, &double_spend_seen],
            )
            .await
            .map_err(|error| describe_database_error(&error))
    }

    /// Rows in a block that still need a look: fewer than their invoice's `confirmations_final` confirmations past
    /// the final one (`block_height > tip - confirmations_final`, so the final confirmation is read too), or a
    /// cross-check still pending. `confirmations_final` is the chain's default, standing in for an invoice without
    /// one of its own.
    pub async fn read_unsettled_block_payments(
        &mut self,
        chain_key: &str,
        tip_height: u64,
        confirmations_final: u64,
    ) -> Result<Vec<UnsettledBlockPaymentRow>, String> {
        let rows = self
            .connected_client()
            .await?
            .query(
                "SELECT payment.id, payment.invoice_id, invoice.monero_wallet_id, payment.txid, payment.output_public_key, \
                 payment.amount_piconero::text, payment.block_height, payment.block_hash, \
                 payment.cross_check_state::text, payment.final_cross_check_state::text, invoice.subaddress_minor, \
                 COALESCE(invoice.confirmations_final, $3::bigint) \
                 FROM monero_payments AS payment JOIN invoices AS invoice ON invoice.id = payment.invoice_id \
                 WHERE payment.chain_key = $1 AND payment.block_height IS NOT NULL \
                 AND invoice.monero_wallet_id IS NOT NULL AND invoice.subaddress_minor IS NOT NULL \
                 AND (payment.block_height > $2::bigint - COALESCE(invoice.confirmations_final, $3::bigint) \
                 OR payment.cross_check_state = 'pending' OR payment.final_cross_check_state = 'pending') \
                 ORDER BY payment.block_height, payment.id",
                &[
                    &chain_key,
                    &(tip_height as i64),
                    &(confirmations_final as i64),
                ],
            )
            .await
            .map_err(|error| describe_database_error(&error))?;
        let mut payments = Vec::with_capacity(rows.len());
        for row in &rows {
            let amount_text: String = row.get(5);
            payments.push(UnsettledBlockPaymentRow {
                payment_id: row.get(0),
                invoice_id: row.get(1),
                monero_wallet_id: row.get(2),
                transaction_hash_hex: row.get(3),
                output_public_key_hex: row.get(4),
                amount_piconero: amount_text
                    .parse()
                    .map_err(|_| "amount_piconero is not a u64".to_owned())?,
                block_height: row.get::<_, i64>(6) as u64,
                block_hash_hex: row.get(7),
                cross_check_state: row.get(8),
                final_cross_check_state: row.get(9),
                subaddress_minor: u32::try_from(row.get::<_, i32>(10))
                    .map_err(|_| "subaddress_minor is negative".to_owned())?,
                confirmations_final: u64::try_from(row.get::<_, i64>(11))
                    .map_err(|_| "confirmations_final is negative".to_owned())?,
            });
        }
        Ok(payments)
    }

    /// A block that vanished from the chain: its rows lose height and hash (the Python watcher uncredits them), and
    /// the cross-checks restart for whichever block includes the transaction next. The rows count as in the pool
    /// again (monerod returns a reorged-out block's transactions to its pool), so the invoice waits instead of
    /// expiring; the next pool read corrects it. The ids of the rows reset.
    pub async fn clear_vanished_block(
        &mut self,
        chain_key: &str,
        block_height: u64,
        block_hash_hex: &str,
    ) -> Result<Vec<i64>, String> {
        let rows = self
            .connected_client()
            .await?
            .query(
                "UPDATE monero_payments SET block_height = NULL, block_hash = NULL, in_pool = true, \
                 cross_check_state = 'pending', final_cross_check_state = 'pending', cross_check_operator = NULL, \
                 updated_at = now() WHERE chain_key = $1 AND block_height = $2 AND block_hash = $3 RETURNING id",
                &[&chain_key, &(block_height as i64), &block_hash_hex],
            )
            .await
            .map_err(|error| describe_database_error(&error))?;
        Ok(rows.iter().map(|row| row.get(0)).collect())
    }

    /// Records a cross-check outcome on a row still in the block it was checked for. `final_check` picks
    /// `final_cross_check_state` (at `confirmations_final`) over `cross_check_state` (before crediting).
    pub async fn record_cross_check(
        &mut self,
        payment_id: i64,
        block_hash_hex: &str,
        final_check: bool,
        cross_check_state: &str,
        cross_check_operator: &str,
    ) -> Result<u64, String> {
        let statement = if final_check {
            "UPDATE monero_payments SET final_cross_check_state = $3::text::monero_cross_check_state, \
             cross_check_operator = $4, updated_at = now() WHERE id = $1 AND block_hash = $2"
        } else {
            "UPDATE monero_payments SET cross_check_state = $3::text::monero_cross_check_state, \
             cross_check_operator = $4, updated_at = now() WHERE id = $1 AND block_hash = $2"
        };
        self.connected_client()
            .await?
            .execute(
                statement,
                &[&payment_id, &block_hash_hex, &cross_check_state, &cross_check_operator],
            )
            .await
            .map_err(|error| describe_database_error(&error))
    }
}
