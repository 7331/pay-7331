//! Opens `account_monero_wallets.sealed_private_view_key`, byte for byte as `backend/app/shared/sealed_bytes.py` and
//! `backend/app/payments/monero_wallets/secret_sealing.py` seal it:
//!
//! - key: HKDF-SHA256 (RFC 5869), no salt, `info` = `pay-7331 account monero private view key encryption v1`, 32 bytes,
//!   over the bytes of `PAY_INVOICE_SALT_SEED` (hex-decoded);
//! - blob: a 12-byte nonce, then the AES-256-GCM ciphertext with its 16-byte tag appended;
//! - associated data: `"{account_id}|{chain_key}|{primary_address}"` (UTF-8), so a blob opens only on its own row;
//! - plaintext: the private view key as its 64 lowercase-or-uppercase hex characters (UTF-8), not raw bytes.

use aes_gcm::aead::{Aead, KeyInit, Payload};
use aes_gcm::{Aes256Gcm, Nonce};
use hkdf::Hkdf;
use sha2::Sha256;
use zeroize::Zeroizing;

pub const PRIVATE_VIEW_KEY_ENCRYPTION_KEY_INFO: &[u8] = b"pay-7331 account monero private view key encryption v1";
const NONCE_BYTES: usize = 12;

pub struct PrivateViewKeyOpener {
    encryption_key: Zeroizing<[u8; 32]>,
}

impl PrivateViewKeyOpener {
    pub fn from_invoice_salt_seed(invoice_salt_seed: &[u8]) -> Self {
        let mut encryption_key = Zeroizing::new([0u8; 32]);
        Hkdf::<Sha256>::new(None, invoice_salt_seed)
            .expand(PRIVATE_VIEW_KEY_ENCRYPTION_KEY_INFO, encryption_key.as_mut_slice())
            .expect("32 bytes is a valid HKDF-SHA256 output length");
        Self { encryption_key }
    }

    /// The private view key's hex text, or `None` when the blob does not open under this seed and row.
    pub fn open_private_view_key_hex(
        &self,
        sealed_private_view_key: &[u8],
        account_id: i64,
        chain_key: &str,
        primary_address: &str,
    ) -> Option<Zeroizing<String>> {
        if sealed_private_view_key.len() < NONCE_BYTES {
            return None;
        }
        let (nonce, ciphertext_and_tag) = sealed_private_view_key.split_at(NONCE_BYTES);
        let associated_data = format!("{account_id}|{chain_key}|{primary_address}");
        let cipher = Aes256Gcm::new_from_slice(self.encryption_key.as_slice()).ok()?;
        let plaintext = Zeroizing::new(
            cipher
                .decrypt(
                    Nonce::from_slice(nonce),
                    Payload {
                        msg: ciphertext_and_tag,
                        aad: associated_data.as_bytes(),
                    },
                )
                .ok()?,
        );
        let private_view_key_hex = std::str::from_utf8(&plaintext).ok()?;
        Some(Zeroizing::new(private_view_key_hex.to_owned()))
    }
}
