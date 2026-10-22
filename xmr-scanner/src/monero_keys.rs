//! Addresses and view keys, network-explicit everywhere (PLAN.md section 14: AcceptXMR shipped mainnet prefixes on
//! stagenet). All crypto is monero-oxide's: Base58 with Monero's checksum and the network prefixes in monero-address,
//! `a*G` and the subaddress derivation in monero-wallet's `ViewPair`
//! (https://docs.getmonero.org/public-address/subaddress/), so the scanner recognises exactly what the API hands out.

use monero_wallet::address::{AddressType, MoneroAddress, Network, SubaddressIndex};
use monero_wallet::ed25519::Scalar;
use monero_wallet::ViewPair;
use zeroize::Zeroizing;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MoneroNetwork {
    Mainnet,
    Stagenet,
}

impl MoneroNetwork {
    pub fn from_name(name: &str) -> Option<Self> {
        match name {
            "mainnet" => Some(Self::Mainnet),
            "stagenet" => Some(Self::Stagenet),
            _ => None,
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Self::Mainnet => "mainnet",
            Self::Stagenet => "stagenet",
        }
    }

    pub fn address_network(self) -> Network {
        match self {
            Self::Mainnet => Network::Mainnet,
            Self::Stagenet => Network::Stagenet,
        }
    }
}

/// A standard (primary) address of `network` with a good checksum: prefix 18 ("4") on mainnet, 24 ("5") on stagenet.
/// Subaddresses, integrated and featured addresses are refused.
pub fn parse_primary_address(network: MoneroNetwork, primary_address: &str) -> Option<MoneroAddress> {
    let address = MoneroAddress::from_str(network.address_network(), primary_address).ok()?;
    matches!(address.kind(), AddressType::Legacy).then_some(address)
}

/// 64 hex characters decoded to 32 bytes; `None` when it is not exactly that. Never echoes the input.
pub fn decode_private_view_key_hex(private_view_key_hex: &str) -> Option<Zeroizing<[u8; 32]>> {
    if private_view_key_hex.len() != 64 {
        return None;
    }
    let mut private_view_key_bytes = Zeroizing::new([0u8; 32]);
    hex::decode_to_slice(private_view_key_hex, private_view_key_bytes.as_mut_slice()).ok()?;
    Some(private_view_key_bytes)
}

/// The view pair of a primary address and its private view key, only when `a*G` equals the public view key inside the
/// address (a wrong view key derives subaddresses nobody can spend). `None` for a non-canonical scalar, a mismatch, or
/// a torsioned spend key (monero-wallet refuses to scan for one).
pub fn build_matching_view_pair(
    primary_address: &MoneroAddress,
    private_view_key_bytes: &Zeroizing<[u8; 32]>,
) -> Option<ViewPair> {
    let private_view_key = Zeroizing::new(Scalar::read(&mut private_view_key_bytes.as_slice()).ok()?);
    let view_pair = ViewPair::new(primary_address.spend(), private_view_key).ok()?;
    (view_pair.view() == primary_address.view()).then_some(view_pair)
}

/// The subaddress string of (`major_index`, `minor_index`): 95 characters, prefix 42 ("8") on mainnet, 36 ("7") on
/// stagenet. `None` for (0, 0), which is the primary address and not a subaddress.
pub fn derive_subaddress(
    view_pair: &ViewPair,
    network: MoneroNetwork,
    major_index: u32,
    minor_index: u32,
) -> Option<String> {
    let subaddress_index = SubaddressIndex::new(major_index, minor_index)?;
    Some(
        view_pair
            .subaddress(network.address_network(), subaddress_index)
            .to_string(),
    )
}
