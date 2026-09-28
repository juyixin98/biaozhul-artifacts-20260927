//! 容器：一个高 16 位分片内部的稀疏数组或稠密位图。

use crate::{ARRAY_MAX_CARDINALITY, BITMAP_WORDS};

/// 单个 65536 值分片内的元素集合。
///
/// 不变量（构造与 [`Container::canonicalize`] 后始终成立）：
///
/// - `Array(v)`：`v` 严格升序、无重复，且 `v.len() <= 4096`；
/// - `Bitmap(b)`：`popcount(b) > 4096`。基数恰好在分界点时规范化为 `Array`。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Container {
    /// 稀疏表示：排序去重的低 16 位值。
    Array(Vec<u16>),
    /// 稠密表示：1024 个 u64，按低位比特索引。
    Bitmap(Box<[u64; BITMAP_WORDS]>),
}

impl Default for Container {
    fn default() -> Self {
        Container::Array(Vec::new())
    }
}

// ---------- 构造与基本查询 ----------

impl Container {
    /// 空数组容器。
    pub fn new() -> Self {
        Container::Array(Vec::new())
    }

    /// 从一批（可能乱序、重复的）低 16 位值构建规范容器。
    pub fn from_values(mut values: Vec<u16>) -> Self {
        values.sort_unstable();
        values.dedup();
        Self::from_sorted_unique(values)
    }

    /// 从已排序去重的值构建（内部仍会在 debug 下断言）。
    pub fn from_sorted_unique(values: Vec<u16>) -> Self {
        debug_assert!(is_sorted_unique(&values));
        let mut c = Container::Array(values);
        c.canonicalize();
        c
    }

    /// 加入一个值，返回是否为新增。
    pub fn insert(&mut self, low: u16) -> bool {
        let added = match self {
            Container::Array(v) => match v.binary_search(&low) {
                Ok(_) => false,
                Err(pos) => {
                    v.insert(pos, low);
                    true
                }
            },
            Container::Bitmap(b) => {
                let added = !get_bit(b, low);
                set_bit(b, low);
                added
            }
        };
        if added {
            self.canonicalize();
        }
        added
    }

    /// 删除一个值，返回是否原本存在。
    pub fn remove(&mut self, low: u16) -> bool {
        let existed = match self {
            Container::Array(v) => match v.binary_search(&low) {
                Ok(pos) => {
                    v.remove(pos);
                    true
                }
                Err(_) => false,
            },
            Container::Bitmap(b) => {
                let existed = get_bit(b, low);
                clear_bit(b, low);
                existed
            }
        };
        if existed {
            // 位图删除后基数可能降到阈值以下，需要转回数组。
            self.canonicalize();
        }
        existed
    }

    #[inline]
    pub fn contains(&self, low: u16) -> bool {
        match self {
            Container::Array(v) => v.binary_search(&low).is_ok(),
            Container::Bitmap(b) => get_bit(b, low),
        }
    }

    /// 基数（元素个数）。数组取长度 O(1)；位图做 popcount，O(1024)。
    pub fn len(&self) -> usize {
        match self {
            Container::Array(v) => v.len(),
            Container::Bitmap(b) => b.iter().map(|w| w.count_ones() as usize).sum(),
        }
    }

    #[inline]
    pub fn is_empty(&self) -> bool {
        match self {
            Container::Array(v) => v.is_empty(),
            Container::Bitmap(b) => b.iter().all(|w| *w == 0),
        }
    }

    /// 当前是否为稀疏数组容器。
    #[inline]
    pub fn is_array(&self) -> bool {
        matches!(self, Container::Array(_))
    }

    /// 升序遍历容器内所有低 16 位值。
    pub fn iter(&self) -> ContainerIter<'_> {
        match self {
            Container::Array(v) => ContainerIter::Array(v.iter()),
            Container::Bitmap(b) => ContainerIter::Bitmap {
                words: b,
                wi: 0,
                current: b[0],
            },
        }
    }

    /// 升序收集为 Vec（仅供测试、序列化数组路径或小规模导出；运算不依赖它）。
    pub fn to_vec(&self) -> Vec<u16> {
        self.iter().collect()
    }

    /// 按固定阈值规范化表示：
    /// - 数组长度 > 4096 → 转位图；
    /// - 位图 popcount <= 4096 → 转回数组（空位图也归为空数组）。
    pub fn canonicalize(&mut self) {
        match self {
            Container::Array(v) => {
                if v.len() > ARRAY_MAX_CARDINALITY {
                    let mut b = Box::new([0u64; BITMAP_WORDS]);
                    for &x in v.iter() {
                        set_bit(&mut b, x);
                    }
                    *self = Container::Bitmap(b);
                }
            }
            Container::Bitmap(b) => {
                let n = b.iter().map(|w| w.count_ones() as usize).sum::<usize>();
                if n <= ARRAY_MAX_CARDINALITY {
                    let mut v = Vec::with_capacity(n);
                    for (wi, word) in b.iter().enumerate() {
                        let mut w = *word;
                        while w != 0 {
                            let bit = w.trailing_zeros();
                            v.push((wi as u16) * 64 + bit as u16);
                            w &= w - 1;
                        }
                    }
                    *self = Container::Array(v);
                }
            }
        }
    }

    /// 从位图字构造，并规范化（仅容器运算内部使用）。
    pub(crate) fn from_bitmap_words(words: [u64; BITMAP_WORDS]) -> Self {
        let mut c = Container::Bitmap(Box::new(words));
        c.canonicalize();
        c
    }
}

// ---------- rank / select ----------

impl Container {
    /// `rank(x)`：容器中严格小于 `x` 的元素个数（`0..=65536`）。
    pub fn rank(&self, x: u16) -> usize {
        match self {
            // 严格小于 x 的位置即插入点；x=65536 的语义由上层在调用前处理，
            // 这里 x 为 u16，最大 65535，rank(65535)=len 由上层包装处理。
            Container::Array(v) => v.partition_point(|&e| e < x),
            Container::Bitmap(b) => {
                let idx = x as usize;
                let full_words = idx / 64;
                let mut n: usize = b[..full_words]
                    .iter()
                    .map(|w| w.count_ones() as usize)
                    .sum();
                let rem = idx % 64;
                if rem != 0 {
                    n += (b[full_words] & ((1u64 << rem) - 1)).count_ones() as usize;
                }
                n
            }
        }
    }

    /// 容器内的 rank：把容器视作覆盖 `[0, 65535]`，`x` 以 u32 传入（0..=65536）。
    /// `rank(65536) == len()`。
    pub fn rank_full(&self, x: u32) -> usize {
        debug_assert!(x <= crate::CONTAINER_BITS);
        if x >= crate::CONTAINER_BITS {
            self.len()
        } else {
            self.rank(x as u16)
        }
    }

    /// `select(i)`：容器中第 `i` 小的元素（0 基下标）；越界返回 `None`。
    pub fn select(&self, i: usize) -> Option<u16> {
        if i >= self.len() {
            return None;
        }
        match self {
            Container::Array(v) => v.get(i).copied(),
            Container::Bitmap(b) => {
                let mut remaining = i;
                for (wi, word) in b.iter().enumerate() {
                    let ones = word.count_ones() as usize;
                    if remaining < ones {
                        // 找该字内第 remaining 个置位。
                        let mut w = *word;
                        for _ in 0..remaining {
                            w &= w - 1;
                        }
                        let bit = w.trailing_zeros() as u16;
                        return Some(wi as u16 * 64 + bit);
                    }
                    remaining -= ones;
                }
                None
            }
        }
    }

    /// 向 `x` 之后（含 `x`）找第一个存在的元素，不存在返回 `None`。
    pub fn next_value(&self, x: u16) -> Option<u16> {
        match self {
            Container::Array(v) => {
                let pos = v.partition_point(|&e| e < x);
                v.get(pos).copied()
            }
            Container::Bitmap(b) => {
                let start = x as usize;
                let wi = start / 64;
                let rem = start % 64;
                let mask = u64::MAX << rem;
                if b[wi] & mask != 0 {
                    let bit = (b[wi] & mask).trailing_zeros() as u16;
                    return Some(wi as u16 * 64 + bit);
                }
                for w in wi + 1..BITMAP_WORDS {
                    if b[w] != 0 {
                        return Some(w as u16 * 64 + b[w].trailing_zeros() as u16);
                    }
                }
                None
            }
        }
    }
}

// ---------- 位操作工具 ----------

#[inline]
pub(crate) fn get_bit(b: &[u64; BITMAP_WORDS], low: u16) -> bool {
    b[low as usize / 64] >> (low % 64) & 1 != 0
}

#[inline]
pub(crate) fn set_bit(b: &mut [u64; BITMAP_WORDS], low: u16) {
    b[low as usize / 64] |= 1u64 << (low % 64);
}

#[inline]
pub(crate) fn clear_bit(b: &mut [u64; BITMAP_WORDS], low: u16) {
    b[low as usize / 64] &= !(1u64 << (low % 64));
}

/// 迭代器：避免每次迭代都收集成 Vec。
pub enum ContainerIter<'a> {
    Array(std::slice::Iter<'a, u16>),
    Bitmap {
        words: &'a [u64; BITMAP_WORDS],
        /// 当前扫描到的字下标。
        wi: usize,
        /// 当前字中尚未产出的置位。
        current: u64,
    },
}

impl Iterator for ContainerIter<'_> {
    type Item = u16;

    fn next(&mut self) -> Option<u16> {
        match self {
            ContainerIter::Array(it) => it.next().copied(),
            ContainerIter::Bitmap { words, wi, current } => loop {
                if *current != 0 {
                    let bit = current.trailing_zeros();
                    *current &= *current - 1;
                    return Some(*wi as u16 * 64 + bit as u16);
                }
                if *wi + 1 >= BITMAP_WORDS {
                    return None;
                }
                *wi += 1;
                *current = words[*wi];
            },
        }
    }
}

// 仅 debug 断言使用的有序唯一性检查（兼容较老 rustc，不用 slice::is_sorted）。
fn is_sorted_unique(v: &[u16]) -> bool {
    v.windows(2).all(|w| w[0] < w[1])
}
