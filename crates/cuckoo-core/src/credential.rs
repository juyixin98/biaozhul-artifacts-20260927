//! 删除凭证：删除请求必须持有「插入时签发」的凭证。
//!
//! 设计目标是防止近似成员查询的假阳性变成*假删除*——
//! 仅靠 `contains` 通过就允许删除，可能删掉别的键共享同一指纹槽的副本。
//!
//! 凭证（token）形态：
//! ```text
//! base64url(jti[16]) + "." + base64url(HMAC-SHA256(master_key, jti || 0x00 || key))
//! ```
//! - `jti` 是每次插入随机生成的 128 位唯一标识；
//! - 签名把 `jti` 与*插入时的确切键字节*绑定，凭证无法转移给其他键，也无法离线伪造；
//! - 服务端在持久化的凭证账本里按 `jti` 记录 `(fingerprint, i1, remaining_uses)`，
//!   `remaining_uses` 随删除递减、到 0 即删除账本项，因此同一凭证无法重复使用，
//!   重放删除得到 `CREDENTIAL_EXHAUSTED`（插入期间已计入的那次除外）。
//!
//! 签名本身可无状态校验；账本提供「只允许持有已插入键凭证者删除」的一次性语义。

use hmac::{Hmac, Mac};
use sha2::Sha256;

use crate::error::CoreError;

type HmacSha256 = Hmac<Sha256>;

/// 每次插入的唯一标识长度（字节）。
pub const JTI_LEN: usize = 16;
/// HMAC-SHA256 标签长度（字节）。
pub const TAG_LEN: usize = 32;

/// 生成一个新的随机 jti（128 位，getrandom，不依赖可预测种子）。
pub fn new_jti() -> [u8; JTI_LEN] {
    let mut jti = [0u8; JTI_LEN];
    getrandom::getrandom(&mut jti).expect("getrandom 在常规平台上不会失败");
    jti
}

/// 计算 `HMAC(master_key, jti || 0x00 || key)`。
fn tag(master_key: &[u8], jti: &[u8], key: &[u8]) -> [u8; TAG_LEN] {
    let mut mac = <HmacSha256 as Mac>::new_from_slice(master_key)
        .expect("HMAC 接受任意长度密钥");
    mac.update(jti);
    mac.update(&[0x00]);
    mac.update(key);
    let out = mac.finalize().into_bytes();
    let mut tag = [0u8; TAG_LEN];
    tag.copy_from_slice(&out);
    tag
}

/// 为一次插入签发 token。
pub fn issue(master_key: &[u8], jti: &[u8; JTI_LEN], key: &[u8]) -> String {
    let t = tag(master_key, jti, key);
    format!("{}.{}", b64url(jti), b64url(&t))
}

/// 校验 token：格式、签名与键绑定全部通过才返回 jti。
///
/// 注意：这里不检查「是否已消费」——一次性语义由服务端凭证账本负责。
pub fn verify(
    master_key: &[u8],
    token: &str,
    key: &[u8],
) -> Result<[u8; JTI_LEN], CoreError> {
    let (jti_b64, tag_b64) = token
        .split_once('.')
        .ok_or_else(|| CoreError::InvalidCredential("token 必须包含 '.' 分隔符".to_string()))?;

    let jti_vec = b64url_decode(jti_b64)
        .map_err(|e| CoreError::InvalidCredential(format!("jti 解码失败: {e}")))?;
    let tag_vec = b64url_decode(tag_b64)
        .map_err(|e| CoreError::InvalidCredential(format!("签名解码失败: {e}")))?;

    if jti_vec.len() != JTI_LEN {
        return Err(CoreError::InvalidCredential(format!(
            "jti 长度应为 {JTI_LEN} 字节，实际 {}",
            jti_vec.len()
        )));
    }
    if tag_vec.len() != TAG_LEN {
        return Err(CoreError::InvalidCredential(format!(
            "签名长度应为 {TAG_LEN} 字节，实际 {}",
            tag_vec.len()
        )));
    }

    let mut jti = [0u8; JTI_LEN];
    jti.copy_from_slice(&jti_vec);
    let expected = tag(master_key, &jti, key);

    // 常量时间比较。
    let mut mac = <HmacSha256 as Mac>::new_from_slice(master_key)
        .expect("HMAC 接受任意长度密钥");
    mac.update(&jti);
    mac.update(&[0x00]);
    mac.update(key);
    mac.verify_slice(&tag_vec)
        .map_err(|_| CoreError::InvalidCredential("签名不匹配或键不一致".to_string()))?;
    let _ = expected; // expected 仅用于文档化；verify_slice 已做常量时间校验

    Ok(jti)
}

// ---- 最小 base64url（无填充），避免再引一个依赖 ----

fn b64url(data: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = if chunk.len() > 1 { chunk[1] as u32 } else { 0 };
        let b2 = if chunk.len() > 2 { chunk[2] as u32 } else { 0 };
        let triple = (b0 << 16) | (b1 << 8) | b2;
        out.push(T[((triple >> 18) & 63) as usize] as char);
        out.push(T[((triple >> 12) & 63) as usize] as char);
        if chunk.len() > 1 {
            out.push(T[((triple >> 6) & 63) as usize] as char);
        }
        if chunk.len() > 2 {
            out.push(T[(triple & 63) as usize] as char);
        }
    }
    out
}

fn b64url_decode(s: &str) -> Result<Vec<u8>, String> {
    fn val(c: u8) -> Result<u8, String> {
        match c {
            b'A'..=b'Z' => Ok(c - b'A'),
            b'a'..=b'z' => Ok(c - b'a' + 26),
            b'0'..=b'9' => Ok(c - b'0' + 52),
            b'-' => Ok(62),
            b'_' => Ok(63),
            other => Err(format!("非法 base64url 字符: {other:#x}")),
        }
    }
    let bytes = s.as_bytes();
    if bytes.len() < 2 {
        return Err("长度过短".to_string());
    }
    let mut out = Vec::with_capacity(bytes.len() * 3 / 4);
    let mut i = 0;
    while i < bytes.len() {
        let remain = bytes.len() - i;
        if remain < 2 {
            return Err("存在孤立字符".to_string());
        }
        let c0 = val(bytes[i])? as u32;
        let c1 = val(bytes[i + 1])? as u32;
        out.push(((c0 << 2) | (c1 >> 4)) as u8);
        if remain >= 3 {
            let c2 = val(bytes[i + 2])? as u32;
            out.push((((c1 & 15) << 4) | (c2 >> 2)) as u8);
            if remain >= 4 {
                let c3 = val(bytes[i + 3])? as u32;
                out.push((((c2 & 3) << 6) | c3) as u8);
            }
        }
        i += 4;
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn issue_verify_roundtrip() {
        let key = [7u8; 20];
        let jti = new_jti();
        let tok = issue(b"master", &jti, &key);
        let got = verify(b"master", &tok, &key).unwrap();
        assert_eq!(got, jti);
    }

    #[test]
    fn rejects_wrong_key_and_wrong_master() {
        let jti = new_jti();
        let tok = issue(b"master", &jti, b"real-key");
        assert!(verify(b"master", &tok, b"other-key").is_err());
        assert!(verify(b"attacker", &tok, b"real-key").is_err());
        assert!(verify(b"master", "not-a-token", b"real-key").is_err());
        assert!(verify(b"master", "abc.abc", b"real-key").is_err());
    }

    #[test]
    fn tampered_tag_rejected() {
        let jti = new_jti();
        let mut tok = issue(b"master", &jti, b"k");
        tok.push('x');
        assert!(verify(b"master", &tok, b"k").is_err());
    }
}
