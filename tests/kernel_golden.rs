//! 黄金向量测试：把独立 Python 预言机生成的定位向量当作**外部夹具**逐字节断言。
//!
//! 夹具文件 tests/golden/golden_vectors.json 由 tests/golden/golden_oracle.py
//! （Python + hashlib/OpenSSL，独立实现）生成。本测试只**读取**它，绝不生成期望值，
//! 从而能抓住「测试与实现犯同一个错误」的情况。
//!
//! 若夹具缺失，测试以明确错误失败并提示重新生成命令（而不是悄悄跳过或自造数据）。

use deletable_cuckoo::hashing::{alt_index, fingerprint, primary_index, KernelParams};
use serde::Deserialize;
use std::collections::HashSet;

#[derive(Debug, Deserialize)]
struct GoldenDoc {
    schema: String,
    kernel_version: u32,
    vectors: Vec<GoldenRow>,
}

#[derive(Debug, Deserialize)]
struct GoldenRow {
    #[allow(dead_code)]
    set: String,
    key_utf8: String,
    num_buckets: u64,
    bucket_size: u32,
    fingerprint_bits: u32,
    seed_hex: String,
    i1: u64,
    i2: u64,
    fingerprint: u32,
}

fn load_golden() -> GoldenDoc {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/golden/golden_vectors.json"
    );
    let text = std::fs::read_to_string(path).unwrap_or_else(|e| {
        panic!("无法读取黄金向量 {path}: {e}\n请先运行: python3 tests/golden/golden_oracle.py")
    });
    let doc: GoldenDoc = serde_json::from_str(&text).expect("黄金向量 JSON 结构非法");
    assert_eq!(doc.schema, "deletable-cuckoo-golden-v1");
    assert_eq!(doc.kernel_version, deletable_cuckoo::KERNEL_VERSION);
    doc
}

fn decode_seed(s: &str) -> [u8; 32] {
    let mut out = [0u8; 32];
    hex::decode_to_slice(s, &mut out).unwrap();
    out
}

#[test]
fn golden_vectors_match_independent_python_oracle() {
    let doc = load_golden();
    assert!(doc.vectors.len() >= 40, "至少应覆盖 4 个参数集 x 10 个键");
    let mut seen_sets = HashSet::new();

    for row in &doc.vectors {
        seen_sets.insert(row.set.clone());
        let seed = decode_seed(&row.seed_hex);
        assert_eq!(row.bucket_size, 4);
        assert!(row.num_buckets.is_power_of_two());

        let key = row.key_utf8.as_bytes();
        let got_i1 = primary_index(key, &seed, row.num_buckets);
        let got_fp = fingerprint(key, &seed, row.fingerprint_bits);
        let got_i2 = alt_index(got_i1, got_fp, &seed, row.num_buckets, row.fingerprint_bits);

        assert_eq!(
            (got_i1, got_fp, got_i2),
            (row.i1, row.fingerprint, row.i2),
            "黄金向量不匹配：set={} key={:?}（期望 i1={}, fp={}, i2={}）",
            row.set,
            row.key_utf8,
            row.i1,
            row.fingerprint,
            row.i2
        );

        // 指纹范围。
        let max = (1u64 << row.fingerprint_bits) - 1;
        assert!(got_fp as u64 >= 1 && got_fp as u64 <= max);

        // 对合性：i2 必须反推回 i1。
        let back = alt_index(got_i2, got_fp, &seed, row.num_buckets, row.fingerprint_bits);
        assert_eq!(back, got_i1, "备用桶定位对合性被破坏: {}", row.set);
    }

    assert!(
        seen_sets.len() >= 4,
        "应覆盖至少 4 个参数集，实际 {:?}",
        seen_sets
    );
}

#[test]
fn golden_vectors_are_deterministic_and_distinct() {
    let doc = load_golden();
    // 同参数集下，空键与普通键的定位不应整体相同（基本碰撞健全性）。
    let m16: Vec<_> = doc.vectors.iter().filter(|r| r.set == "m16_f8").collect();
    let empty = m16.iter().find(|r| r.key_utf8.is_empty()).unwrap();
    let a = m16.iter().find(|r| r.key_utf8 == "a").unwrap();
    assert_ne!((empty.i1, empty.fingerprint), (a.i1, a.fingerprint));

    // 用同样参数直接构造内核，结果应稳定（重复计算一致）。
    let row = &m16[1];
    let seed = decode_seed(&row.seed_hex);
    let p = KernelParams {
        version: 1,
        num_buckets: row.num_buckets,
        bucket_size: row.bucket_size,
        fingerprint_bits: row.fingerprint_bits,
        max_kicks: 0,
        seed,
    };
    let x = deletable_cuckoo::hashing::place(row.key_utf8.as_bytes(), &p);
    let y = deletable_cuckoo::hashing::place(row.key_utf8.as_bytes(), &p);
    assert_eq!(x, y);
}
