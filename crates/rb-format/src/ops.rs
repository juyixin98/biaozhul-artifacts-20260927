//! 集合运算：并（∪）、交（∩）、差（A∖B）。
//!
//! ## 非退化保证
//!
//! 所有运算都在容器内部的原生表示上直接进行，**不会**把容器展开成完整
//! 整数列表后再做朴素集合运算：
//!
//! | A \\ B | Array | Bitmap |
//! |---|---|---|
//! | **Array** | 有序双指针归并 O(|A|+|B|)，交集可提前耗尽 | 以小数组驱动：并 O(|A|)、差 O(|A|)、交 O(|A|) 位图查表 |
//! | **Bitmap** | 以小数组驱动（同上，方向取反） | 1024 字的按位 `|`/`&`/&`!`，O(1024) |
//!
//! 集合级（[`RoaringSet`](crate::RoaringSet)）运算对两侧有序的容器键做
//! 双指针归并，缺失的分片按空容器处理（差/交集直接短路为其中一方或空），
//! 结果容器经 [`Container::canonicalize`] 重新规范化。

use crate::container::Container;
use crate::roaring::RoaringSet;
use crate::BITMAP_WORDS;

// ===================================================================
// 容器级
// ===================================================================

/// 容器并集。
pub fn union(a: &Container, b: &Container) -> Container {
    match (a, b) {
        (Container::Bitmap(x), Container::Bitmap(y)) => {
            let mut out = Box::new([0u64; BITMAP_WORDS]);
            for i in 0..BITMAP_WORDS {
                out[i] = x[i] | y[i];
            }
            Container::Bitmap(out) // 基数至少 >4096（x 已超阈值），保持位图
        }
        (Container::Bitmap(bm), Container::Array(av))
        | (Container::Array(av), Container::Bitmap(bm)) => {
            let mut out = **bm;
            for &v in av {
                out[v as usize / 64] |= 1u64 << (v % 64);
            }
            Container::Bitmap(Box::new(out)) // 位图方基数已 >4096
        }
        (Container::Array(x), Container::Array(y)) => {
            let mut out = Vec::with_capacity(x.len() + y.len());
            merge_union(x, y, &mut out);
            Container::from_sorted_unique(out)
        }
    }
}

/// 容器交集。
pub fn intersect(a: &Container, b: &Container) -> Container {
    match (a, b) {
        (Container::Bitmap(x), Container::Bitmap(y)) => {
            let mut words = [0u64; BITMAP_WORDS];
            for i in 0..BITMAP_WORDS {
                words[i] = x[i] & y[i];
            }
            // 交集基数可能很小，必须规范化。
            Container::from_bitmap_words(words)
        }
        // 永远用数组一侧驱动，单次位图查表；驱动数组耗尽即结束。
        (Container::Array(av), Container::Bitmap(bm)) => {
            Container::from_sorted_unique(drive_array(av, bm, true))
        }
        (Container::Bitmap(bm), Container::Array(av)) => {
            Container::from_sorted_unique(drive_array(av, bm, true))
        }
        (Container::Array(x), Container::Array(y)) => {
            let mut out = Vec::new();
            merge_intersect(x, y, &mut out);
            // 两数组交集必 <= 各数组长度 <= 4096，仍合法；from_sorted_unique 幂等。
            Container::from_sorted_unique(out)
        }
    }
}

/// 容器差集 `a \ b`。
pub fn difference(a: &Container, b: &Container) -> Container {
    match (a, b) {
        (Container::Bitmap(x), Container::Bitmap(y)) => {
            let mut words = [0u64; BITMAP_WORDS];
            for i in 0..BITMAP_WORDS {
                words[i] = x[i] & !y[i];
            }
            Container::from_bitmap_words(words)
        }
        (Container::Array(av), Container::Bitmap(bm)) => {
            // 从数组中剔除位图命中的元素。
            Container::from_sorted_unique(drive_array(av, bm, false))
        }
        (Container::Bitmap(bm), Container::Array(av)) => {
            let mut out = **bm;
            for &v in av {
                out[v as usize / 64] &= !(1u64 << (v % 64));
            }
            Container::from_bitmap_words(out)
        }
        (Container::Array(x), Container::Array(y)) => {
            let mut out = Vec::new();
            merge_difference(x, y, &mut out);
            Container::from_sorted_unique(out)
        }
    }
}

/// 容器是否相交（交集基数 > 0），交集路径的短路版本。
pub fn intersects(a: &Container, b: &Container) -> bool {
    match (a, b) {
        (Container::Bitmap(x), Container::Bitmap(y)) => {
            x.iter().zip(y.iter()).any(|(p, q)| p & q != 0)
        }
        (Container::Array(av), Container::Bitmap(bm))
        | (Container::Bitmap(bm), Container::Array(av)) => {
            av.iter().any(|&v| bm[v as usize / 64] >> (v % 64) & 1 != 0)
        }
        (Container::Array(x), Container::Array(y)) => {
            let mut i = 0;
            let mut j = 0;
            while i < x.len() && j < y.len() {
                match x[i].cmp(&y[j]) {
                    std::cmp::Ordering::Equal => return true,
                    std::cmp::Ordering::Less => i += 1,
                    std::cmp::Ordering::Greater => j += 1,
                }
            }
            false
        }
    }
}

// ---------- 数组归并原语（线性、有序、去重） ----------

fn merge_union(x: &[u16], y: &[u16], out: &mut Vec<u16>) {
    let (mut i, mut j) = (0, 0);
    while i < x.len() && j < y.len() {
        match x[i].cmp(&y[j]) {
            std::cmp::Ordering::Less => {
                out.push(x[i]);
                i += 1;
            }
            std::cmp::Ordering::Greater => {
                out.push(y[j]);
                j += 1;
            }
            std::cmp::Ordering::Equal => {
                out.push(x[i]);
                i += 1;
                j += 1;
            }
        }
    }
    out.extend_from_slice(&x[i..]);
    out.extend_from_slice(&y[j..]);
}

fn merge_intersect(x: &[u16], y: &[u16], out: &mut Vec<u16>) {
    let (mut i, mut j) = (0, 0);
    while i < x.len() && j < y.len() {
        match x[i].cmp(&y[j]) {
            std::cmp::Ordering::Equal => {
                out.push(x[i]);
                i += 1;
                j += 1;
            }
            std::cmp::Ordering::Less => i += 1,
            std::cmp::Ordering::Greater => j += 1,
        }
    }
}

fn merge_difference(x: &[u16], y: &[u16], out: &mut Vec<u16>) {
    let (mut i, mut j) = (0, 0);
    while i < x.len() && j < y.len() {
        match x[i].cmp(&y[j]) {
            std::cmp::Ordering::Less => {
                out.push(x[i]);
                i += 1;
            }
            std::cmp::Ordering::Equal => {
                i += 1;
                j += 1;
            }
            std::cmp::Ordering::Greater => j += 1,
        }
    }
    out.extend_from_slice(&x[i..]);
}

/// 用小数组驱动位图查表：`keep_hit == true` 保留命中（交集），否则保留未命中（差）。
fn drive_array(av: &[u16], bm: &[u64; BITMAP_WORDS], keep_hit: bool) -> Vec<u16> {
    let mut out = Vec::new();
    for &v in av {
        let hit = bm[v as usize / 64] >> (v % 64) & 1 != 0;
        if hit == keep_hit {
            out.push(v);
        }
    }
    out
}

// ===================================================================
// 集合级（键有序双指针归并）
// ===================================================================

/// 两个 [`RoaringSet`] 的并集。
pub fn set_union(a: &RoaringSet, b: &RoaringSet) -> RoaringSet {
    let mut out = RoaringSet::new();
    let (mut i, mut j) = (0usize, 0usize);
    let (ka, kb) = (a.keys(), b.keys());
    while i < ka.len() && j < kb.len() {
        match ka[i].cmp(&kb[j]) {
            std::cmp::Ordering::Equal => {
                out.insert_container(ka[i], union(a.container_at(i), b.container_at(j)));
                i += 1;
                j += 1;
            }
            std::cmp::Ordering::Less => {
                out.insert_container(ka[i], a.container_at(i).clone());
                i += 1;
            }
            std::cmp::Ordering::Greater => {
                out.insert_container(kb[j], b.container_at(j).clone());
                j += 1;
            }
        }
    }
    append_tail(&mut out, a, i);
    append_tail(&mut out, b, j);
    out
}

/// 把 `set` 中从下标 `from` 起的容器（按既有顺序）复制进结果。
fn append_tail(out: &mut RoaringSet, set: &RoaringSet, from: usize) {
    for (idx, key) in set.keys().iter().enumerate().skip(from) {
        out.insert_container(*key, set.container_at(idx).clone());
    }
}

/// 交集：键不共有的分片直接跳过，不产生空容器。
pub fn set_intersect(a: &RoaringSet, b: &RoaringSet) -> RoaringSet {
    let mut out = RoaringSet::new();
    let (mut i, mut j) = (0usize, 0usize);
    let (ka, kb) = (a.keys(), b.keys());
    while i < ka.len() && j < kb.len() {
        match ka[i].cmp(&kb[j]) {
            std::cmp::Ordering::Equal => {
                let c = intersect(a.container_at(i), b.container_at(j));
                if !c.is_empty() {
                    out.insert_container(ka[i], c);
                }
                i += 1;
                j += 1;
            }
            std::cmp::Ordering::Less => i += 1,
            std::cmp::Ordering::Greater => j += 1,
        }
    }
    out
}

/// 差集 `a \ b`。
pub fn set_difference(a: &RoaringSet, b: &RoaringSet) -> RoaringSet {
    let mut out = RoaringSet::new();
    let (mut i, mut j) = (0usize, 0usize);
    let (ka, kb) = (a.keys(), b.keys());
    while i < ka.len() {
        let key = ka[i];
        if j < kb.len() && kb[j] == key {
            let c = difference(a.container_at(i), b.container_at(j));
            if !c.is_empty() {
                out.insert_container(key, c);
            }
            i += 1;
            j += 1;
        } else if j < kb.len() && kb[j] < key {
            j += 1; // b 独有的键与差集无关，推进 b
        } else {
            out.insert_container(key, a.container_at(i).clone());
            i += 1;
        }
    }
    out
}

/// 两个集合是否相交（任一容器对相交即真）。
pub fn set_intersects(a: &RoaringSet, b: &RoaringSet) -> bool {
    let (mut i, mut j) = (0usize, 0usize);
    let (ka, kb) = (a.keys(), b.keys());
    while i < ka.len() && j < kb.len() {
        match ka[i].cmp(&kb[j]) {
            std::cmp::Ordering::Equal => {
                if intersects(a.container_at(i), b.container_at(j)) {
                    return true;
                }
                i += 1;
                j += 1;
            }
            std::cmp::Ordering::Less => i += 1,
            std::cmp::Ordering::Greater => j += 1,
        }
    }
    false
}
