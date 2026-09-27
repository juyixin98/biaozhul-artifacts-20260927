//! 固定的指纹与双候选桶计算。
//!
//! # 哈希约定（本项目格式版本 1 的固定内核，禁止在同一存储格式版本内修改）
//!
//! 对键 `key` 计算两个 XXH64：
//! - `h1 = XXH64(seed = 0x43_4B_46_5F_41_31_00_01, key)`
//! - `h2 = XXH64(seed = 0x43_4B_4F_4F_4B_46_32_02, key)`
//!
//! 指纹取 `h2` 的低 f 位，并强制映射到 `1..2^f-1`（绝不为 0；0 表示空槽）：
//! - `fp_raw = (h2 mod (2^f - 1)) + 1`
//!
//! 候选桶：
//! - `i1 = h1 mod b`
//! - `i2 = i1 XOR (XXH64(seed = FP_INDEX_SEED, 小端编码的 fp) mod b)`
//!
//! 因为 XOR 对称，只持有 `(i, fp)` 即可求出另一个候选桶：
//! `i1 = i2 XOR (hash(fp) mod b)`，这是迁移与删除都能工作的关键。

use crate::params::FilterParams;

/// 主桶哈希种子（ASCII 近似 "CKF_A1\x00\x01"）。
pub const SEED_H1: u64 = 0x434B_465F_4131_0001;
/// 指纹哈希种子（ASCII 近似 "CKOOKF2\x02"）。
pub const SEED_H2: u64 = 0x434B_4F4F_4B46_3202;
/// 指纹 -> 桶位移 的哈希种子。
pub const SEED_FP_INDEX: u64 = 0x434B_465F_4650_4944;

/// 由键计算 `(指纹, i1)`。
pub fn fingerprint_and_i1(key: &[u8], p: &FilterParams) -> (u32, usize) {
    let h1 = xxhash64(key, SEED_H1);
    let h2 = xxhash64(key, SEED_H2);

    let fp_mod_nonzero = p.fingerprint_mod() - 1; // 2^f - 1
    let fp = (h2 % fp_mod_nonzero as u64) as u32 + 1;
    let i1 = (h1 % p.num_buckets() as u64) as usize;
    (fp, i1)
}

/// 由 `(指纹, 任一候选桶)` 求另一个候选桶。纯函数，不依赖键。
pub fn alt_index(index: usize, fp: u32, p: &FilterParams) -> usize {
    let mut buf = [0u8; 4];
    buf.copy_from_slice(&fp.to_le_bytes());
    let h = xxhash64(&buf, SEED_FP_INDEX);
    (index ^ (h % p.num_buckets() as u64) as usize) & (p.num_buckets() - 1)
}

/// 同时给出键的两个候选桶与指纹：`(fp, i1, i2)`。
pub fn locate(key: &[u8], p: &FilterParams) -> (u32, usize, usize) {
    let (fp, i1) = fingerprint_and_i1(key, p);
    let i2 = alt_index(i1, fp, p);
    (fp, i1, i2)
}

/// 重新导出 XXH64，便于持久化层/测试使用同一份实现。
pub fn xxhash64(input: &[u8], seed: u64) -> u64 {
    xxhash_rust::xxh64::xxh64(input, seed)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fp_never_zero_and_in_range() {
        let p = FilterParams::new(8, 4, 6, 50).unwrap();
        for n in 0..2000u32 {
            let key = format!("key-{n}");
            let (fp, i1, i2) = locate(key.as_bytes(), &p);
            assert!(fp >= 1 && fp < p.fingerprint_mod(), "fp={fp}");
            assert!(i1 < p.num_buckets());
            assert!(i2 < p.num_buckets());
        }
    }

    #[test]
    fn alt_index_is_involution() {
        let p = FilterParams::new(8, 4, 8, 50).unwrap();
        for n in 0..500u32 {
            let key = format!("k{n}");
            let (fp, i1, i2) = locate(key.as_bytes(), &p);
            assert_eq!(alt_index(i2, fp, &p), i1);
            assert_eq!(alt_index(i1, fp, &p), i2);
        }
    }

    #[test]
    fn deterministic_for_same_key() {
        let p = FilterParams::default();
        let a = locate(b"stable-key", &p);
        let b = locate(b"stable-key", &p);
        assert_eq!(a, b);
    }
}
