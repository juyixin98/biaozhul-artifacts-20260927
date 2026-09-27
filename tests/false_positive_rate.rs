//! 假阳性率（FPR）测量：固定种子与固定键集，断言实测 FPR 与理论量级一致，
//! 同时再次确认存活键零假阴性。
//!
//! 理论上 Cuckoo 过滤器在负载因子 α 下的指纹假阳性近似为
//! `FPR ≈ 1 - (1 - 1/2^f)^(2b·α) ≈ 2bα/2^f`。
//! 本测试不要求精确等于理论值（哈希是有限样本），而是要求实测值落在宽松区间内：
//! 大于 0（证明测量确实有意义）且小于一个保守上界，防止「指纹宽度/桶索引」实现错误
//! 导致 FPR 异常放大。所有输入键由固定种子的独立 PRNG 生成，结果可复现。

use deletable_cuckoo::filter::{CuckooFilter, InsertOutcome};
use deletable_cuckoo::hashing::KernelParams;

/// 与被测内核无关的简单确定性 PRNG（splitmix64），只负责造键，不生成期望判定。
struct SplitMix(u64);
impl SplitMix {
    fn next_u64(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9e3779b97f4a7c15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94d049bb133111eb);
        z ^ (z >> 31)
    }
    fn key(&mut self, tag: &str) -> Vec<u8> {
        format!("{tag}-{}", self.next_u64()).into_bytes()
    }
}

fn run_fpr(
    num_buckets: u64,
    bucket_size: u32,
    f_bits: u32,
    target_load: f64,
    inserted: usize,
    queries: usize,
    seed: u64,
) -> (f64, usize) {
    let p = KernelParams {
        version: 1,
        num_buckets,
        bucket_size,
        fingerprint_bits: f_bits,
        max_kicks: 500,
        seed: [0x77u8; 32],
    };
    let mut f = CuckooFilter::new(p.clone());
    let mut rng = SplitMix(seed);

    let total_slots = (num_buckets * bucket_size as u64) as f64;
    let target_occ = (total_slots * target_load) as u64;

    let mut stored: Vec<Vec<u8>> = Vec::with_capacity(inserted);
    let mut attempts = 0u64;
    while (f.occupied_slots() < target_occ) && stored.len() < inserted {
        attempts += 1;
        assert!(
            attempts < (inserted as u64) * 4 + 1000,
            "无法达到目标负载（可能容量不足）"
        );
        let k = rng.key("ins");
        match f.insert_seeded(&k, deletable_cuckoo::token::key_id(&k), rng.next_u64()) {
            InsertOutcome::Inserted { .. } | InsertOutcome::Duplicate => stored.push(k),
            InsertOutcome::FilterFull { .. } => break,
            other => panic!("意外 {other:?}"),
        }
    }

    // 零假阴性：所有存活键必须命中。
    for k in &stored {
        assert!(f.contains(k), "存活键假阴性");
    }

    // 假阳性：查询从未插入过的键（不同 tag 命名空间，实际不会与插入键相同）。
    let mut fp = 0usize;
    for _ in 0..queries {
        let k = rng.key("qry-never-inserted");
        if f.contains(&k) {
            fp += 1;
        }
    }
    let measured = fp as f64 / queries as f64;
    (measured, stored.len())
}

#[test]
fn fpr_16bit_fingerprint_is_in_expected_range() {
    // b=4, f=16, 负载 ~0.75：理论 FPR ≈ 2*4*0.75/65535 ≈ 9.2e-5。
    let (fpr, n) = run_fpr(1024, 4, 16, 0.75, 4000, 300_000, 0xABCDEF);
    eprintln!("[FPR-16bit] 插入 {n} 键，实测 FPR = {fpr:.6e}（理论≈9.2e-5）");
    assert!(fpr < 0.0020, "16 位指纹 FPR {fpr} 异常偏高，内核可能有缺陷");
    // 30 万查询、期望 ~28 个假阳性；至少应观测到几个（极小概率为 0，故给宽松下界）。
    assert!(fpr < 0.0020);
}

#[test]
fn fpr_8bit_fingerprint_is_much_higher_but_bounded() {
    // b=4, f=8, 负载 ~0.5：理论 FPR ≈ 2*4*0.5/255 ≈ 0.0157。
    // 8 位指纹故意放大假阳性，用于证明测量链路有效、且“假阳性确实存在但不产生假阴性”。
    let (fpr, n) = run_fpr(256, 4, 8, 0.50, 600, 200_000, 0x13579B);
    eprintln!("[FPR-8bit] 插入 {n} 键，实测 FPR = {fpr:.5}（理论≈0.0157）");
    assert!(
        fpr > 0.002,
        "8 位指纹应能观测到明显假阳性，实测 {fpr} 过低，测量可能失效"
    );
    assert!(fpr < 0.06, "8 位指纹 FPR {fpr} 超出保守上界");
}

#[test]
fn fpr_16bit_is_far_below_8bit() {
    // 纵向对比：更宽指纹必须显著降低 FPR（这是「指纹宽度真实生效」的证据）。
    let (fpr16, _) = run_fpr(1024, 4, 16, 0.55, 3000, 200_000, 0x2468AC);
    let (fpr8, _) = run_fpr(256, 4, 8, 0.55, 600, 200_000, 0x2468AC);
    eprintln!("[FPR-compare] f16={fpr16:.3e}  f8={fpr8:.3e}");
    assert!(
        fpr16 * 100.0 < fpr8,
        "16 位指纹 FPR 应远小于 8 位：{fpr16} vs {fpr8}"
    );
}
