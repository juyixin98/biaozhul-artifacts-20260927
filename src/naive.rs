//! 独立朴素参照实现（original-text scan）。
//!
//! 该模块**刻意不使用索引内核的任何数据结构**，只对原文做线性扫描，
//! 是测试与 `/v1/indexes/:name/verify` 的唯一参照来源。
//! 需求约束：朴素扫描只能做测试参考，不代替索引查询——服务的 search/locate
//! 永远走 FM 内核，verify 仅把两者结果并列返回供外部判断。

/// 朴素命中数（含重叠）。
/// 空模式按全服务统一语义返回 `text.len() + 1`；超长模式返回 0。
pub fn naive_count(text: &[u8], pattern: &[u8]) -> u64 {
    if pattern.is_empty() {
        return text.len() as u64 + 1;
    }
    if pattern.len() > text.len() {
        return 0;
    }
    text.windows(pattern.len())
        .filter(|w| *w == pattern)
        .count() as u64
}

/// 朴素定位：全部重叠匹配起点（升序）。
/// 空模式返回 0..=m 的所有位置；超长模式返回空。
pub fn naive_locate(text: &[u8], pattern: &[u8]) -> Vec<u64> {
    if pattern.is_empty() {
        return (0..=text.len() as u64).collect();
    }
    if pattern.len() > text.len() {
        return Vec::new();
    }
    text.windows(pattern.len())
        .enumerate()
        .filter(|(_, w)| *w == pattern)
        .map(|(i, _)| i as u64)
        .collect()
}

/// 一次朴素/索引对照结论。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Verdict {
    /// 计数与定位都一致。
    Agree,
    /// 计数不一致（附索引值与朴素值）。
    CountMismatch { fm: u64, naive: u64 },
    /// 计数一致但定位集合不一致（附差异描述）。
    LocationsMismatch { detail: String },
}

/// 对一个模式执行 FM 结果 vs 朴素扫描的独立对照。
pub fn compare(text: &[u8], fm_count: u64, fm_locations: &[u64], pattern: &[u8]) -> Verdict {
    let n_count = naive_count(text, pattern);
    if fm_count != n_count {
        return Verdict::CountMismatch {
            fm: fm_count,
            naive: n_count,
        };
    }
    let n_locs = naive_locate(text, pattern);
    if fm_locations != n_locs.as_slice() {
        // 计算集合差异，方便日志定位问题
        let only_fm: Vec<u64> = fm_locations
            .iter()
            .copied()
            .filter(|p| !n_locs.contains(p))
            .collect();
        let only_naive: Vec<u64> = n_locs
            .iter()
            .copied()
            .filter(|p| !fm_locations.contains(p))
            .collect();
        return Verdict::LocationsMismatch {
            detail: format!("仅索引给出={only_fm:?}, 仅朴素给出={only_naive:?}"),
        };
    }
    Verdict::Agree
}
