//! 验证器自己的固定夹具与参考答案（不调用被测核心的哈希函数）。
//!
//! 种子/常量与格式 v1 文档一致，但计算全部走验证器内置的 [`crate::xxh64`] 参考实现，
//! 因此若有人改动了内核的指纹或候选桶计算，这里的期望会对不上而被抓到。

use crate::xxh64::xxh64;

pub const SEED_H1: u64 = 0x434B_465F_4131_0001;
pub const SEED_H2: u64 = 0x434B_4F4F_4B46_3202;
pub const SEED_FP_INDEX: u64 = 0x434B_465F_4650_4944;

/// 确定性 LCG 生成“输入键”（固定夹具），与服务端迁移 RNG 无关。
pub struct KeyGenerator {
    state: u64,
}

impl KeyGenerator {
    pub fn new(seed: u64) -> Self {
        Self { state: seed }
    }
}

impl Iterator for KeyGenerator {
    type Item = Vec<u8>;
    fn next(&mut self) -> Option<Vec<u8>> {
        // MMIX LCG
        self.state = self
            .state
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        let n = self.state;
        // 键内容同时含序号与 LCG 值，确保不同运行间稳定且键间区分度高。
        Some(format!("fixture-{n:020}").into_bytes())
    }
}

/// 验证器推导的位置参考答案。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExpectedLocation {
    pub key: String,
    pub fp: u32,
    pub i1: usize,
    pub i2: usize,
}

pub fn expected_locate(key: &[u8], buckets_exp: u32, fp_bits: u32) -> ExpectedLocation {
    let num_buckets = 1usize << buckets_exp;
    let h1 = xxh64(key, SEED_H1);
    let h2 = xxh64(key, SEED_H2);
    let fp_mod_nonzero = (1u32 << fp_bits) - 1;
    let fp = (h2 % fp_mod_nonzero as u64) as u32 + 1;
    let i1 = (h1 % num_buckets as u64) as usize;

    let mut buf = [0u8; 4];
    buf.copy_from_slice(&fp.to_le_bytes());
    let hfp = xxh64(&buf, SEED_FP_INDEX);
    let i2 = (i1 ^ (hfp % num_buckets as u64) as usize) & (num_buckets - 1);

    ExpectedLocation {
        key: String::from_utf8_lossy(key).to_string(),
        fp,
        i1,
        i2,
    }
}

/// 理论单副本假阳性上界（Cuckoo Filter，b=每桶槽位数）：
/// `8/b * 2^(-f)` 是工程上常用近似；验证器只做记录，不把它当硬断言，
/// 实测以固定种子测量结果为准。
pub fn theoretical_fp_approx(bucket_size: u32, fp_bits: u32, load: f64) -> f64 {
    let base = 8.0 / bucket_size as f64 * 2f64.powi(-(fp_bits as i32));
    // 负载因子放大（简化）：~ load/(1-load) 形式的修正，仅展示用。
    base * (load / (1.0 - load).max(0.02))
}
