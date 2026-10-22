//! pay-7331's Monero sidecar (PLAN.md section 14): the wallet checks and subaddresses the Python API asks for over
//! HTTP, and one scan loop per chain in `XMR_SCANNER_CHAINS` writing `monero_payments`. Logs are JSON lines with ids
//! and counts only: never a view key, a sealed blob, a database URL, a seed or a request body.

mod chain_health;
mod chain_scanner;
mod configuration;
mod cross_check;
mod http_api;
mod monero_keys;
mod node_pool;
mod payments_store;
mod transaction_checks;
mod view_key_sealing;
mod wallet_scanners;

use std::collections::BTreeMap;
use std::sync::Arc;

use crate::chain_health::ChainHealthRegistry;
use crate::chain_scanner::ChainScanner;
use crate::configuration::read_scanner_settings;
use crate::http_api::HttpApiState;
use crate::payments_store::PaymentsStore;
use crate::transaction_checks::TransactionChecker;
use crate::view_key_sealing::PrivateViewKeyOpener;

fn initialize_json_logging() {
    tracing_subscriber::fmt()
        .json()
        .flatten_event(true)
        .with_current_span(false)
        .with_span_list(false)
        .with_target(false)
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();
}

async fn wait_for_shutdown_signal() {
    let interrupt = tokio::signal::ctrl_c();
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        .expect("installing a SIGTERM handler succeeds on unix");
    tokio::select! {
        _ = interrupt => {},
        _ = terminate.recv() => {},
    }
}

/// `xmr-scanner healthcheck`: the container's probe (the runtime image has no curl). Exits 0 when `/healthz` on the
/// listen port answers 200 with `"ok": true` (every chain completed a tick within 3 poll intervals and has no
/// unusable wallet).
async fn run_healthcheck() -> ! {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let listen_address = std::env::var("XMR_SCANNER_LISTEN_ADDRESS")
        .ok()
        .and_then(|address| address.parse::<std::net::SocketAddr>().ok())
        .unwrap_or_else(|| {
            configuration::DEFAULT_LISTEN_ADDRESS
                .parse()
                .expect("the default parses")
        });
    let probe_address = std::net::SocketAddr::from(([127, 0, 0, 1], listen_address.port()));
    let probe = async {
        let mut stream = tokio::net::TcpStream::connect(probe_address).await.ok()?;
        stream
            .write_all(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            .await
            .ok()?;
        let mut response = Vec::new();
        stream.read_to_end(&mut response).await.ok()?;
        if !response.starts_with(b"HTTP/1.1 200") {
            return Some(false);
        }
        let body_start = response.windows(4).position(|window| window == b"\r\n\r\n")? + 4;
        let health: serde_json::Value = serde_json::from_slice(&response[body_start..]).ok()?;
        Some(health.get("ok") == Some(&serde_json::Value::Bool(true)))
    };
    let healthy = tokio::time::timeout(std::time::Duration::from_secs(4), probe)
        .await
        .ok()
        .flatten()
        == Some(true);
    std::process::exit(if healthy { 0 } else { 1 });
}

#[tokio::main]
async fn main() {
    if std::env::args().nth(1).as_deref() == Some("healthcheck") {
        run_healthcheck().await;
    }
    initialize_json_logging();
    let settings = match read_scanner_settings() {
        Ok(settings) => settings,
        Err(settings_error) => {
            tracing::error!(event = "xmr_scanner_settings_invalid", error = %settings_error);
            std::process::exit(2);
        }
    };

    let chain_health_registry = Arc::new(ChainHealthRegistry::default());
    let mut transaction_checkers_by_chain_key = BTreeMap::new();
    if let (Some(database_url), Some(invoice_salt_seed)) = (&settings.database_url, &settings.invoice_salt_seed) {
        let view_key_opener = Arc::new(PrivateViewKeyOpener::from_invoice_salt_seed(invoice_salt_seed));
        for chain in settings.chains.iter().cloned() {
            transaction_checkers_by_chain_key.insert(
                chain.chain_key.clone(),
                tokio::sync::Mutex::new(TransactionChecker::new(
                    chain.clone(),
                    PaymentsStore::new(database_url.clone()),
                    view_key_opener.clone(),
                )),
            );
            let chain_scanner = ChainScanner::new(
                chain,
                PaymentsStore::new(database_url.clone()),
                view_key_opener.clone(),
                chain_health_registry.clone(),
            );
            tokio::spawn(chain_scanner.run_forever());
        }
    }

    let listener = match tokio::net::TcpListener::bind(settings.listen_address).await {
        Ok(listener) => listener,
        Err(bind_error) => {
            tracing::error!(event = "xmr_scanner_bind_failed", listen_address = %settings.listen_address, error = %bind_error);
            std::process::exit(1);
        }
    };
    tracing::info!(
        event = "xmr_scanner_listening",
        listen_address = %settings.listen_address,
        chain_count = settings.chains.len(),
    );
    let router = http_api::build_router(Arc::new(HttpApiState {
        chain_health_registry,
        transaction_checkers_by_chain_key,
    }));
    if let Err(serve_error) = axum::serve(listener, router)
        .with_graceful_shutdown(wait_for_shutdown_signal())
        .await
    {
        tracing::error!(event = "xmr_scanner_server_failed", error = %serve_error);
        std::process::exit(1);
    }
    tracing::info!(event = "xmr_scanner_stopped");
}
