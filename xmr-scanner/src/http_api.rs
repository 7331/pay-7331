//! The HTTP contract the Python API codes against. Bodies are parsed here from raw bytes, not by an extractor, so no
//! error message can echo an input (a view key above all); nothing logs a request body.
//!
//! - `GET /healthz` -> 200 `{"ok", "chains": [{"chain_key", "ok", "node_count", "healthy_operator_count",
//!   "tip_height", "confirmed_tip_height", "scanned_height", "last_completed_tick_at", "unusable_wallet_count"}]}`
//!   (`chain_health.rs`); `ok` is false when any chain is not ok. Always 200 while the process serves: the Python
//!   side reads the fields, and the container probe (`xmr-scanner healthcheck`) reads `ok`.
//! - `POST /v1/wallet-checks` `{"network", "primary_address", "private_view_key"}` -> 200
//!   `{"address_valid", "view_key_matches"}`
//! - `POST /v1/subaddresses` `{"network", "primary_address", "private_view_key", "major_index", "minor_index"}` -> 200
//!   `{"subaddress"}`
//! - `POST /v1/transaction-checks` `{"chain_key", "txid", "invoice_id"}` -> 200 `{"outcome", "amount_piconero",
//!   "confirmations"}` (transaction_checks.rs)
//!
//! Errors are 400 `{"error": "..."}` with a fixed message; a database error during a transaction check is 500.

use std::collections::BTreeMap;
use std::sync::Arc;

use axum::body::Bytes;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::json;
use tokio::sync::Mutex;
use zeroize::Zeroizing;

use crate::chain_health::ChainHealthRegistry;
use crate::monero_keys::{
    build_matching_view_pair, decode_private_view_key_hex, derive_subaddress, parse_primary_address, MoneroNetwork,
};
use crate::transaction_checks::{
    TransactionCheckAnswer, TransactionCheckOutcome, TransactionChecker, TRANSACTION_CHECK_TIME_LIMIT,
};

/// What the handlers share: the health snapshots and one transaction checker per scanned chain.
pub struct HttpApiState {
    pub chain_health_registry: Arc<ChainHealthRegistry>,
    pub transaction_checkers_by_chain_key: BTreeMap<String, Mutex<TransactionChecker>>,
}

type SharedState = axum::extract::State<Arc<HttpApiState>>;

/// An invoice id is `inv_` and a short random text; anything longer is not one.
const INVOICE_ID_MAXIMUM_LENGTH: usize = 64;

#[derive(Deserialize)]
struct TransactionCheckRequest {
    chain_key: String,
    txid: String,
    invoice_id: String,
}

#[derive(Deserialize)]
struct WalletCheckRequest {
    network: String,
    primary_address: String,
    private_view_key: Zeroizing<String>,
}

#[derive(Deserialize)]
struct SubaddressRequest {
    network: String,
    primary_address: String,
    private_view_key: Zeroizing<String>,
    major_index: u32,
    minor_index: u32,
}

fn build_error_response(message: &'static str) -> Response {
    (StatusCode::BAD_REQUEST, Json(json!({ "error": message }))).into_response()
}

pub fn build_router(http_api_state: Arc<HttpApiState>) -> Router {
    Router::new()
        .route("/healthz", get(read_health))
        .route("/v1/wallet-checks", post(check_wallet))
        .route("/v1/subaddresses", post(create_subaddress))
        .route("/v1/transaction-checks", post(check_transaction))
        .with_state(http_api_state)
}

async fn read_health(axum::extract::State(http_api_state): SharedState) -> Response {
    let chains = http_api_state.chain_health_registry.report_all_chains();
    let every_chain_ok = chains.iter().all(|chain| chain.ok);
    (StatusCode::OK, Json(json!({ "ok": every_chain_ok, "chains": chains }))).into_response()
}

async fn check_wallet(body: Bytes) -> Response {
    let Ok(request) = serde_json::from_slice::<WalletCheckRequest>(&body) else {
        return build_error_response("body must be {network, primary_address, private_view_key}");
    };
    let Some(network) = MoneroNetwork::from_name(&request.network) else {
        return build_error_response("network must be mainnet or stagenet");
    };
    let Some(private_view_key_bytes) = decode_private_view_key_hex(&request.private_view_key) else {
        return build_error_response("private_view_key must be 64 hex characters");
    };
    let primary_address = parse_primary_address(network, &request.primary_address);
    let view_key_matches = primary_address
        .as_ref()
        .is_some_and(|primary_address| build_matching_view_pair(primary_address, &private_view_key_bytes).is_some());
    (
        StatusCode::OK,
        Json(json!({ "address_valid": primary_address.is_some(), "view_key_matches": view_key_matches })),
    )
        .into_response()
}

async fn create_subaddress(body: Bytes) -> Response {
    let Ok(request) = serde_json::from_slice::<SubaddressRequest>(&body) else {
        return build_error_response(
            "body must be {network, primary_address, private_view_key, major_index, minor_index}",
        );
    };
    let Some(network) = MoneroNetwork::from_name(&request.network) else {
        return build_error_response("network must be mainnet or stagenet");
    };
    let Some(private_view_key_bytes) = decode_private_view_key_hex(&request.private_view_key) else {
        return build_error_response("private_view_key must be 64 hex characters");
    };
    let Some(primary_address) = parse_primary_address(network, &request.primary_address) else {
        return build_error_response("primary_address is not a standard address of this network");
    };
    // A subaddress from a view key that does not match would be one nobody can spend: refuse rather than burn.
    let Some(view_pair) = build_matching_view_pair(&primary_address, &private_view_key_bytes) else {
        return build_error_response("private_view_key does not match primary_address");
    };
    let Some(subaddress) = derive_subaddress(&view_pair, network, request.major_index, request.minor_index) else {
        return build_error_response("major_index 0 with minor_index 0 is the primary address, not a subaddress");
    };
    (StatusCode::OK, Json(json!({ "subaddress": subaddress }))).into_response()
}

async fn check_transaction(axum::extract::State(http_api_state): SharedState, body: Bytes) -> Response {
    let Ok(request) = serde_json::from_slice::<TransactionCheckRequest>(&body) else {
        return build_error_response("body must be {chain_key, txid, invoice_id}");
    };
    let mut transaction_hash = [0u8; 32];
    let txid_is_lowercase_hex = request.txid.len() == 64
        && request
            .txid
            .bytes()
            .all(|character| character.is_ascii_digit() || (b'a'..=b'f').contains(&character));
    if !txid_is_lowercase_hex || hex::decode_to_slice(&request.txid, &mut transaction_hash).is_err() {
        return build_error_response("txid must be 64 lowercase hex characters");
    }
    if request.invoice_id.is_empty() || request.invoice_id.len() > INVOICE_ID_MAXIMUM_LENGTH {
        return build_error_response("invoice_id is not an invoice id");
    }
    let Some(transaction_checker) = http_api_state.transaction_checkers_by_chain_key.get(&request.chain_key) else {
        return build_error_response("chain_key is not a chain this sidecar scans");
    };
    // One deadline for waiting on the chain's lock and for the check itself.
    let deadline = tokio::time::Instant::now() + TRANSACTION_CHECK_TIME_LIMIT;
    let check_outcome = match tokio::time::timeout_at(deadline, transaction_checker.lock()).await {
        Ok(mut locked_transaction_checker) => {
            let check_result = tokio::time::timeout_at(
                deadline,
                locked_transaction_checker.check_transaction(&request.invoice_id, transaction_hash),
            )
            .await;
            if check_result.is_err() {
                locked_transaction_checker.reset_after_time_limit();
            }
            check_result.ok()
        }
        Err(_) => None,
    };
    let answer = match check_outcome {
        Some(Ok(Some(answer))) => answer,
        Some(Ok(None)) => return build_error_response("invoice_id is not a Monero invoice of this chain"),
        Some(Err(database_error)) => {
            tracing::error!(
                event = "monero_transaction_check_failed",
                chain_key = %request.chain_key,
                invoice_id = %request.invoice_id,
                error = %database_error,
            );
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({ "error": "database error" })),
            )
                .into_response();
        }
        // Out of time, waiting for the lock or inside the check: nothing was proven, the payer retries.
        None => TransactionCheckAnswer::without_amount(TransactionCheckOutcome::Unavailable),
    };
    tracing::info!(
        event = "monero_transaction_checked",
        chain_key = %request.chain_key,
        invoice_id = %request.invoice_id,
        transaction_hash = %request.txid,
        outcome = ?answer.outcome,
        confirmations = answer.confirmations,
    );
    (StatusCode::OK, Json(answer)).into_response()
}
