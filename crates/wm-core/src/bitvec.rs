//! Immutable bit-vector with block prefix ranks.
//!
//! Supports O(1) `rank1`/`rank0` queries: ranks are the only primitive the
//! wavelet matrix needs while navigating levels.

/// Compact bit-vector backed by 64-bit words and a prefix-popcount table.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BitVector {
    words: Vec<u64>,
    /// `prefix_ones[i]` = number of `1` bits in words `0..i`.
    /// Length is `words.len() + 1`.
    prefix_ones: Vec<u64>,
    len: usize,
}

impl BitVector {
    /// Build from a slice of booleans (`true` == bit 1).
    pub fn from_bools(bits: &[bool]) -> Self {
        let len = bits.len();
        let mut words = vec![0u64; len.div_ceil(64)];
        for (i, b) in bits.iter().enumerate() {
            if *b {
                words[i / 64] |= 1u64 << (i % 64);
            }
        }
        // Safe: words were just derived from `len`.
        Self::from_words(len, words)
    }

    /// Rebuild from raw words, recomputing prefix ranks.
    ///
    /// # Errors
    /// The last word must not carry set bits at positions `>= len`, and the
    /// number of words must match `len`. Otherwise the on-disk image is
    /// inconsistent and decoding rejects it.
    pub fn try_from_words(len: usize, words: Vec<u64>) -> Result<Self, InvalidBitVector> {
        let expected_words = len.div_ceil(64);
        if words.len() != expected_words {
            return Err(InvalidBitVector::WordCount {
                len,
                words: words.len(),
                expected: expected_words,
            });
        }
        if let Some(&last) = words.last() {
            let tail = (len % 64) as u32;
            if tail != 0 && last >> tail != 0 {
                return Err(InvalidBitVector::TrailingBits { len });
            }
        }
        Ok(Self::from_words(len, words))
    }

    fn from_words(len: usize, words: Vec<u64>) -> Self {
        let mut prefix_ones = Vec::with_capacity(words.len() + 1);
        prefix_ones.push(0u64);
        for w in &words {
            let acc = *prefix_ones.last().unwrap() + w.count_ones() as u64;
            prefix_ones.push(acc);
        }
        Self {
            words,
            prefix_ones,
            len,
        }
    }

    /// Number of stored bits.
    #[must_use]
    pub fn len(&self) -> usize {
        self.len
    }

    /// Whether the vector holds zero bits.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.len == 0
    }

    /// Bit at position `i`.
    ///
    /// # Panics
    /// Panics when `i >= len`.
    #[must_use]
    pub fn get(&self, i: usize) -> bool {
        assert!(
            i < self.len,
            "bit index {i} out of bounds (len {})",
            self.len
        );
        (self.words[i / 64] >> (i % 64)) & 1 == 1
    }

    /// Number of `1` bits in the half-open prefix `[0, pos)`.
    #[must_use]
    pub fn rank1(&self, pos: usize) -> usize {
        assert!(pos <= self.len, "rank position {pos} > len {}", self.len);
        let (w, off) = (pos / 64, (pos % 64) as u32);
        let mut c = self.prefix_ones[w] as usize;
        if off != 0 {
            // `off < 64`, shift is always well defined.
            c += (self.words[w] & ((1u64 << off) - 1)).count_ones() as usize;
        }
        c
    }

    /// Number of `0` bits in the half-open prefix `[0, pos)`.
    #[must_use]
    pub fn rank0(&self, pos: usize) -> usize {
        pos - self.rank1(pos)
    }

    /// Raw storage words (used by the format crate for serialization).
    #[must_use]
    pub fn words(&self) -> &[u64] {
        &self.words
    }
}

/// Failure describing a structurally invalid bit-vector image.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InvalidBitVector {
    /// Word count does not match the declared bit length.
    WordCount {
        len: usize,
        words: usize,
        expected: usize,
    },
    /// The last word contains set bits past the declared length.
    TrailingBits { len: usize },
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ranks_over_word_boundaries() {
        // Explicit 130-bit pattern with known prefix counts.
        let bits: Vec<bool> = (0..130).map(|i| i % 3 == 0 || i == 65).collect();
        let bv = BitVector::from_bools(&bits);
        assert_eq!(bv.len(), 130);
        for pos in [0usize, 1, 63, 64, 65, 66, 128, 130] {
            let ones = bits[..pos].iter().filter(|b| **b).count();
            assert_eq!(bv.rank1(pos), ones, "rank1({pos})");
            assert_eq!(bv.rank0(pos), pos - ones, "rank0({pos})");
        }
        assert!(bv.get(65));
        assert!(bv.get(66)); // 66 % 3 == 0 -> set
        assert!(!bv.get(67));
    }

    #[test]
    fn empty_and_single_word_tail() {
        let bv = BitVector::from_bools(&[]);
        assert_eq!(bv.len(), 0);
        assert_eq!(bv.rank1(0), 0);

        let err = BitVector::try_from_words(3, vec![0b1011_0000]).unwrap_err();
        assert!(matches!(err, InvalidBitVector::TrailingBits { len: 3 }));
        let ok = BitVector::try_from_words(3, vec![0b0011]).unwrap();
        assert_eq!(ok.rank1(3), 2);

        assert!(matches!(
            BitVector::try_from_words(64, vec![0, 0]).unwrap_err(),
            InvalidBitVector::WordCount { expected: 1, .. }
        ));
    }
}
