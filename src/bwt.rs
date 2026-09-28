//! Burrows-Wheeler transform and the C table.
//!
//! Given suffix array `sa` of the sentinel-terminated coded text `t` of
//! length `n`:
//!
//! ```text
//! bwt[i] = t[sa[i] - 1]   if sa[i] > 0
//! bwt[i] = SENTINEL       if sa[i] == 0   // row 0, predecessor wraps to end
//! ```
//!
//! Because the unique sentinel is the smallest symbol and sits at `t[n-1]`,
//! `sa[0] == n - 1` and therefore `bwt[0] == t[n-2]` (or the sentinel for a
//! one-symbol text). The sentinel's own occurrence in the BWT is at the row
//! whose predecessor is position 0, i.e. the row `i` with `sa[i] == 1`.

use crate::coding::{ALPHABET_SIZE, SENTINEL};

/// Compute the BWT symbols (one per SA row).
pub fn build_bwt(coded: &[u16], sa: &[u32]) -> Vec<u16> {
    debug_assert_eq!(coded.len(), sa.len());
    sa.iter()
        .map(|&p| {
            let p = p as usize;
            if p == 0 { SENTINEL } else { coded[p - 1] }
        })
        .collect()
}

/// C[c] = number of coded symbols strictly smaller than `c`.
///
/// Indexed by the 257-symbol alphabet; entry 0 is always 0.
pub fn build_c_table(coded: &[u16]) -> [u64; ALPHABET_SIZE] {
    let mut freq = [0u64; ALPHABET_SIZE];
    for &s in coded {
        freq[s as usize] += 1;
    }
    let mut c = [0u64; ALPHABET_SIZE];
    let mut smaller = 0u64;
    for (sym, f) in freq.iter().enumerate() {
        c[sym] = smaller;
        smaller += *f;
    }
    c
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::coding::code_with_sentinel;
    use crate::suffix::build_sa;

    #[test]
    fn bwt_of_banana() {
        // text "banana" -> coded banana$ , classic BWT (with terminal $):
        // rotations sort to "annb$aa" up to the exact sentinel convention.
        let coded = code_with_sentinel(b"banana");
        let sa = build_sa(&coded).unwrap();
        let bwt = build_bwt(&coded, &sa);
        // Decode symbols to bytes for the readable assertion; 0 -> '$'.
        let as_str: String = bwt
            .iter()
            .map(|&s| {
                if s == SENTINEL {
                    '$'
                } else {
                    (s - 1) as u8 as char
                }
            })
            .collect();
        assert_eq!(as_str, "annb$aa");
    }

    #[test]
    fn bwt_is_a_permutation_of_coded_text() {
        for text in [&b"mississippi"[..], b"\x00\x00\xff\x00", b"a"] {
            let coded = code_with_sentinel(text);
            let sa = build_sa(&coded).unwrap();
            let mut bwt = build_bwt(&coded, &sa);
            let mut orig = coded.clone();
            bwt.sort();
            orig.sort();
            assert_eq!(bwt, orig, "BWT must permute coded text ({text:?})");
        }
    }

    #[test]
    fn c_table_is_cumulative() {
        let coded = code_with_sentinel(b"aba"); // symbols: a->98? no: a=1? 'a'+1
        let c = build_c_table(&coded);
        assert_eq!(c[0], 0); // nothing smaller than sentinel
        // Sorted coded text is [$, a, a, b, b? no...]: "$aba": $ < a < a < b.
        // C['a'] = count(smaller than 'a') = 1 (the sentinel).
        assert_eq!(c[(b'a' + 1) as usize], 1);
        // C['b'] = 1 sentinel + 2 a's = 3.
        assert_eq!(c[(b'b' + 1) as usize], 3);
    }
}
