//! Settings from the environment and the Monero chains of `backend/chains.toml` (mounted read-only).
//!
//! `PAY_MONERO_NODES_<CHAIN>` overrides a chain's `nodes` exactly as `read_monero_nodes_override` in
//! `backend/app/payments/chains/configuration.py` does: comma-separated `url=operator` pairs, mainnet `https://` only,
//! stagenet `http://` too, no query; errors name an entry by its position, never its text (a URL may carry
//! credentials).

use std::collections::{BTreeMap, BTreeSet};
use std::net::SocketAddr;

use serde::Deserialize;
use zeroize::Zeroizing;

use crate::monero_keys::MoneroNetwork;

pub const DEFAULT_LISTEN_ADDRESS: &str = "0.0.0.0:8750";
pub const DEFAULT_CHAINS_TOML_PATH: &str = "/app/chains.toml";
const MINIMUM_NODE_OPERATORS: usize = 2;
/// A replaced wallet stays watched this long after its last invoice closed, so a late payment is still found (the
/// Python side's `recent_deposit_window_seconds` default for a Monero chain).
const DEFAULT_RECENT_DEPOSIT_WINDOW_SECONDS: u64 = 86_400;

#[derive(Clone, Debug)]
pub struct MoneroNodeConfiguration {
    pub url: String,
    pub operator: String,
}

#[derive(Clone, Debug)]
pub struct MoneroChainConfiguration {
    pub chain_key: String,
    pub network: MoneroNetwork,
    pub nodes: Vec<MoneroNodeConfiguration>,
    pub confirmations_final: u64,
    pub poll_interval_seconds: u64,
    pub recent_deposit_window_seconds: u64,
}

pub struct ScannerSettings {
    pub listen_address: SocketAddr,
    pub chains: Vec<MoneroChainConfiguration>,
    /// `DATABASE_URL`, with SQLAlchemy's `+psycopg` driver suffix dropped. Never logged.
    pub database_url: Option<Zeroizing<String>>,
    /// `PAY_INVOICE_SALT_SEED` as bytes. Never logged.
    pub invoice_salt_seed: Option<Zeroizing<Vec<u8>>>,
}

#[derive(Deserialize)]
struct ChainsFile {
    chains: BTreeMap<String, toml::Table>,
}

#[derive(Deserialize)]
struct NodeEntry {
    url: String,
    operator: String,
}

#[derive(Deserialize)]
struct MoneroChainTable {
    #[serde(default)]
    kind: Option<String>,
    network: String,
    nodes: Vec<NodeEntry>,
    confirmations_final: u64,
    poll_interval_seconds: u64,
    #[serde(default)]
    recent_deposit_window_seconds: Option<u64>,
}

fn read_environment_variable(name: &str) -> Option<String> {
    std::env::var(name)
        .ok()
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
}

pub fn read_scanner_settings() -> Result<ScannerSettings, String> {
    let listen_address_text =
        read_environment_variable("XMR_SCANNER_LISTEN_ADDRESS").unwrap_or_else(|| DEFAULT_LISTEN_ADDRESS.to_owned());
    let listen_address = listen_address_text
        .parse::<SocketAddr>()
        .map_err(|_| "XMR_SCANNER_LISTEN_ADDRESS is not host:port".to_owned())?;

    let chain_keys: Vec<String> = read_environment_variable("XMR_SCANNER_CHAINS")
        .unwrap_or_default()
        .split(',')
        .map(|chain_key| chain_key.trim().to_owned())
        .filter(|chain_key| !chain_key.is_empty())
        .collect();
    let chains = if chain_keys.is_empty() {
        Vec::new()
    } else {
        let chains_toml_path = read_environment_variable("XMR_SCANNER_CHAINS_TOML_PATH")
            .unwrap_or_else(|| DEFAULT_CHAINS_TOML_PATH.to_owned());
        let chains_toml_text = std::fs::read_to_string(&chains_toml_path)
            .map_err(|error| format!("cannot read {chains_toml_path}: {error}"))?;
        read_monero_chains(&chains_toml_text, &chain_keys)?
    };

    let database_url = read_environment_variable("DATABASE_URL").map(|database_url| {
        Zeroizing::new(
            database_url
                .replacen("postgresql+psycopg://", "postgresql://", 1)
                .replacen("postgres+psycopg://", "postgresql://", 1),
        )
    });
    if !chains.is_empty() && database_url.is_none() {
        return Err("DATABASE_URL is required when XMR_SCANNER_CHAINS names a chain".to_owned());
    }
    let invoice_salt_seed = match read_environment_variable("PAY_INVOICE_SALT_SEED") {
        Some(seed_hex) => Some(Zeroizing::new(
            hex::decode(seed_hex.as_bytes()).map_err(|_| "PAY_INVOICE_SALT_SEED is not hex".to_owned())?,
        )),
        None => None,
    };
    if !chains.is_empty() && invoice_salt_seed.is_none() {
        return Err("PAY_INVOICE_SALT_SEED is required when XMR_SCANNER_CHAINS names a chain".to_owned());
    }

    Ok(ScannerSettings {
        listen_address,
        chains,
        database_url,
        invoice_salt_seed,
    })
}

fn read_monero_chains(chains_toml_text: &str, chain_keys: &[String]) -> Result<Vec<MoneroChainConfiguration>, String> {
    let chains_file: ChainsFile =
        toml::from_str(chains_toml_text).map_err(|error| format!("chains.toml is unreadable: {error}"))?;
    let mut chains = Vec::new();
    for chain_key in chain_keys {
        let table = chains_file
            .chains
            .get(chain_key)
            .ok_or_else(|| format!("chains.{chain_key} is not in chains.toml"))?;
        let chain_table: MoneroChainTable = table
            .clone()
            .try_into()
            .map_err(|error| format!("chains.{chain_key} is not a Monero chain table: {error}"))?;
        if chain_table.kind.as_deref() != Some("monero") {
            return Err(format!("chains.{chain_key} is not kind = \"monero\""));
        }
        let network = MoneroNetwork::from_name(&chain_table.network)
            .ok_or_else(|| format!("chains.{chain_key}: network must be mainnet or stagenet"))?;
        let nodes_override = read_monero_nodes_override(chain_key, network)?;
        let nodes = if nodes_override.is_empty() {
            chain_table
                .nodes
                .into_iter()
                .map(|node| MoneroNodeConfiguration {
                    url: node.url,
                    operator: node.operator,
                })
                .collect()
        } else {
            nodes_override
        };
        let operators: BTreeSet<&str> = nodes.iter().map(|node| node.operator.as_str()).collect();
        if operators.len() < MINIMUM_NODE_OPERATORS {
            return Err(format!(
                "chains.{chain_key}: nodes must come from at least {MINIMUM_NODE_OPERATORS} operators"
            ));
        }
        if chain_table.confirmations_final == 0 || chain_table.poll_interval_seconds == 0 {
            return Err(format!(
                "chains.{chain_key}: confirmations_final and poll_interval_seconds must be positive"
            ));
        }
        chains.push(MoneroChainConfiguration {
            chain_key: chain_key.clone(),
            network,
            nodes,
            confirmations_final: chain_table.confirmations_final,
            poll_interval_seconds: chain_table.poll_interval_seconds,
            recent_deposit_window_seconds: chain_table
                .recent_deposit_window_seconds
                .unwrap_or(DEFAULT_RECENT_DEPOSIT_WINDOW_SECONDS),
        });
    }
    Ok(chains)
}

fn read_monero_nodes_override(chain_key: &str, network: MoneroNetwork) -> Result<Vec<MoneroNodeConfiguration>, String> {
    let variable_name = format!("PAY_MONERO_NODES_{}", chain_key.to_uppercase());
    let allowed_schemes: &[&str] = match network {
        MoneroNetwork::Mainnet => &["https"],
        MoneroNetwork::Stagenet => &["http", "https"],
    };
    let mut nodes = Vec::new();
    let raw_value = std::env::var(&variable_name).unwrap_or_default();
    for (position, entry) in raw_value
        .split(',')
        .enumerate()
        .map(|(index, entry)| (index + 1, entry))
    {
        if entry.trim().is_empty() {
            continue;
        }
        let Some((node_url, operator)) = entry.rsplit_once('=') else {
            return Err(format!("{variable_name}: entry {position} is not url=operator"));
        };
        let (node_url, operator) = (node_url.trim(), operator.trim());
        if node_url.is_empty() || operator.is_empty() {
            return Err(format!("{variable_name}: entry {position} is not url=operator"));
        }
        let url_is_allowed = node_url.split_once("://").is_some_and(|(scheme, rest)| {
            let authority = rest.split('/').next().unwrap_or("");
            let host_and_port = authority.rsplit('@').next().unwrap_or("");
            let host = host_and_port.split(':').next().unwrap_or("");
            allowed_schemes.contains(&scheme) && !rest.contains('?') && !host.is_empty()
        });
        if !url_is_allowed {
            return Err(format!(
                "{variable_name}: entry {position} needs a {} URL with a host and no query",
                allowed_schemes.join(" or ")
            ));
        }
        nodes.push(MoneroNodeConfiguration {
            url: node_url.to_owned(),
            operator: operator.to_owned(),
        });
    }
    Ok(nodes)
}
