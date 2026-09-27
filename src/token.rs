//! 删除凭证令牌（capability token）：HMAC 签名、自包含、可校验。
//!
//! # 线格式（base64url 无填充，载荷与签名均为小端）
//!
//! ```text
//! payload = u8  version(=1)
//!         || [u8;32] key_id
//!         || u64    ordinal     // 该键第几次插入（从 0 起）
//!         || u64    i1
//!         || u64    i2
//!         || u32    fingerprint
//! token   = base64url_nopad( payload || HMAC_SHA256(domain, payload) )
//! ```
//!
//! HMAC 密钥只存在服务端（`<data_dir>/secret.key`，0600）。没有密钥的一方无法为任意
//! 键伪造有效删除凭证；载荷里的 i1/i2/fp 仅为删除路径提供精确定位，任何篡改都会使
//! 签名失效。

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use hmac::{Hmac, Mac};
use sha2::{Digest, Sha256};

use crate::credentials::CredentialError;
use crate::TOKEN_VERSION;

type HmacSha256 = Hmac<Sha256>;

const DOMAIN: &[u8] = b"cf1.token.v1";
const KEYID_DOMAIN: &[u8] = b"cf1.keyid.v1";

/// 载荷长度：1 + 32 + 8 + 8 + 8 + 4。
pub const PAYLOAD_LEN: usize = 61;
/// 完整二进制长度：载荷 + 32 字节 HMAC。
pub const TOKEN_BYTES: usize = PAYLOAD_LEN + 32;

/// 服务端签名/校验密钥。
#[derive(Clone)]
pub struct SigningKey {
    key: [u8; 32],
}

impl std::fmt::Debug for SigningKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SigningKey")
            .field("key", &"<redacted>")
            .finish()
    }
}

impl SigningKey {
    pub fn from_bytes(key: [u8; 32]) -> Self {
        Self { key }
    }

    fn mac(&self) -> HmacSha256 {
        // 域前缀纳入签名，避免同一密钥在不同用途间串扰。
        let mut m = HmacSha256::new_from_slice(&self.key).expect("HMAC 接受任意长度密钥");
        m.update(&(DOMAIN.len() as u16).to_le_bytes());
        m.update(DOMAIN);
        m
    }
}

/// 键标识：域分离 SHA-256。仅作为记账主键与令牌载荷，不暴露明文。
pub fn key_id(key: &[u8]) -> [u8; 32] {
    let mut h = Sha256::new_with_prefix(KEYID_DOMAIN);
    // 长度前缀避免不同长度输入的编码歧义。
    h.update((key.len() as u32).to_le_bytes());
    h.update(key);
    h.finalize().into()
}

/// 一张删除凭证的完整内容。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Capability {
    pub key_id: [u8; 32],
    pub ordinal: u64,
    pub i1: u64,
    pub i2: u64,
    pub fingerprint: u32,
}

impl Capability {
    /// 编码载荷（小端定长）。
    pub fn encode_payload(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(PAYLOAD_LEN);
        out.push(TOKEN_VERSION);
        out.extend_from_slice(&self.key_id);
        out.extend_from_slice(&self.ordinal.to_le_bytes());
        out.extend_from_slice(&self.i1.to_le_bytes());
        out.extend_from_slice(&self.i2.to_le_bytes());
        out.extend_from_slice(&self.fingerprint.to_le_bytes());
        debug_assert_eq!(out.len(), PAYLOAD_LEN);
        out
    }

    fn decode_payload(buf: &[u8]) -> Result<Capability, CredentialError> {
        if buf.len() != PAYLOAD_LEN {
            return Err(CredentialError::MalformedToken);
        }
        if buf[0] != TOKEN_VERSION {
            return Err(CredentialError::UnsupportedTokenVersion);
        }
        let mut key_id = [0u8; 32];
        key_id.copy_from_slice(&buf[1..33]);
        let rd = |o: usize| u64::from_le_bytes(buf[o..o + 8].try_into().unwrap());
        let ordinal = rd(33);
        let i1 = rd(41);
        let i2 = rd(49);
        let fingerprint = u32::from_le_bytes(buf[57..61].try_into().unwrap());
        Ok(Capability {
            key_id,
            ordinal,
            i1,
            i2,
            fingerprint,
        })
    }
}

/// 为一次成功插入签发令牌字符串。
pub fn issue(
    signing: &SigningKey,
    key_id: [u8; 32],
    ordinal: u64,
    i1: u64,
    i2: u64,
    fingerprint: u32,
) -> String {
    let cap = Capability {
        key_id,
        ordinal,
        i1,
        i2,
        fingerprint,
    };
    let payload = cap.encode_payload();
    let mut mac = signing.mac();
    mac.update(&payload);
    let tag = mac.finalize().into_bytes();
    let mut raw = payload;
    raw.extend_from_slice(&tag);
    URL_SAFE_NO_PAD.encode(raw)
}

/// 解析并校验令牌；成功返回其凭证内容。
pub fn verify(token: &str, signing: &SigningKey) -> Result<Capability, CredentialError> {
    let raw = URL_SAFE_NO_PAD
        .decode(token.trim().as_bytes())
        .map_err(|_| CredentialError::MalformedToken)?;
    if raw.len() != TOKEN_BYTES {
        return Err(CredentialError::MalformedToken);
    }
    let (payload, tag) = raw.split_at(PAYLOAD_LEN);

    let mut mac = signing.mac();
    mac.update(payload);
    mac.verify_slice(tag)
        .map_err(|_| CredentialError::BadSignature)?;

    Capability::decode_payload(payload)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn issue_then_verify_roundtrip() {
        let sk = SigningKey::from_bytes([9u8; 32]);
        let t = issue(&sk, [1u8; 32], 3, 7, 11, 42);
        let cap = verify(&t, &sk).expect("合法令牌");
        assert_eq!(cap.ordinal, 3);
        assert_eq!(cap.i1, 7);
        assert_eq!(cap.i2, 11);
        assert_eq!(cap.fingerprint, 42);
        assert_eq!(cap.key_id, [1u8; 32]);
    }

    #[test]
    fn tampered_token_is_rejected() {
        let sk = SigningKey::from_bytes([9u8; 32]);
        let other = SigningKey::from_bytes([8u8; 32]);
        let t = issue(&sk, [1u8; 32], 0, 1, 2, 3);
        assert_eq!(
            verify(&t, &other).unwrap_err(),
            CredentialError::BadSignature
        );

        let mut raw = URL_SAFE_NO_PAD.decode(t).unwrap();
        raw[33] ^= 0x01; // 改 ordinal 一字节
        let t2 = URL_SAFE_NO_PAD.encode(raw);
        assert_eq!(verify(&t2, &sk).unwrap_err(), CredentialError::BadSignature);
    }

    #[test]
    fn garbage_is_malformed_not_success() {
        let sk = SigningKey::from_bytes([9u8; 32]);
        assert_eq!(
            verify("not-base64!!", &sk).unwrap_err(),
            CredentialError::MalformedToken
        );
        assert_eq!(
            verify("", &sk).unwrap_err(),
            CredentialError::MalformedToken
        );
        let short = URL_SAFE_NO_PAD.encode([0u8; 10]);
        assert_eq!(
            verify(&short, &sk).unwrap_err(),
            CredentialError::MalformedToken
        );
    }
}
