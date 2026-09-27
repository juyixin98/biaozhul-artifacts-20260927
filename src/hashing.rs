//! 固定、确定性的 Cuckoo 哈希内核。
//!
//! # 口径（任何独立实现必须逐字节一致）
//!
//! 所有哈希均为 SHA-256，域分离前缀是**带长度的 ASCII 字符串**，从而不同上下文不会串扰：
//!
//! ```text
//! H_index(x)      = SHA256( u16le(len("cf1.index.v1")) || b"cf1.index.v1" ||
//!                           u16le(32) || seed ||
//!                           u32le(len(key)) || key )
//! H_fingerprint(x)= SHA256( b"cf1.fingerprint.v1" ... 同上布局 ... )
//! H_altname(f)    = SHA256( u16le(len("cf1.alt.v1")) || b"cf1.alt.v1" ||
//!                           u16le(32) || seed ||
//!                           u32le(f_bits) || u64le(m) || u32le(f) )
//! ```
//! 注：备用桶哈希**只依赖指纹**（以及参数 m/f_bits/seed），不含当前桶号——这是
//! 对合（involution）的必要条件：`i XOR h(fp)` 再算一次必然回到 i。
//!
//! * 主桶：`i1 = H_index 的前 8 字节 u64 mod m`（要求 m 为 2 的幂）。
//! * 指纹：`fp = 1 + (H_fp 的前 8 字节 u64 mod (2^f - 1))`，故 fp ∈ [1, 2^f-1]，
//!   0 始终保留给空槽。
//! * 备用桶：`i2 = i1 XOR (H_altname(fp, i1) 的前 8 字节 mod m)`。
//!   该定义天然对合（involution）：从 i2 反查只需 `i2 XOR hash(fp,i2) mod m`，
//!   即 [`alt_index`] 对 i1、i2 都成立——这是删除/迁移能找到另一个桶的前提。
//!
//! 内核不含随机源：迁移时的随机性只属于 [`crate::filter`]，不影响「指纹和两个桶固定」
//! 这一可复核性质。

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// 内核参数（进入快照头，重载时校验一致）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct KernelParams {
    /// 哈希内核版本。
    pub version: u32,
    /// 桶数量 m（2 的幂）。
    pub num_buckets: u64,
    /// 每桶槽位数 b。
    pub bucket_size: u32,
    /// 指纹位宽 f。
    pub fingerprint_bits: u32,
    /// 迁移次数上限。
    pub max_kicks: u32,
    /// 域分离种子。
    pub seed: [u8; 32],
}

const CTX_INDEX: &[u8] = b"cf1.index.v1";
const CTX_FP: &[u8] = b"cf1.fingerprint.v1";
const CTX_ALT: &[u8] = b"cf1.alt.v1";

/// 写入 `u16le(len(ctx)) || ctx`。
fn push_ctx(out: &mut Vec<u8>, ctx: &[u8]) {
    out.extend_from_slice(&(ctx.len() as u16).to_le_bytes());
    out.extend_from_slice(ctx);
}

/// 计算主桶 i1。
pub fn primary_index(key: &[u8], seed: &[u8; 32], num_buckets: u64) -> u64 {
    debug_assert!(num_buckets.is_power_of_two());
    let mut h = Sha256::new();
    let mut pre = Vec::with_capacity(64 + key.len());
    push_ctx(&mut pre, CTX_INDEX);
    pre.extend_from_slice(&32u16.to_le_bytes());
    pre.extend_from_slice(seed);
    pre.extend_from_slice(&(key.len() as u32).to_le_bytes());
    h.update(&pre);
    h.update(key);
    let d = h.finalize();
    u64::from_le_bytes(d[0..8].try_into().unwrap()) & (num_buckets - 1)
}

/// 计算指纹（非零）。
pub fn fingerprint(key: &[u8], seed: &[u8; 32], fingerprint_bits: u32) -> u32 {
    debug_assert!((1..=32).contains(&fingerprint_bits));
    let modulus: u64 = if fingerprint_bits == 32 {
        // 2^32 - 1 用 u64 表示，避免 1u64 << 32 的心智负担（这里本就安全）。
        u32::MAX as u64
    } else {
        (1u64 << fingerprint_bits) - 1
    };
    let mut h = Sha256::new();
    let mut pre = Vec::with_capacity(64 + key.len());
    push_ctx(&mut pre, CTX_FP);
    pre.extend_from_slice(&32u16.to_le_bytes());
    pre.extend_from_slice(seed);
    pre.extend_from_slice(&(key.len() as u32).to_le_bytes());
    h.update(&pre);
    h.update(key);
    let d = h.finalize();
    let raw = u64::from_le_bytes(d[0..8].try_into().unwrap()) % modulus;
    (raw as u32) + 1
}

/// 备用桶定位：`alt = (i XOR (H_altname(fp) mod m)) & (m-1)`。
///
/// H 只依赖指纹（与当前桶号无关），因此该运算天然对合：
/// `alt(alt(i,fp),fp) = i`。这一点由 Python 独立预言机与 `kernel_golden` 测试
/// 对每个黄金向量双向断言。
pub fn alt_index(
    idx: u64,
    fp: u32,
    seed: &[u8; 32],
    num_buckets: u64,
    fingerprint_bits: u32,
) -> u64 {
    debug_assert!(num_buckets.is_power_of_two());
    let mut h = Sha256::new();
    let mut pre = Vec::with_capacity(56);
    push_ctx(&mut pre, CTX_ALT);
    pre.extend_from_slice(&32u16.to_le_bytes());
    pre.extend_from_slice(seed);
    pre.extend_from_slice(&fingerprint_bits.to_le_bytes());
    pre.extend_from_slice(&num_buckets.to_le_bytes());
    h.update(&pre);
    // 指纹为定长 4 字节，直接追加。
    h.update(fp.to_le_bytes());
    let d = h.finalize();
    let h64 = u64::from_le_bytes(d[0..8].try_into().unwrap());
    (idx ^ h64) & (num_buckets - 1)
}

/// 一次定位结果：指纹与其两个候选桶。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Placement {
    pub fingerprint: u32,
    pub i1: u64,
    pub i2: u64,
}

/// 计算键的完整定位（指纹 + 两个候选桶）。
pub fn place(key: &[u8], p: &KernelParams) -> Placement {
    let fp = fingerprint(key, &p.seed, p.fingerprint_bits);
    let i1 = primary_index(key, &p.seed, p.num_buckets);
    let i2 = alt_index(i1, fp, &p.seed, p.num_buckets, p.fingerprint_bits);
    Placement {
        fingerprint: fp,
        i1,
        i2,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn params(m: u64, f: u32) -> KernelParams {
        KernelParams {
            version: 1,
            num_buckets: m,
            bucket_size: 4,
            fingerprint_bits: f,
            max_kicks: 10,
            seed: [7u8; 32],
        }
    }

    #[test]
    fn fingerprint_never_zero_and_in_range() {
        let p = params(16, 8);
        for n in 0..2000u32 {
            let k = format!("k-{n}");
            let fp = fingerprint(k.as_bytes(), &p.seed, 8);
            assert!((1..=255).contains(&fp), "fp={fp}");
        }
    }

    #[test]
    fn alt_is_an_involution() {
        let p = params(64, 12);
        for n in 0..500u32 {
            let k = format!("item-{n}");
            let pl = place(k.as_bytes(), &p);
            let back = alt_index(pl.i2, pl.fingerprint, &p.seed, 64, 12);
            assert_eq!(back, pl.i1, "对合性被破坏");
        }
    }

    #[test]
    fn placement_is_deterministic() {
        let p = params(32, 16);
        let a = place(b"hello", &p);
        let b = place(b"hello", &p);
        assert_eq!(a, b);
        let c = place(b"hellp", &p);
        assert!(!(a.fingerprint == c.fingerprint && a.i1 == c.i1));
    }
}
