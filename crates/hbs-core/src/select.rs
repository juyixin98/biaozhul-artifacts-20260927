//! Rank/select primitives.
//!
//! `rank_le(w, i)`  = number of set bits in `w` at positions `<= i`.
//! `select64(w, k)` = index (0-based) of the `k`-th set bit in `w`.
//!
//! `select64` uses a byte-at-a-time search over precomputed popcount tables:
//! it needs no BMI2 (`PDEP`/`TZCNT`) and is fully deterministic, which keeps
//! the behaviour identical across test machines.

/// Precomputed popcount of every byte value.
pub const POPCOUNT: [u8; 256] = {
    let mut table = [0u8; 256];
    let mut i = 0usize;
    while i < 256 {
        table[i] = (i.count_ones()) as u8;
        i += 1;
    }
    table
};

/// Sum of popcounts of the low `nbytes` bytes of `w` (0..=8).
#[inline]
pub fn popcount_prefix(w: u64, nbytes: usize) -> u32 {
    let mask = if nbytes >= 8 {
        u64::MAX
    } else {
        (1u64 << (nbytes * 8)) - 1
    };
    (w & mask).count_ones()
}

/// Number of set bits at positions `0..=i` within `w`.
///
/// `i` is a bit index `0..63`; the shift is done with `checked_shl` so that
/// the boundary case `i == 63` (`i + 1 == 64`) cannot overflow.
#[inline]
pub fn rank_le_word(w: u64, i: usize) -> usize {
    debug_assert!(i < 64);
    // Bits 0..=i  <=>  (1 << (i+1)) - 1.
    let mask = match 1u64.checked_shl(i as u32 + 1) {
        Some(bit) => bit - 1,
        None => u64::MAX,
    };
    (w & mask).count_ones() as usize
}

/// Number of set bits at positions `0..i` within `w` (`i` in 0..=64).
#[inline]
pub fn rank_lt_word(w: u64, i: usize) -> usize {
    debug_assert!(i <= 64);
    let mask = if i == 64 { u64::MAX } else { (1u64 << i) - 1 };
    (w & mask).count_ones() as usize
}

/// Position of the `rank`-th set bit (rank is 0-based) in `w`.
///
/// Returns `None` when `w` has at most `rank` set bits.
#[inline]
pub fn select64(w: u64, rank: usize) -> Option<usize> {
    if (w.count_ones() as usize) <= rank {
        return None;
    }
    let mut remaining = rank;
    // Walk the 8 bytes, skipping whole bytes whose popcount is too small.
    let mut byte_index = 0usize;
    while byte_index < 8 {
        let b = (w >> (byte_index * 8)) as u8;
        let pc = POPCOUNT[b as usize] as usize;
        if remaining < pc {
            // The target bit lies inside this byte.
            let mut target = b;
            // Clear the lowest `remaining` set bits, then read the position
            // of the next (target) set bit directly.
            for _ in 0..remaining {
                target &= target - 1;
            }
            let bit_in_byte = target.trailing_zeros() as usize;
            return Some(byte_index * 8 + bit_in_byte);
        }
        remaining -= pc;
        byte_index += 1;
    }
    None // Unreachable: the popcount check above guarantees success.
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rank_word_boundaries() {
        let w = u64::MAX;
        assert_eq!(rank_le_word(w, 0), 1);
        assert_eq!(rank_le_word(w, 63), 64);
        assert_eq!(rank_lt_word(w, 0), 0);
        assert_eq!(rank_lt_word(w, 64), 64);
        assert_eq!(rank_le_word(1, 0), 1);
        assert_eq!(rank_le_word(1, 1), 1);
        assert_eq!(rank_le_word(2, 0), 0);
        assert_eq!(rank_le_word(2, 1), 1);
    }

    #[test]
    fn select_word_exhaustive_small() {
        // For every 12-bit pattern, compare against a simple reference.
        for pat in 0u32..(1 << 12) {
            let w = pat as u64;
            let ones: Vec<usize> = (0..12).filter(|&i| (w >> i) & 1 == 1).collect();
            for (rank, &pos) in ones.iter().enumerate() {
                assert_eq!(select64(w, rank), Some(pos));
            }
            assert_eq!(select64(w, ones.len()), None);
        }
    }
}
