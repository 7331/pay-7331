//! What `/healthz` reports per chain. The scan loop stores a snapshot after each tick and when it takes a node probe's
//! outcomes; whether
//! the chain is `ok` is decided when the report is read, against the clock.

use std::collections::BTreeMap;
use std::sync::RwLock;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::Serialize;

/// A chain whose last completed tick is older than this many poll intervals is not `ok` (nor is one that has not
/// completed a tick this long after the loop started).
pub const STALE_TICK_POLL_INTERVALS: u32 = 3;

#[derive(Clone, Debug)]
pub struct ChainHealthSnapshot {
    pub chain_key: String,
    pub poll_interval: Duration,
    pub scanner_started_at: Instant,
    pub node_count: usize,
    /// Operators with at least one node whose last call succeeded.
    pub healthy_operator_count: usize,
    /// The tip the last completed tick read from its preferred node; `None` until a tick completed.
    pub tip_height: Option<u64>,
    /// The lower of that tip and the tip a node of another operator reported in the same tick: what final counting
    /// reads, so one node's tip alone never makes a payment final. `None` while no other operator answered.
    pub confirmed_tip_height: Option<u64>,
    /// The last block the scan loop scanned (or followed, with no watched wallet).
    pub scanned_height: Option<u64>,
    pub last_completed_tick_at: Option<(Instant, SystemTime)>,
    /// Watched wallets whose sealed view key does not open or match: the cursor holds while there is one.
    pub unusable_wallet_count: usize,
    /// Since when completed ticks have seen the confirmed tip more than 3 blocks under the tip, or unknown.
    pub confirmed_tip_lagging_since: Option<SystemTime>,
}

/// One chain of `GET /healthz`.
#[derive(Debug, Serialize)]
pub struct ChainHealthReport {
    pub chain_key: String,
    pub ok: bool,
    pub node_count: usize,
    pub healthy_operator_count: usize,
    pub tip_height: Option<u64>,
    pub confirmed_tip_height: Option<u64>,
    pub scanned_height: Option<u64>,
    /// Unix seconds.
    pub last_completed_tick_at: Option<u64>,
    pub unusable_wallet_count: usize,
    /// Unix seconds; null while the confirmed tip keeps within 3 blocks of the tip.
    pub confirmed_tip_lagging_since: Option<u64>,
}

fn unix_seconds(wall_clock: SystemTime) -> u64 {
    wall_clock
        .duration_since(UNIX_EPOCH)
        .map(|since_epoch| since_epoch.as_secs())
        .unwrap_or(0)
}

impl ChainHealthSnapshot {
    fn report(&self) -> ChainHealthReport {
        let stale_after = self.poll_interval * STALE_TICK_POLL_INTERVALS;
        let tick_is_recent = match self.last_completed_tick_at {
            Some((completed_at, _)) => completed_at.elapsed() <= stale_after,
            None => self.scanner_started_at.elapsed() <= stale_after,
        };
        ChainHealthReport {
            chain_key: self.chain_key.clone(),
            ok: tick_is_recent && self.unusable_wallet_count == 0,
            node_count: self.node_count,
            healthy_operator_count: self.healthy_operator_count,
            tip_height: self.tip_height,
            confirmed_tip_height: self.confirmed_tip_height,
            scanned_height: self.scanned_height,
            last_completed_tick_at: self
                .last_completed_tick_at
                .map(|(_, completed_at_wall_clock)| unix_seconds(completed_at_wall_clock)),
            unusable_wallet_count: self.unusable_wallet_count,
            confirmed_tip_lagging_since: self.confirmed_tip_lagging_since.map(unix_seconds),
        }
    }
}

#[derive(Default)]
pub struct ChainHealthRegistry {
    snapshots_by_chain_key: RwLock<BTreeMap<String, ChainHealthSnapshot>>,
}

impl ChainHealthRegistry {
    pub fn store_snapshot(&self, snapshot: ChainHealthSnapshot) {
        self.snapshots_by_chain_key
            .write()
            .expect("the health lock is never poisoned: no writer panics while holding it")
            .insert(snapshot.chain_key.clone(), snapshot);
    }

    pub fn report_all_chains(&self) -> Vec<ChainHealthReport> {
        self.snapshots_by_chain_key
            .read()
            .expect("the health lock is never poisoned: no writer panics while holding it")
            .values()
            .map(ChainHealthSnapshot::report)
            .collect()
    }
}
