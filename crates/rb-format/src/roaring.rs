//! 分层集合：按高 16 位分片的容器有序集合。

use crate::container::Container;
use crate::ops;

/// 32 位整数上的分层位图集合。
///
/// 内部分片按高 16 位键严格升序存储；空容器不保留。
/// 所有表示在构造后都满足 [`Container`] 的阈值不变量。
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct RoaringSet {
    /// 按键升序的并行结构（键与容器一一对应）。
    keys: Vec<u16>,
    containers: Vec<Container>,
}

impl RoaringSet {
    /// 空集合。
    pub fn new() -> Self {
        RoaringSet {
            keys: Vec::new(),
            containers: Vec::new(),
        }
    }

    /// 从任意 u32 迭代器构建（自动去重排序与规范化）。
    pub fn from_values<I: IntoIterator<Item = u32>>(iter: I) -> Self {
        let mut s = RoaringSet::new();
        for v in iter {
            s.insert(v);
        }
        s
    }

    // ---------- 基础变更 / 查询 ----------

    /// 插入值，返回是否为新增。
    pub fn insert(&mut self, value: u32) -> bool {
        let (key, low) = split(value);
        match self.keys.binary_search(&key) {
            Ok(idx) => self.containers[idx].insert(low),
            Err(idx) => {
                let mut c = Container::new();
                c.insert(low);
                self.keys.insert(idx, key);
                self.containers.insert(idx, c);
                true
            }
        }
    }

    /// 删除值，返回是否原本存在。
    pub fn remove(&mut self, value: u32) -> bool {
        let (key, low) = split(value);
        if let Ok(idx) = self.keys.binary_search(&key) {
            let existed = self.containers[idx].remove(low);
            if existed && self.containers[idx].is_empty() {
                self.keys.remove(idx);
                self.containers.remove(idx);
            }
            existed
        } else {
            false
        }
    }

    #[inline]
    pub fn contains(&self, value: u32) -> bool {
        let (key, low) = split(value);
        match self.keys.binary_search(&key) {
            Ok(idx) => self.containers[idx].contains(low),
            Err(_) => false,
        }
    }

    /// 分片数量（非空容器数）。
    #[inline]
    pub fn container_count(&self) -> usize {
        self.keys.len()
    }

    /// 有序分片键（供运算/序列化模块使用）。
    #[inline]
    pub fn keys(&self) -> &[u16] {
        &self.keys
    }

    /// 按下标取容器（与 [`RoaringSet::keys`] 对应）。
    #[inline]
    pub fn container_at(&self, idx: usize) -> &Container {
        &self.containers[idx]
    }

    /// 按高 16 位键取容器。
    pub fn container(&self, key: u16) -> Option<&Container> {
        self.keys
            .binary_search(&key)
            .ok()
            .map(|i| &self.containers[i])
    }

    /// 直接放入一个已构造的容器（反序列化与集合运算使用）。
    /// 会重新规范化；空容器被忽略。
    pub(crate) fn insert_container(&mut self, key: u16, mut c: Container) {
        c.canonicalize();
        if c.is_empty() {
            return;
        }
        match self.keys.binary_search(&key) {
            Ok(idx) => self.containers[idx] = c,
            Err(idx) => {
                self.keys.insert(idx, key);
                self.containers.insert(idx, c);
            }
        }
    }

    /// 集合基数。各容器基数之和用 u64 饱和累加——满集为 2^32，不溢出。
    pub fn len_u64(&self) -> u64 {
        self.containers
            .iter()
            .map(|c| c.len() as u64)
            .fold(0u64, u64::saturating_add)
    }

    /// 集合基数，返回 usize（64 位平台与 [`Self::len_u64`] 等价）。
    pub fn len(&self) -> usize {
        self.len_u64() as usize
    }

    #[inline]
    pub fn is_empty(&self) -> bool {
        self.keys.is_empty()
    }

    /// 清空。
    pub fn clear(&mut self) {
        self.keys.clear();
        self.containers.clear();
    }

    /// 升序遍历所有 32 位值。
    pub fn iter(&self) -> RoaringIter<'_> {
        RoaringIter {
            set: self,
            ci: 0,
            inner: None,
        }
    }

    /// 升序收集为 Vec（主要用于测试对照与小规模导出）。
    pub fn to_vec(&self) -> Vec<u32> {
        self.iter().collect()
    }

    /// 最小值 / 最大值。
    pub fn min(&self) -> Option<u32> {
        let k = *self.keys.first()?;
        let c = self.containers.first()?;
        c.select(0).map(|low| combine(k, low))
    }

    pub fn max(&self) -> Option<u32> {
        let k = *self.keys.last()?;
        let c = self.containers.last()?;
        c.select(c.len() - 1).map(|low| combine(k, low))
    }

    // ---------- rank / select ----------

    /// `rank(x)`：集合中严格小于 `x` 的元素个数（u64 计数，满边界不溢出）。
    pub fn rank(&self, x: u32) -> u64 {
        let (key, low) = split(x);
        let idx = match self.keys.binary_search(&key) {
            Ok(i) => i,
            Err(i) => i,
        };
        // 所有键 < key 的容器整体计入（每容器 <= 65536，最多 65535 个：< 2^32）。
        let mut total: u64 = 0;
        for c in &self.containers[..idx] {
            total = total.saturating_add(c.len() as u64);
        }
        if idx < self.keys.len() && self.keys[idx] == key {
            total = total.saturating_add(self.containers[idx].rank_full(low as u32) as u64);
        }
        total
    }

    /// 第 `i` 小的元素（0 基），`i >= 基数` 返回 `None`。
    pub fn select(&self, i: u64) -> Option<u32> {
        if i >= self.len_u64() {
            return None;
        }
        let mut remaining = i;
        for (k, c) in self.keys.iter().zip(self.containers.iter()) {
            let n = c.len() as u64;
            if remaining < n {
                return c.select(remaining as usize).map(|low| combine(*k, low));
            }
            remaining -= n;
        }
        None
    }

    // ---------- 集合运算（委托 ops，返回新集合） ----------

    pub fn union(&self, other: &RoaringSet) -> RoaringSet {
        ops::set_union(self, other)
    }

    pub fn intersect(&self, other: &RoaringSet) -> RoaringSet {
        ops::set_intersect(self, other)
    }

    pub fn difference(&self, other: &RoaringSet) -> RoaringSet {
        ops::set_difference(self, other)
    }

    pub fn intersects(&self, other: &RoaringSet) -> bool {
        ops::set_intersects(self, other)
    }

    /// 子集判定：`self ⊆ other`。
    pub fn is_subset(&self, other: &RoaringSet) -> bool {
        for (k, c) in self.keys.iter().zip(self.containers.iter()) {
            match other.keys.binary_search(k) {
                Ok(j) => {
                    if !container_subset(c, &other.containers[j]) {
                        return false;
                    }
                }
                Err(_) => return false,
            }
        }
        true
    }
}

/// 标准库 `FromIterator`（`collect()` 支持）；语义等价于 [`RoaringSet::from_values`]。
impl std::iter::FromIterator<u32> for RoaringSet {
    fn from_iter<I: IntoIterator<Item = u32>>(iter: I) -> Self {
        RoaringSet::from_values(iter)
    }
}

/// 升序值迭代器（不要求先整体展开，逐容器产出）。
pub struct RoaringIter<'a> {
    set: &'a RoaringSet,
    /// 下一个待访问的容器下标。
    ci: usize,
    /// 当前容器内迭代器及其高 16 位键。
    inner: Option<(u16, crate::container::ContainerIter<'a>)>,
}

impl Iterator for RoaringIter<'_> {
    type Item = u32;

    fn next(&mut self) -> Option<u32> {
        loop {
            if let Some((key, it)) = self.inner.as_mut() {
                if let Some(low) = it.next() {
                    return Some(combine(*key, low));
                }
            }
            if self.ci >= self.set.containers.len() {
                return None;
            }
            let key = self.set.keys[self.ci];
            self.inner = Some((key, self.set.containers[self.ci].iter()));
            self.ci += 1;
        }
    }
}

// ---------- 辅助 ----------

#[inline]
fn split(v: u32) -> (u16, u16) {
    ((v >> 16) as u16, (v & 0xFFFF) as u16)
}

#[inline]
fn combine(high: u16, low: u16) -> u32 {
    ((high as u32) << 16) | low as u32
}

/// 容器子集判定（位图路径逐字 `a & !b == 0`，数组路径双指针）。
fn container_subset(a: &Container, b: &Container) -> bool {
    match (a, b) {
        (Container::Bitmap(x), Container::Bitmap(y)) => {
            x.iter().zip(y.iter()).all(|(p, q)| p & !q == 0)
        }
        (Container::Array(av), Container::Bitmap(bm)) => {
            av.iter().all(|&v| bm[v as usize / 64] >> (v % 64) & 1 != 0)
        }
        (Container::Bitmap(_), Container::Array(_)) => {
            // 数组最多 4096 元素，不可能包含基数 >4096 的位图。
            false
        }
        (Container::Array(x), Container::Array(y)) => {
            let (mut i, mut j) = (0, 0);
            while i < x.len() {
                while j < y.len() && y[j] < x[i] {
                    j += 1;
                }
                if j >= y.len() || y[j] != x[i] {
                    return false;
                }
                i += 1;
                j += 1;
            }
            true
        }
    }
}
