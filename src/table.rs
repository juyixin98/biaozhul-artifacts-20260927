//! Static frequency tables — validated, bounded, with prefix sums.
//!
//! A [`FreqTable`] is the *only* way the encoder or decoder obtains symbol
//! probabilities.  Validation is performed once at construction; the encoding
//! kernel then relies on the stored invariants.
//!
//! # Integer contract
//!
//! * `bound >= 1` and `bound <= MAX_BOUND` (`2**24`).  The 32-bit kernel
//!   multiplies `range` (up to `2**32 - 1`) by a cumulative frequency
//!   (up to `bound`); keeping `bound <= 2**24` bounds that product at
//!   `(2**32 - 1) * 2**24 < 2**56`, safely inside `u64`.
//! * `1 <= total <= bound`.
//! * Every symbol with positive frequency has `freq >= 1`.  Symbols with
//!   `freq == 0` may appear in the table but **cannot be encoded**
//!   ([`EncodeError::ZeroFrequency`](crate::error::EncodeError::ZeroFrequency)).
//!
//! Prefix sums use `u64` during construction, then narrow to `u32` once the
//! bound check has passed.

use crate::error::TableError;

/// Largest legal frequency total for the fixed-precision kernel.
pub const MAX_BOUND: u32 = 1 << 24;

/// A validated static frequency table plus cumulative prefix sums.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FreqTable {
    /// Per-symbol raw frequencies; zeros mark symbols that cannot be coded.
    freqs: Vec<u32>,
    /// `cum[i] = sum(freqs[0..i])`; length `len + 1`, `cum[0] == 0`.
    cum: Vec<u32>,
    total: u32,
    bound: u32,
}

impl FreqTable {
    /// Construct from raw frequencies.
    ///
    /// `bound` caps the total; pass the same value to both encoder and
    /// decoder.  [`TableError`] names the exact rule violated.
    pub fn new(frequencies: &[u32], bound: u32) -> Result<Self, TableError> {
        if frequencies.is_empty() {
            return Err(TableError::EmptyAlphabet);
        }
        if bound == 0 || bound > MAX_BOUND {
            return Err(TableError::InvalidBound { bound });
        }

        let mut total: u64 = 0;
        for (i, &f) in frequencies.iter().enumerate() {
            if f > bound {
                return Err(TableError::EntryExceeded {
                    index: i,
                    freq: f,
                    bound,
                });
            }
            total += f as u64;
            if total > bound as u64 {
                return Err(TableError::TotalExceeded {
                    total: total as u32,
                    bound,
                    point: i,
                });
            }
        }
        if total == 0 {
            return Err(TableError::AllZeroFrequencies);
        }

        let mut cum = Vec::with_capacity(frequencies.len() + 1);
        cum.push(0u32);
        let mut acc: u32 = 0;
        for &f in frequencies {
            acc += f;
            cum.push(acc);
        }

        Ok(Self {
            freqs: frequencies.to_vec(),
            cum,
            total: total as u32,
            bound,
        })
    }

    /// Uniform table: every symbol gets frequency 1, so `total == alphabet`.
    pub fn uniform(alphabet: u32, bound: u32) -> Result<Self, TableError> {
        if alphabet == 0 {
            return Err(TableError::EmptyAlphabet);
        }
        if alphabet > bound || bound > MAX_BOUND {
            return Err(TableError::InvalidBound { bound });
        }
        Self::new(&vec![1u32; alphabet as usize], bound)
    }

    /// Re-validate against a length that came from untrusted input.
    pub fn with_declared_length(
        frequencies: &[u32],
        bound: u32,
        declared: usize,
    ) -> Result<Self, TableError> {
        if frequencies.len() != declared {
            return Err(TableError::LengthMismatch {
                declared,
                given: frequencies.len(),
            });
        }
        Self::new(frequencies, bound)
    }

    /// Number of symbols in the alphabet.
    pub fn len(&self) -> usize {
        self.freqs.len()
    }

    /// Never empty — construction guarantees it.
    #[allow(clippy::len_without_is_empty)]
    pub fn is_empty(&self) -> bool {
        false
    }

    pub fn bound(&self) -> u32 {
        self.bound
    }

    pub fn total(&self) -> u32 {
        self.total
    }

    pub fn frequencies(&self) -> &[u32] {
        &self.freqs
    }

    pub fn cumulative(&self) -> &[u32] {
        &self.cum
    }

    /// Frequency of one symbol (0 if outside the table).
    pub fn freq(&self, symbol: usize) -> u32 {
        self.freqs.get(symbol).copied().unwrap_or(0)
    }

    /// Half-open cumulative interval `[cum[s], cum[s+1])`.
    /// Returns `None` for out-of-range symbols.
    pub fn interval(&self, symbol: usize) -> Option<(u32, u32)> {
        if symbol >= self.freqs.len() {
            return None;
        }
        Some((self.cum[symbol], self.cum[symbol + 1]))
    }

    /// Inverse cumulative lookup: given `0 <= value < total`, return the
    /// unique symbol `s` with `cum[s] <= value < cum[s+1]`.
    ///
    /// Binary search over the strictly-positive-width steps (`partition_point`
    /// gives the first cumulative strictly greater than `value`).
    pub fn symbol_for_cum(&self, value: u32) -> usize {
        debug_assert!(value < self.total);
        // Find first index i with cum[i] > value; that i is symbol+1.
        let i = self.cum.partition_point(|&c| c <= value);
        // Zero-frequency entries produce repeated cum values; the decode path
        // additionally verifies freqs[s] > 0 before accepting.
        i.saturating_sub(1)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uniform_table_math() {
        let t = FreqTable::uniform(4, 1 << 16).unwrap();
        assert_eq!(t.total(), 4);
        assert_eq!(t.cumulative(), &[0, 1, 2, 3, 4]);
        assert_eq!(t.symbol_for_cum(0), 0);
        assert_eq!(t.symbol_for_cum(3), 3);
        assert_eq!(t.interval(2), Some((2, 3)));
    }

    #[test]
    fn sparse_table_skips_zero_widths() {
        // symbols: 0 absent, 1 wide, 2 absent, 3 wide
        let t = FreqTable::new(&[0, 3, 0, 2], 8).unwrap();
        assert_eq!(t.total(), 5);
        assert_eq!(t.cumulative(), &[0, 0, 3, 3, 5]);
        // value 0..2 all map to symbol 1 (zero-freq symbol 0 is skipped)
        assert_eq!(t.symbol_for_cum(0), 1);
        assert_eq!(t.symbol_for_cum(2), 1);
        assert_eq!(t.symbol_for_cum(3), 3);
        assert_eq!(t.symbol_for_cum(4), 3);
    }

    #[test]
    fn rejects_empty_alphabet() {
        assert_eq!(FreqTable::new(&[], 10), Err(TableError::EmptyAlphabet));
    }

    #[test]
    fn rejects_all_zero() {
        assert_eq!(
            FreqTable::new(&[0, 0], 10),
            Err(TableError::AllZeroFrequencies)
        );
    }

    #[test]
    fn rejects_bad_bound() {
        assert_eq!(
            FreqTable::new(&[1], 0),
            Err(TableError::InvalidBound { bound: 0 })
        );
        assert_eq!(
            FreqTable::uniform(2, MAX_BOUND + 1),
            Err(TableError::InvalidBound {
                bound: MAX_BOUND + 1
            })
        );
    }

    #[test]
    fn rejects_total_overflow_with_exact_point() {
        let err = FreqTable::new(&[4, 4, 4], 10).unwrap_err();
        assert_eq!(
            err,
            TableError::TotalExceeded {
                total: 12,
                bound: 10,
                point: 2
            }
        );
    }

    #[test]
    fn rejects_single_entry_overflow() {
        let err = FreqTable::new(&[1, 11], 10).unwrap_err();
        assert_eq!(
            err,
            TableError::EntryExceeded {
                index: 1,
                freq: 11,
                bound: 10
            }
        );
    }

    #[test]
    fn length_mismatch_detected() {
        assert_eq!(
            FreqTable::with_declared_length(&[1, 2], 8, 3),
            Err(TableError::LengthMismatch {
                declared: 3,
                given: 2
            })
        );
    }

    #[test]
    fn boundary_total_equal_bound_accepted() {
        let t = FreqTable::new(&[5, 5], 10).unwrap();
        assert_eq!(t.total(), 10);
    }
}
