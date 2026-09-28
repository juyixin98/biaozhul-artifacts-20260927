//! # rb-testkit
//!
//! 独立的测试参考实现（oracle）与可复用夹具。
//!
//! **关键性质：本 crate 不依赖 `rb-format`**，参考答案全部由标准库的
//! 排序集合 [`BTreeSet`] 独立给出，因此测试不是“用被测实现给自己判卷”。
//!
//! 同时提供：
//!
//! - 确定性伪随机数生成器 [`SplitMix64`]（固定种子，夹具可复现，不依赖 rand）；
//! - 覆盖稀疏/稠密/交错/边界/阈值的夹具 [`fixtures`]；
//! - 与 Rust 端同分布的 JSON 夹具产出（便于 Python 交叉验证）。

#![forbid(unsafe_code)]

pub mod oracle;
pub mod rng;

pub use oracle::Oracle;
pub use rng::SplitMix64;

/// 容器稀疏/稠密切换阈值，与 rb-format 保持一致（常量在此独立定义）。
pub const ARRAY_MAX_CARDINALITY: u32 = 4096;
/// 每个容器覆盖的值数。
pub const CONTAINER_BITS: u32 = 1 << 16;

/// 夹具集合。
#[derive(Debug, Clone)]
pub struct Fixture {
    /// 可读名称（也是建议的集合名）。
    pub name: &'static str,
    /// 该夹具的语义说明。
    pub description: &'static str,
    /// 生成器。
    pub values: Vec<u32>,
}

/// 生成全部标准夹具（确定性结果，进程间一致）。
pub fn all_fixtures() -> Vec<Fixture> {
    vec![
        Fixture {
            name: "empty",
            description: "空集合",
            values: vec![],
        },
        Fixture {
            name: "sparse_tiny",
            description: "单容器内极稀疏（数组容器，<<4096）",
            values: sparse_in_container(0, 7, 0x9E37_79B9_F9B7_502F),
        },
        Fixture {
            name: "array_at_threshold",
            description: "单容器恰有 4096 个元素（数组侧边界，规范必须是 Array）",
            values: threshold_exact(0, ARRAY_MAX_CARDINALITY),
        },
        Fixture {
            name: "bitmap_just_above",
            description: "单容器恰有 4097 个元素（位图侧边界，规范必须是 Bitmap）",
            values: threshold_exact(0, ARRAY_MAX_CARDINALITY + 1),
        },
        Fixture {
            name: "dense_container",
            description: "单容器接近满（位图容器，60000 元素）",
            values: dense_in_container(7, 60_000, 0x1234_5678_9ABC_DEF0),
        },
        Fixture {
            name: "full_container",
            description: "一个完全填满的容器（65536 元素）",
            values: (0..CONTAINER_BITS).collect(),
        },
        Fixture {
            name: "interleaved_pair_a",
            description: "交错分布 A：偶数键容器 + 偶数值（与 B 形成部分重叠）",
            values: interleaved(0),
        },
        Fixture {
            name: "interleaved_pair_b",
            description: "交错分布 B：相邻奇数键容器 + 奇数值",
            values: interleaved(1),
        },
        Fixture {
            name: "wide_spread",
            description: "跨许多容器的稀疏集合（大量数组容器）",
            values: wide_spread(),
        },
        Fixture {
            name: "boundary_values",
            description: "u32 边界值：0、1、2^16-1、2^16、u32::MAX 等",
            values: boundary_values(),
        },
        Fixture {
            name: "high_dense",
            description: "最高分片（key=65535）稠密位图，含 u32::MAX",
            values: dense_in_container(65535, 50_000, 0xDEAD_BEEF_CAFE_BABE),
        },
    ]
}

/// 在指定高 16 位容器内放 `n` 个稀疏值。
pub fn sparse_in_container(high: u16, n: u32, seed: u64) -> Vec<u32> {
    let mut rng = SplitMix64::new(seed);
    let mut picked = Oracle::new();
    while (picked.len() as u32) < n {
        let low = rng.next_u64() as u16;
        picked.insert(combine(high, low));
    }
    picked.sorted_vec()
}

/// 在指定容器内生成 `n` 个稠密（较均匀）的值。
pub fn dense_in_container(high: u16, n: u32, seed: u64) -> Vec<u32> {
    assert!(n <= CONTAINER_BITS);
    let mut rng = SplitMix64::new(seed);
    let mut picked = Oracle::new();
    while (picked.len() as u32) < n {
        let low = rng.next_u64() as u16;
        picked.insert(combine(high, low));
    }
    picked.sorted_vec()
}

/// 阈值边界：容器内最小的 `n` 个值（n=4096 / 4097）。
pub fn threshold_exact(high: u16, n: u32) -> Vec<u32> {
    (0..n).map(|low| combine(high, low as u16)).collect()
}

/// 交错夹具：`parity=0` 取偶数 key 容器、每个容器内取偶数 low；
/// `parity=1` 取奇数 key、奇数 low，并额外在共有键上制造部分重叠。
pub fn interleaved(parity: u32) -> Vec<u32> {
    let mut out = Oracle::new();
    for high in (parity..40u32).step_by(2) {
        // 每容器 5000 个元素 → 位图容器（>4096），保证交错运算命中位图路径。
        let mut low: u32 = parity;
        let mut count = 0u32;
        while count < 5_000 {
            out.insert(combine(high as u16, low as u16));
            low = (low + 2) % CONTAINER_BITS;
            count += 1;
        }
    }
    // 制造共有键重叠：A 在 key=2 也放一批奇数；B 在 key=2 也放一批偶数。
    let shared_high: u16 = 2;
    let mut low: u32 = 1 - parity; // A→奇数, B→偶数
    let mut count = 0u32;
    while count < 3_000 {
        out.insert(combine(shared_high, low as u16));
        low = (low + 2) % CONTAINER_BITS;
        count += 1;
    }
    out.sorted_vec()
}

/// 跨 ~2000 个容器、每容器少量元素的稀疏大集合。
pub fn wide_spread() -> Vec<u32> {
    let mut rng = SplitMix64::new(0xA5A5_5A5A_0001_0002);
    let mut out = Oracle::new();
    for _ in 0..6_000 {
        let v = (rng.next_u64() % (2000u64 * CONTAINER_BITS as u64)) as u32;
        out.insert(v);
    }
    out.sorted_vec()
}

/// 边界值集合（含重复，验证去重）。
pub fn boundary_values() -> Vec<u32> {
    vec![
        0,
        0,
        1,
        2,
        CONTAINER_BITS - 1, // 65535
        CONTAINER_BITS,     // 65536（新容器起点）
        CONTAINER_BITS + 1,
        u32::MAX - 1,
        u32::MAX,
        u32::MAX,
    ]
    .into_iter()
    .collect::<std::collections::BTreeSet<u32>>()
    .into_iter()
    .collect()
}

#[inline]
fn combine(high: u16, low: u16) -> u32 {
    ((high as u32) << 16) | low as u32
}
