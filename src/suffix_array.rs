//! 后缀数组构造：倍增法 + 计数排序（基数排序），O(n log n)。
//!
//! 输入为带哨兵的符号序列（哨兵 0 且唯一、最小），输出后缀数组 `sa`：
//! `sa[i]` 是字典序第 i 小的后缀在原文中的起点。哨兵后缀（起点 n-1）必在 `sa[0]`。
//!
//! 实现要点（倍增等价类法）：
//! 1. 按长度 1 的前缀（即符号本身）计数排序得到初始 sa，rank 直接取符号值，
//!    类号空间保留空洞（0..257），首类号恒为哨兵 0；
//! 2. 每轮把已排序的长度 k 后缀左移 k 位，得到“长度 2k 后缀中第二关键字有序”的
//!    起点列表（第二关键字不存在的后缀排最前，恰有 k 个：起点 n-k..n-1）；
//! 3. 对该列表按第一关键字 rank 做稳定计数排序，得到长度 2k 的 sa；
//! 4. 重新编号等价类，全互异即结束。

use crate::alphabet::ALPHABET_SIZE;

/// 对带哨兵的符号序列构造后缀数组。
pub fn build_suffix_array(syms: &[u16]) -> Vec<u32> {
    let n = syms.len();
    debug_assert!(n >= 1, "编码序列至少包含哨兵");
    debug_assert!(n <= u32::MAX as usize, "文本长度受 u32 寻址限制");

    // ---- 初始排序：按第一个符号计数排序（桶即字母表）----
    let mut cnt = vec![0u32; ALPHABET_SIZE];
    for &s in syms {
        cnt[s as usize] += 1;
    }
    let mut bucket_start = vec![0u32; ALPHABET_SIZE];
    for c in 1..ALPHABET_SIZE {
        bucket_start[c] = bucket_start[c - 1] + cnt[c - 1];
    }
    let mut sa = vec![0u32; n];
    let mut next_slot = bucket_start.clone();
    for (i, &s) in syms.iter().enumerate() {
        let b = s as usize;
        sa[next_slot[b] as usize] = i as u32;
        next_slot[b] += 1;
    }

    // 初始等价类：类号直接用符号值（保留空洞），计数数组按 257 个桶分配。
    let mut rank: Vec<u32> = syms.iter().map(|&s| s as u32).collect();
    let mut class_count = ALPHABET_SIZE;

    // ---- 倍增 ----
    // 经典“偏移列表 + 单轮稳定计数排序”做法：
    //   先构造按第二关键字有序的起点列表 tmp——
    //     前 k 个位置放第二关键字缺失（-∞）的起点：n-k..n-1，按起点升序；
    //     随后按旧 sa 顺序追加所有 (sa[j] >= k) 的起点（即 sa[j]-k），
    //     它们的第二关键字顺序与旧 sa 完全一致。
    //   再对 tmp 按第一关键字做稳定计数排序，得到新 sa。
    let mut tmp = vec![0u32; n];
    let mut new_rank = vec![0u32; n];
    let mut k = 1usize;
    while k < n {
        // 1) 第二关键字有序列表。
        for (slot, value) in tmp.iter_mut().take(k).enumerate() {
            *value = (n - k + slot) as u32;
        }
        let mut t = k;
        for &p in sa.iter() {
            if p as usize >= k {
                tmp[t] = p - k as u32;
                t += 1;
            }
        }

        // 2) 按第一关键字稳定计数排序。
        let mut rcnt = vec![0u32; class_count];
        for &p in tmp.iter() {
            rcnt[rank[p as usize] as usize] += 1;
        }
        let mut rstart = vec![0u32; class_count];
        for c in 1..class_count {
            rstart[c] = rstart[c - 1] + rcnt[c - 1];
        }
        for &p in tmp.iter() {
            let c = rank[p as usize] as usize;
            sa[rstart[c] as usize] = p;
            rstart[c] += 1;
        }

        // 3) 重新编号等价类（压缩为连续类号 0..classes-1）。
        new_rank[sa[0] as usize] = 0;
        let mut classes = 1u32;
        for w in 0..n - 1 {
            let p1 = sa[w] as usize;
            let p2 = sa[w + 1] as usize;
            let r1 = rank[p1];
            let r2 = rank[p2];
            let s1 = second_rank(&rank, p1, k, n);
            let s2 = second_rank(&rank, p2, k, n);
            if r1 != r2 || s1 != s2 {
                classes += 1;
            }
            new_rank[p2] = classes - 1;
        }
        std::mem::swap(&mut rank, &mut new_rank);
        class_count = classes as usize;
        if classes as usize == n {
            break;
        }
        k *= 2;
    }

    sa
}

/// 位置 `p + k` 的排名；越界视为比任何真实排名都小的哨兵名 `u32::MAX`。
#[inline]
fn second_rank(rank: &[u32], p: usize, k: usize, n: usize) -> u32 {
    let q = p + k;
    if q < n { rank[q] } else { u32::MAX }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::alphabet::{SENTINEL, encode_text};

    /// 朴素后缀排序：直接对后缀切片按字典序排序，作为独立参照。
    fn naive_sa(syms: &[u16]) -> Vec<u32> {
        let mut idx: Vec<u32> = (0..syms.len() as u32).collect();
        idx.sort_by(|&a, &b| syms[a as usize..].cmp(&syms[b as usize..]));
        idx
    }

    #[test]
    fn banana_suffix_array() {
        let s = encode_text(b"banana");
        assert_eq!(build_suffix_array(&s), vec![6, 5, 3, 1, 0, 4, 2]);
    }

    #[test]
    fn sentinel_only() {
        let s = vec![SENTINEL];
        assert_eq!(build_suffix_array(&s), vec![0]);
    }

    #[test]
    fn high_repetition_aaaa() {
        let s = encode_text(b"aaaa");
        // $ , a$, aa$, aaa$, aaaa$
        assert_eq!(build_suffix_array(&s), vec![4, 3, 2, 1, 0]);
    }

    #[test]
    fn abracadabra_matches_naive() {
        let s = encode_text(b"abracadabra");
        assert_eq!(build_suffix_array(&s), naive_sa(&s));
    }

    #[test]
    fn binary_zero_rich_matches_naive() {
        let cases: [&[u8]; 6] = [
            b"\x00".as_slice(),
            b"\x00\x00\x00".as_slice(),
            b"\x00\xff\x00\x00\xff".as_slice(),
            b"mississippi".as_slice(),
            b"\x01\x00\x02\x00\x03".as_slice(),
            b"abcabcabcabc".as_slice(),
        ];
        for c in cases {
            let s = encode_text(c);
            assert_eq!(build_suffix_array(&s), naive_sa(&s), "input={c:?}");
        }
    }

    #[test]
    fn randomized_against_naive() {
        // 确定性 LCG 伪随机，避免引入 rand 依赖；小字母表制造大量重复。
        let mut state = 0x1234_5678u32;
        let mut next = || {
            state ^= state << 13;
            state ^= state >> 17;
            state ^= state << 5;
            state
        };
        for _ in 0..500 {
            let len = (next() % 60) as usize;
            let alpha = 1 + next() % 6; // 极小字母表
            let text: Vec<u8> = (0..len).map(|_| (next() % alpha) as u8).collect();
            let s = encode_text(&text);
            assert_eq!(build_suffix_array(&s), naive_sa(&s), "text={text:?}");
        }
    }
}
