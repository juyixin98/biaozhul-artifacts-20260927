//! 参考实现：标准库 [`BTreeSet<u32>`] 上的独立 oracle。
//!
//! 与被测 `rb-format` 完全独立——测试用它逐值、逐运算、逐 rank/select
//! 对照，任何内核侧的表示或算法错误都无法被“自己生成的期望”掩盖。

use std::collections::BTreeSet;

/// 朴素排序集合参考实现。所有语义以数学集合定义直接给出。
#[derive(Debug, Clone, Default)]
pub struct Oracle {
    inner: BTreeSet<u32>,
}

impl Oracle {
    pub fn new() -> Self {
        Oracle {
            inner: BTreeSet::new(),
        }
    }

    pub fn from_values<I: IntoIterator<Item = u32>>(it: I) -> Self {
        Oracle {
            inner: it.into_iter().collect(),
        }
    }

    pub fn insert(&mut self, v: u32) -> bool {
        self.inner.insert(v)
    }

    pub fn contains(&self, v: u32) -> bool {
        self.inner.contains(&v)
    }

    pub fn len(&self) -> usize {
        self.inner.len()
    }

    pub fn is_empty(&self) -> bool {
        self.inner.is_empty()
    }

    pub fn min(&self) -> Option<u32> {
        self.inner.first().copied()
    }

    pub fn max(&self) -> Option<u32> {
        self.inner.last().copied()
    }

    pub fn sorted_vec(&self) -> Vec<u32> {
        self.inner.iter().copied().collect()
    }

    /// 并集 A ∪ B。
    pub fn union(&self, other: &Oracle) -> Oracle {
        Oracle {
            inner: self.inner.union(&other.inner).copied().collect(),
        }
    }

    /// 交集 A ∩ B。
    pub fn intersect(&self, other: &Oracle) -> Oracle {
        Oracle {
            inner: self.inner.intersection(&other.inner).copied().collect(),
        }
    }

    /// 差集 A \\ B。
    pub fn difference(&self, other: &Oracle) -> Oracle {
        Oracle {
            inner: self.inner.difference(&other.inner).copied().collect(),
        }
    }

    pub fn intersects(&self, other: &Oracle) -> bool {
        self.inner.intersection(&other.inner).next().is_some()
    }

    pub fn is_subset(&self, other: &Oracle) -> bool {
        self.inner.is_subset(&other.inner)
    }

    /// `rank(x)`：严格小于 x 的元素个数（u64）。
    pub fn rank(&self, x: u32) -> u64 {
        self.inner.range(..x).count() as u64
    }

    /// 第 i 小元素（0 基）。
    pub fn select(&self, i: u64) -> Option<u32> {
        self.inner.iter().copied().nth(i as usize)
    }
}

/// rank/select 互逆关系检查点集合：在这些 x 上验证。
///
/// 覆盖：0、1、每个容器边界、u32::MAX 以及给定样本值。
pub fn rank_select_probe_points(samples: &[u32]) -> Vec<u32> {
    let mut pts = BTreeSet::new();
    pts.insert(0u32);
    pts.insert(1);
    pts.insert(u32::MAX);
    for k in 0u32..=65536 {
        // 全部容器边界（65537 个点在测试中可接受）。
        pts.insert(k.saturating_mul(65536));
    }
    for &s in samples {
        pts.insert(s);
        pts.insert(s.saturating_sub(1));
        pts.insert(s.saturating_add(1));
    }
    pts.into_iter().collect()
}
