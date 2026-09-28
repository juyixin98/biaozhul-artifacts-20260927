//! FM-index core: backwards search, LF mapping and sampled localization.
//!
//! Conventions (shared across the crate):
//!
//! * Coded text: sentinel `0`, bytes mapped to `1..=256` (see [`crate::coding`]).
//! * Intervals are **half-open** `[lo, hi)` over BWT/SA rows; an empty
//!   interval means "no match".
//! * Backwards search walks pattern symbols right-to-left with
//!   `LF`-style updates `lo = C[c] + occ(c, lo)`, `hi = C[c] + occ(c, hi)`.
//! * Localization follows the LF permutation from a result row until a row
//!   whose index is a multiple of `sample_interval`, then adds the number of
//!   LF steps to the stored suffix-array value (or terminates at the unique
//!   sentinel-L row, whose suffix position is 0).
//!
//! Edge semantics (defined explicitly, not left to accident):
//!
//! * **Empty pattern** matches every suffix: interval `[0, n)`, locations
//!   `0..=text.len()`.
//! * **Pattern longer than the text** yields the empty interval immediately;
//!   it is not an error.

use crate::bwt::{build_bwt, build_c_table};
use crate::coding::{ALPHABET_SIZE, SENTINEL, code_with_sentinel};
use crate::error::{Error, Result};
use crate::rank::OccTable;
use crate::suffix::build_sa;

/// Lower/upper bounds enforced at build time and on load.
pub const MIN_SAMPLE_INTERVAL: u32 = 1;
pub const MAX_SAMPLE_INTERVAL: u32 = 65_535;

#[derive(Debug, Clone)]
pub struct FmIndex {
    /// Original, uncoded text.
    text: Vec<u8>,
    /// Coded length = text.len() + 1 (the sentinel).
    n: usize,
    c_table: [u64; ALPHABET_SIZE],
    occ: OccTable,
    /// Store one SA value every `sample_interval` rows (rows 0, K, 2K, ...).
    sample_interval: u32,
    /// Dense samples: `sa_samples[r / K] = SA[r]` for rows with `r % K == 0`.
    sa_samples: Vec<u32>,
}

/// Result of a backwards search. `positions` is sorted ascending.
#[derive(Debug, Clone)]
pub struct SearchOutcome {
    pub lo: u64,
    pub hi: u64,
    /// Number of matching occurrences (`hi - lo`).
    pub count: u64,
    /// Sorted ascending text offsets of every match.
    pub positions: Vec<u64>,
    pub empty_pattern: bool,
}

impl SearchOutcome {
    pub fn is_match(&self) -> bool {
        self.hi > self.lo
    }
}

impl FmIndex {
    /// Build an index over `text` with SA sampled every `sample_interval` rows.
    pub fn build(text: Vec<u8>, sample_interval: u32) -> Result<Self> {
        if text.is_empty() {
            return Err(Error::EmptyText);
        }
        if !(MIN_SAMPLE_INTERVAL..=MAX_SAMPLE_INTERVAL).contains(&sample_interval) {
            return Err(Error::BadSampleInterval {
                got: sample_interval,
            });
        }
        let coded = code_with_sentinel(&text);
        let n = coded.len();
        let sa = build_sa(&coded)?;
        let bwt = build_bwt(&coded, &sa);
        let c_table = build_c_table(&coded);
        let occ = OccTable::new(bwt);
        occ.debug_assert_well_formed();

        let k = sample_interval as usize;
        let mut sa_samples = Vec::with_capacity(n / k + 1);
        for r in (0..n).step_by(k) {
            sa_samples.push(sa[r]);
        }

        let idx = Self {
            text,
            n,
            c_table,
            occ,
            sample_interval,
            sa_samples,
        };
        idx.debug_self_check();
        Ok(idx)
    }

    /// Reconstruct an index from persisted components, re-deriving C and the
    /// rank structure and checking every cross-invariant.
    pub fn from_persisted(
        text: Vec<u8>,
        bwt: Vec<u16>,
        sample_interval: u32,
        sa_samples: Vec<u32>,
    ) -> Result<Self> {
        if text.is_empty() {
            return Err(Error::PersistenceCorrupt {
                section: "header".into(),
                detail: "empty text".into(),
            });
        }
        if !(MIN_SAMPLE_INTERVAL..=MAX_SAMPLE_INTERVAL).contains(&sample_interval) {
            return Err(Error::PersistenceCorrupt {
                section: "header".into(),
                detail: format!("sample interval {sample_interval} out of range"),
            });
        }
        let n = text.len() + 1;
        if bwt.len() != n {
            return Err(Error::PersistenceCorrupt {
                section: "bwt".into(),
                detail: format!("BWT length {} != text length + 1 ({n})", bwt.len()),
            });
        }
        if bwt.iter().any(|&s| s as usize >= ALPHABET_SIZE) {
            return Err(Error::PersistenceCorrupt {
                section: "bwt".into(),
                detail: "symbol outside the 257-symbol alphabet".into(),
            });
        }
        if bwt.iter().filter(|&&s| s == SENTINEL).count() != 1 {
            return Err(Error::PersistenceCorrupt {
                section: "bwt".into(),
                detail: "BWT must contain exactly one sentinel".into(),
            });
        }
        let expected_samples = n.div_ceil(sample_interval as usize);
        if sa_samples.len() != expected_samples {
            return Err(Error::PersistenceCorrupt {
                section: "sa_samples".into(),
                detail: format!(
                    "expected {expected_samples} SA samples for interval {sample_interval}, found {}",
                    sa_samples.len()
                ),
            });
        }
        if sa_samples.iter().any(|&v| v as usize >= n) {
            return Err(Error::PersistenceCorrupt {
                section: "sa_samples".into(),
                detail: "sampled SA value out of range".into(),
            });
        }

        let coded = code_with_sentinel(&text);
        let c_table = build_c_table(&coded);
        let occ = OccTable::new(bwt);

        let idx = Self {
            text,
            n,
            c_table,
            occ,
            sample_interval,
            sa_samples,
        };
        idx.verify_against_coded(&coded)?;
        idx.debug_self_check();
        Ok(idx)
    }

    pub fn text(&self) -> &[u8] {
        &self.text
    }

    pub fn text_len(&self) -> u64 {
        (self.n - 1) as u64
    }

    pub fn coded_len(&self) -> u64 {
        self.n as u64
    }

    pub fn sample_interval(&self) -> u32 {
        self.sample_interval
    }

    pub fn c_table(&self) -> &[u64; ALPHABET_SIZE] {
        &self.c_table
    }

    pub fn occ_table(&self) -> &OccTable {
        &self.occ
    }

    pub fn bwt(&self) -> &[u16] {
        self.occ.bwt()
    }

    pub fn sa_samples(&self) -> &[u32] {
        &self.sa_samples
    }

    /// LF permutation: row of the suffix obtained by prepending `L[row]`.
    ///
    /// `LF(row) = C[L[row]] + occ(L[row], row)` with the exclusive occurrence
    /// count of `L[row]` in `BWT[0..row]`.
    #[inline]
    pub fn lf(&self, row: u64) -> u64 {
        let sym = self.occ.symbol_at(row as usize);
        self.c_table[sym as usize] + self.occ.occ(sym, row as usize)
    }

    /// Half-open interval of rows whose suffix starts with `pattern`, obtained
    /// purely by backwards search (no localization).
    pub fn interval(&self, pattern: &[u8]) -> (u64, u64) {
        if pattern.is_empty() {
            return (0, self.n as u64);
        }
        if pattern.len() > self.n - 1 {
            return (0, 0); // pattern longer than the text cannot match
        }
        let mut lo = 0u64;
        let mut hi = self.n as u64;
        for &byte in pattern.iter().rev() {
            let sym = byte as u16 + 1;
            lo = self.c_table[sym as usize] + self.occ.occ(sym, lo as usize);
            hi = self.c_table[sym as usize] + self.occ.occ(sym, hi as usize);
            if lo == hi {
                return (lo, hi);
            }
        }
        (lo, hi)
    }

    /// Full query: backwards search plus localization of every hit.
    pub fn search(&self, pattern: &[u8]) -> SearchOutcome {
        let empty_pattern = pattern.is_empty();
        let (lo, hi) = self.interval(pattern);

        let mut positions: Vec<u64> = if empty_pattern {
            (0..self.n as u64).collect()
        } else {
            let mut v = Vec::with_capacity((hi - lo) as usize);
            for row in lo..hi {
                v.push(self.locate_row(row));
            }
            v
        };
        positions.sort_unstable();

        SearchOutcome {
            lo,
            hi,
            count: hi - lo,
            positions,
            empty_pattern,
        }
    }

    /// Resolve a single BWT row to its text offset via LF + SA samples.
    ///
    /// LF maps the suffix at position `p` to the suffix at position `p - 1`
    /// (with wrap-around at 0). After `steps` LF applications the current
    /// row's suffix position is therefore `p0 - steps`; once the current row
    /// is a sampled row, `p0 = sampled + steps`.
    ///
    /// Termination: the walk reaches a sampled row within at most K steps, or
    /// earlier hits the unique row whose L symbol is the sentinel, whose
    /// suffix position is 0; then `p0 = steps`.
    pub fn locate_row(&self, row: u64) -> u64 {
        let k = self.sample_interval as u64;
        let mut r = row;
        let mut steps = 0u64;
        loop {
            if self.occ.symbol_at(r as usize) == SENTINEL {
                // Current suffix position is 0 => original was `steps`.
                return steps;
            }
            if r.is_multiple_of(k) {
                let sampled = self.sa_samples[(r / k) as usize] as u64;
                return sampled + steps;
            }
            r = self.lf(r);
            steps += 1;
        }
    }

    /// Expensive re-derivation used after loading persisted data: rebuild SA
    /// from the text and confirm BWT, C-table permutation and SA samples agree.
    fn verify_against_coded(&self, coded: &[u16]) -> Result<()> {
        let corrupt = |section: &'static str, detail: String| Error::PersistenceCorrupt {
            section: section.into(),
            detail,
        };
        // Frequency agreement: BWT must be a permutation of the coded text.
        let mut from_bwt = [0u64; ALPHABET_SIZE];
        for &s in self.occ.bwt() {
            from_bwt[s as usize] += 1;
        }
        if from_bwt != self.c_table_total_counts(coded) {
            return Err(corrupt(
                "bwt",
                "BWT symbol frequencies disagree with the coded text".into(),
            ));
        }
        // Full SA rebuild, then check each stored sample.
        let sa = build_sa(coded).map_err(|e| corrupt("sa_samples", e.to_string()))?;
        let rebuilt_bwt = build_bwt(coded, &sa);
        if rebuilt_bwt != self.occ.bwt() {
            return Err(corrupt("bwt", "rebuilt BWT differs from stored BWT".into()));
        }
        let k = self.sample_interval as usize;
        for (i, &v) in self.sa_samples.iter().enumerate() {
            let row = i * k;
            if sa[row] != v {
                return Err(corrupt(
                    "sa_samples",
                    format!(
                        "SA sample at row {row} is {v}, rebuilt value is {}",
                        sa[row]
                    ),
                ));
            }
        }
        Ok(())
    }

    fn c_table_total_counts(&self, coded: &[u16]) -> [u64; ALPHABET_SIZE] {
        let mut freq = [0u64; ALPHABET_SIZE];
        for &s in coded {
            freq[s as usize] += 1;
        }
        freq
    }

    fn debug_self_check(&self) {
        debug_assert_eq!(self.occ.total(SENTINEL), 1);
        debug_assert_eq!(self.occ.len(), self.n);
        // Localizing every row must give the permutation 0..n.
        let mut resolved: Vec<u64> = (0..self.n as u64).map(|r| self.locate_row(r)).collect();
        resolved.sort_unstable();
        debug_assert_eq!(resolved, (0..self.n as u64).collect::<Vec<_>>());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::reference::naive_scan;

    #[test]
    fn build_rejects_bad_arguments() {
        assert!(matches!(FmIndex::build(vec![], 4), Err(Error::EmptyText)));
        assert!(matches!(
            FmIndex::build(b"abc".to_vec(), 0),
            Err(Error::BadSampleInterval { got: 0 })
        ));
        assert!(matches!(
            FmIndex::build(b"abc".to_vec(), MAX_SAMPLE_INTERVAL + 1),
            Err(Error::BadSampleInterval { .. })
        ));
    }

    #[test]
    fn empty_pattern_and_too_long_pattern_semantics() {
        let idx = FmIndex::build(b"abc".to_vec(), 2).unwrap();
        let all = idx.search(b"");
        assert_eq!((all.lo, all.hi), (0, 4));
        assert_eq!(all.count, 4);
        assert_eq!(all.positions, vec![0, 1, 2, 3]); // every suffix, incl. end

        let long = idx.search(b"abcd");
        assert_eq!(long.count, 0);
        assert_eq!((long.lo, long.hi), (0, 0));
        assert!(long.positions.is_empty());
    }

    #[test]
    fn overlapping_hits_on_repetitive_text() {
        let idx = FmIndex::build(b"aaaa".to_vec(), 1).unwrap();
        let out = idx.search(b"aa");
        assert_eq!(out.count, 3);
        assert_eq!(out.positions, vec![0, 1, 2]); // overlaps preserved
    }

    #[test]
    fn matches_naive_scan_for_banana_with_various_sample_rates() {
        let text = b"banana";
        for k in [1u32, 2, 3, 5, 6, 7] {
            let idx = FmIndex::build(text.to_vec(), k).unwrap();
            for pat in [
                &b""[..],
                b"a",
                b"an",
                b"ana",
                b"na",
                b"ban",
                b"xyz",
                b"banana",
                b"bananas",
            ] {
                let out = idx.search(pat);
                assert_eq!(out.positions, naive_scan(text, pat), "pat={pat:?} k={k}");
            }
        }
    }
}
