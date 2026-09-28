//! Succinct bit vector with O(1) `rank` — the building block of the
//! wavelet matrix. Only `rank` (not `select`) is needed by the index kernel.

/// Bit vector with a per-word prefix table of 1-bit counts.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BitVector {
    len: usize,
    words: Vec<u64>,
    /// `rank1_prefix[i]` = number of set bits in `words[0..i]`.
    rank1_prefix: Vec<u32>,
}

impl BitVector {
    pub fn from_bits(bits: &[bool]) -> Self {
        let len = bits.len();
        let mut words = vec![0u64; len.div_ceil(64)];
        for (i, &b) in bits.iter().enumerate() {
            if b {
                words[i / 64] |= 1 << (i % 64);
            }
        }
        Self::from_words(words, len)
    }

    /// Build from packed words, recomputing the rank table.
    ///
    /// The persistence layer stores only `words` + `len`; the rank table is
    /// derived state and is rebuilt here on load.
    pub fn from_words(words: Vec<u64>, len: usize) -> Self {
        assert!(len <= words.len() * 64, "words too short for len");
        let mut rank1_prefix = Vec::with_capacity(words.len() + 1);
        let mut acc = 0u32;
        rank1_prefix.push(0);
        for w in &words {
            acc += w.count_ones();
            rank1_prefix.push(acc);
        }
        BitVector {
            len,
            words,
            rank1_prefix,
        }
    }

    pub fn len(&self) -> usize {
        self.len
    }

    pub fn is_empty(&self) -> bool {
        self.len == 0
    }

    pub fn get(&self, i: usize) -> bool {
        debug_assert!(i < self.len);
        (self.words[i / 64] >> (i % 64)) & 1 == 1
    }

    /// Number of 1-bits in `[0, pos)`.
    pub fn rank1(&self, pos: usize) -> usize {
        debug_assert!(pos <= self.len);
        let w = pos / 64;
        let off = pos % 64;
        let mut n = self.rank1_prefix[w] as usize;
        if off > 0 {
            n += (self.words[w] & ((1u64 << off) - 1)).count_ones() as usize;
        }
        n
    }

    /// Number of 0-bits in `[0, pos)`.
    pub fn rank0(&self, pos: usize) -> usize {
        pos - self.rank1(pos)
    }

    /// Packed words, most-significant-bit-last within each word (bit `i` of
    /// the vector is bit `i % 64` of `words[i / 64]`). Used by `format`.
    pub fn words(&self) -> &[u64] {
        &self.words
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rank_matches_naive_count() {
        let bits: Vec<bool> = (0..200).map(|i| i % 3 == 0 || i % 7 == 1).collect();
        let bv = BitVector::from_bits(&bits);
        for pos in 0..=bits.len() {
            let ones = bits[..pos].iter().filter(|&&b| b).count();
            assert_eq!(bv.rank1(pos), ones, "rank1({pos})");
            assert_eq!(bv.rank0(pos), pos - ones, "rank0({pos})");
        }
    }

    #[test]
    fn word_roundtrip() {
        let bits: Vec<bool> = (0..130).map(|i| i % 2 == 0).collect();
        let bv = BitVector::from_bits(&bits);
        let rebuilt = BitVector::from_words(bv.words().to_vec(), bv.len());
        assert_eq!(bv, rebuilt);
    }
}
