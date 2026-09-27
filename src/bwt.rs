//! BWT（Burrows–Wheeler Transform）与 C 频次表。
//!
//! 设编码序列 `T`（长度 n，末尾是唯一最小哨兵）后缀数组为 `sa`。
//! FM 索引的矩阵按后缀排序，第 i 行对应后缀 `T[sa[i]..]`，其“前一字符”列：
//!
//! ```text
//! L[i] = if sa[i] == 0 { T[n-1]（哨兵） } else { T[sa[i] - 1] }
//! ```
//!
//! 哨兵因此在 BWT 中出现且仅出现一次，位于 sa[i]=0 的那一行（行号不固定，
//! 作为 `sentinel_row` 显式存入索引）。首行 sa[0] 恒等于 n-1（最短的哨兵后缀），
//! 其 L 是最后一个正文字符。LF 映射 `LF(i)=C[L[i]]+rank_{L[i]}(i)`
//! 把后缀行 i 映到“起点前移一位”的后缀行。

use crate::alphabet::ALPHABET_SIZE;

/// 由后缀数组构造 BWT 最后一列（FM 约定：首行为哨兵）。
pub fn build_bwt(syms: &[u16], sa: &[u32]) -> Vec<u16> {
    assert_eq!(syms.len(), sa.len(), "SA 长度必须与编码序列一致");
    let n = syms.len();
    sa.iter()
        .map(|&p| {
            let p = p as usize;
            if p == 0 {
                // 起点 0 的后缀（首行）前面没有正文，其 BWT 字符是末尾哨兵。
                syms[n - 1]
            } else {
                syms[p - 1]
            }
        })
        .collect()
}

/// 构造 C 表：`C[c]` = 严格小于符号 c 的符号总数（首列起始位置）。
/// 返回长度 257 的数组，满足 `C[0] = 0`。
pub fn build_c(syms: &[u16]) -> [u64; ALPHABET_SIZE] {
    let mut counts = [0u64; ALPHABET_SIZE];
    for &s in syms {
        counts[s as usize] += 1;
    }
    let mut c = [0u64; ALPHABET_SIZE];
    for i in 1..ALPHABET_SIZE {
        c[i] = c[i - 1] + counts[i - 1];
    }
    c
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::alphabet::encode_text;
    use crate::suffix_array::build_suffix_array;

    #[test]
    fn banana_bwt_and_c() {
        // banana$ 后缀数组 sa=[6,5,3,1,0,4,2]，BWT 最后一列 L=[a,n,n,b,$,a,a]，
        // 哨兵在 sa=0 所在的第 4 行（sentinel_row=4），首行 sa=6，L[0]=a。
        let t = encode_text(b"banana");
        let sa = build_suffix_array(&t);
        let l = build_bwt(&t, &sa);
        let a = b'a' as u16 + 1;
        let b = b'b' as u16 + 1;
        let n = b'n' as u16 + 1;
        // 输出符号对照：'a'=98 'b'=99 'n'=111
        assert_eq!(l, vec![a, n, n, b, 0, a, a]);
        assert_eq!(
            l.iter()
                .map(|&s| if s == 0 { b'$' } else { (s - 1) as u8 })
                .collect::<Vec<_>>(),
            b"annb$aa"
        );

        let c = build_c(&t);
        assert_eq!(c[0], 0); // 没有比哨兵小的
        assert_eq!(c[1], 1); // 比 'a' 小的只有哨兵
        assert_eq!(c[b as usize], 4); // $ + a×3
        assert_eq!(c[n as usize], 5); // $ + a×3 + b
    }

    #[test]
    fn bwt_is_a_permutation_of_text() {
        let t = encode_text(b"abracadabra");
        let sa = build_suffix_array(&t);
        let l = build_bwt(&t, &sa);
        let mut x = l.clone();
        let mut y = t.clone();
        x.sort_unstable();
        y.sort_unstable();
        assert_eq!(x, y, "BWT 必须是原序列的一个排列");
    }
}
