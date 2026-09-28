//! Frequency models.
//!
//! Two model kinds satisfy the contract:
//!
//! * [`StaticModel`] — immutable frequency table, validated up front. Zero
//!   frequency symbols are rejected at encode time (`ZeroFrequencySymbol`)
//!   and can never be produced by a valid decoder.
//! * [`AdaptiveModel`] — every symbol starts at frequency 1; after each
//!   observed symbol the count is incremented, and when the total would
//!   exceed [`MAX_FREQ_TOTAL`] the table is rescaled by integer rules
//!   (`f <- max(1, f/2)`). Both encoder and decoder update at exactly the
//!   same point — immediately after the symbol is consumed — so tables stay
//!   in lockstep without side channels.
//!
//! Bounds (part of the wire contract, see `FORMAT.md`):
//!
//! * alphabet size: `1..=MAX_SYMBOLS` (256; one model per byte alphabet)
//! * frequency total: `1..=MAX_FREQ_TOTAL` (16384 = 2^14)
//!
//! The small total combined with the 32-bit coding range (see [`crate::range`])
//! guarantees `range / total >= 2^18`, so no underflow renormalisation is
//! needed inside a symbol step.

use crate::error::{CodecError, Result};

/// Maximum number of symbols in one frequency table.
pub const MAX_SYMBOLS: u32 = 256;
/// Maximum allowed sum of frequencies (`2^14`).
pub const MAX_FREQ_TOTAL: u32 = 1 << 14;

/// A validated frequency view: cumulative counts `cum[s] = sum_{i<s} f[i]`.
/// Invariants held by every constructor:
///
/// * `1 <= num_symbols <= MAX_SYMBOLS`
/// * every `freq[s] >= 0`; `cum[num_symbols] = total` with
///   `1 <= total <= MAX_FREQ_TOTAL`
/// * at least one frequency is positive
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FrequencyTable {
    freq: Vec<u32>,
    cum: Vec<u32>,
    total: u32,
}

impl FrequencyTable {
    /// Build from raw per-symbol frequencies. Validates every bound.
    pub fn new(freqs: &[u32]) -> Result<Self> {
        let n = freqs.len();
        if n == 0 || n as u64 > MAX_SYMBOLS as u64 {
            return Err(CodecError::BadAlphabetSize {
                size: n,
                max: MAX_SYMBOLS,
            });
        }
        let mut cum = Vec::with_capacity(n + 1);
        let mut acc: u64 = 0;
        cum.push(0u32);
        for &f in freqs {
            acc += f as u64;
            if acc > MAX_FREQ_TOTAL as u64 {
                return Err(CodecError::FrequencyTotalOutOfBounds {
                    total: acc,
                    max: MAX_FREQ_TOTAL,
                });
            }
            cum.push(acc as u32);
        }
        if acc == 0 {
            return Err(CodecError::FrequencyTotalOutOfBounds {
                total: 0,
                max: MAX_FREQ_TOTAL,
            });
        }
        Ok(FrequencyTable {
            freq: freqs.to_vec(),
            cum,
            total: acc as u32,
        })
    }

    pub fn num_symbols(&self) -> usize {
        self.freq.len()
    }
    pub fn total(&self) -> u32 {
        self.total
    }
    pub fn freq(&self, symbol: usize) -> u32 {
        self.freq[symbol]
    }
    pub fn cum(&self, symbol: usize) -> u32 {
        self.cum[symbol]
    }
    pub fn freqs(&self) -> &[u32] {
        &self.freq
    }

    /// Encode interval `(cum[s], cum[s] + freq[s])` for `symbol`.
    /// Returns an error if the symbol has zero frequency (cannot be coded)
    /// or is outside the alphabet.
    pub fn interval_for(&self, symbol: u32) -> Result<(u32, u32)> {
        let s = symbol as usize;
        if s >= self.freq.len() {
            return Err(CodecError::SymbolOutOfRange {
                symbol,
                num_symbols: self.freq.len() as u32,
            });
        }
        let f = self.freq[s];
        if f == 0 {
            return Err(CodecError::ZeroFrequencySymbol { symbol });
        }
        Ok((self.cum[s], f))
    }

    /// Inverse lookup: given a scaled code value `v in 0..total`, return the
    /// unique symbol `s` with `cum[s] <= v < cum[s+1]`.
    ///
    /// Binary search over the strictly-increasing prefix positions. Zero
    /// frequency symbols collapse (`cum[s] == cum[s+1]`) and can never be
    /// returned: partition_point lands in the first position with strictly
    /// greater cum, i.e. the positive-frequency symbol covering `v`.
    pub fn symbol_for(&self, v: u32) -> Result<u32> {
        if v >= self.total {
            return Err(CodecError::CodeOutsideRange {
                code: v,
                total: self.total,
            });
        }
        // first index i with cum[i] > v, minus one = containing symbol.
        let i = self.cum.partition_point(|&c| c <= v);
        debug_assert!(i >= 1 && i <= self.freq.len());
        let s = (i - 1) as u32;
        // Defensive: a zero-width slot means corruption.
        if self.freq[i - 1] == 0 {
            return Err(CodecError::CodeOutsideRange {
                code: v,
                total: self.total,
            });
        }
        Ok(s)
    }
}

/// Anything that can hand the kernel the interval for the next symbol.
pub trait Model {
    fn table(&self) -> &FrequencyTable;
    /// Observe that `symbol` was just coded. Static models ignore it;
    /// adaptive models update counters and rescale on integer schedule.
    fn update(&mut self, symbol: u32) -> Result<()>;
}

/// Immutable model. Construction rejects zero-total / over-budget tables;
/// encoding rejects zero-frequency symbols per [`FrequencyTable::interval_for`].
#[derive(Debug, Clone)]
pub struct StaticModel {
    table: FrequencyTable,
}

impl StaticModel {
    pub fn new(freqs: &[u32]) -> Result<Self> {
        Ok(Self {
            table: FrequencyTable::new(freqs)?,
        })
    }
    pub fn from_table(table: FrequencyTable) -> Self {
        Self { table }
    }
}

impl Model for StaticModel {
    fn table(&self) -> &FrequencyTable {
        &self.table
    }
    fn update(&mut self, _symbol: u32) -> Result<()> {
        Ok(())
    }
}

/// Adaptive model with integer halving rescale.
///
/// Rescale rule (deterministic, identical for encoder and decoder):
///
/// 1. increment `freq[s]`; if `total < MAX_FREQ_TOTAL` after the increment,
///    stop;
/// 2. otherwise replace every `f` by `max(1, f/2)` using *integer* division,
///    then rebuild cumulative counts. The new total is strictly smaller than
///    `MAX_FREQ_TOTAL` (each non-zero count loses at least one), while symbols
///    ever seen remain codeable (`max(1, ...)`).
#[derive(Debug, Clone)]
pub struct AdaptiveModel {
    table: FrequencyTable,
}

impl AdaptiveModel {
    /// Start with frequency `init` (usually 1) for every symbol.
    pub fn uniform(num_symbols: u32, init: u32) -> Result<Self> {
        if num_symbols == 0 || num_symbols > MAX_SYMBOLS {
            return Err(CodecError::BadAlphabetSize {
                size: num_symbols as usize,
                max: MAX_SYMBOLS,
            });
        }
        if init == 0 {
            return Err(CodecError::FrequencyTotalOutOfBounds {
                total: 0,
                max: MAX_FREQ_TOTAL,
            });
        }
        let total = num_symbols as u64 * init as u64;
        if total > MAX_FREQ_TOTAL as u64 {
            return Err(CodecError::FrequencyTotalOutOfBounds {
                total,
                max: MAX_FREQ_TOTAL,
            });
        }
        let freqs = vec![init; num_symbols as usize];
        Ok(Self {
            table: FrequencyTable::new(&freqs)?,
        })
    }

    fn rebuild(&mut self) {
        let n = self.table.freq.len();
        let mut cum = Vec::with_capacity(n + 1);
        cum.push(0u32);
        let mut acc: u64 = 0;
        for &f in &self.table.freq {
            acc += f as u64;
            cum.push(acc as u32);
        }
        self.table.cum = cum;
        self.table.total = acc as u32;
    }

    fn rescale(&mut self) {
        for f in self.table.freq.iter_mut() {
            // Integer rule; never let a previously codeable symbol die.
            *f = std::cmp::max(1, *f / 2);
        }
        self.rebuild();
        debug_assert!(self.table.total < MAX_FREQ_TOTAL);
    }
}

impl Model for AdaptiveModel {
    fn table(&self) -> &FrequencyTable {
        &self.table
    }

    fn update(&mut self, symbol: u32) -> Result<()> {
        let s = symbol as usize;
        if s >= self.table.freq.len() {
            return Err(CodecError::SymbolOutOfRange {
                symbol,
                num_symbols: self.table.freq.len() as u32,
            });
        }
        self.table.freq[s] += 1;
        self.table.total += 1;
        if self.table.total > MAX_FREQ_TOTAL {
            // Roll the increment into the rescale: rescale the already
            // incremented counts so the symbol just observed is counted.
            self.rescale();
        } else {
            self.rebuild();
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_zero_total() {
        let err = FrequencyTable::new(&[0, 0, 0]).unwrap_err();
        assert_eq!(
            err,
            CodecError::FrequencyTotalOutOfBounds {
                total: 0,
                max: MAX_FREQ_TOTAL
            }
        );
        assert_eq!(err.decision(), crate::error::Decision::Rejected);
    }

    #[test]
    fn rejects_oversized_total_with_exact_running_total() {
        // total crosses the bound at the third symbol.
        let freqs = [9000u32, 7000, 400];
        let err = FrequencyTable::new(&freqs).unwrap_err();
        match err {
            CodecError::FrequencyTotalOutOfBounds { total, max } => {
                assert_eq!(total, 16400);
                assert_eq!(max, MAX_FREQ_TOTAL);
            }
            other => panic!("wrong error: {other:?}"),
        }
    }

    #[test]
    fn rejects_empty_and_too_large_alphabet() {
        assert!(matches!(
            FrequencyTable::new(&[]),
            Err(CodecError::BadAlphabetSize { size: 0, .. })
        ));
        let big = vec![1u32; MAX_SYMBOLS as usize + 1];
        assert!(matches!(
            FrequencyTable::new(&big),
            Err(CodecError::BadAlphabetSize { .. })
        ));
    }

    #[test]
    fn zero_frequency_symbol_cannot_be_encoded() {
        let t = FrequencyTable::new(&[1, 0, 2]).unwrap();
        assert_eq!(
            t.interval_for(1),
            Err(CodecError::ZeroFrequencySymbol { symbol: 1 })
        );
        assert_eq!(t.interval_for(2).unwrap(), (1, 2));
    }

    #[test]
    fn inverse_lookup_skips_zero_slots() {
        // freqs: a=2, b=0, c=1 ; cum: 0,2,2,3
        let t = FrequencyTable::new(&[2, 0, 1]).unwrap();
        assert_eq!(t.symbol_for(0).unwrap(), 0);
        assert_eq!(t.symbol_for(1).unwrap(), 0);
        assert_eq!(t.symbol_for(2).unwrap(), 2); // lands in c, never b
        assert!(matches!(
            t.symbol_for(3),
            Err(CodecError::CodeOutsideRange { .. })
        ));
    }

    #[test]
    fn adaptive_rescale_uses_integer_halving() {
        let mut m = AdaptiveModel::uniform(2, 1).unwrap();
        // Drive symbol 0 until total exceeds the bound.
        let mut rescales = 0;
        for _ in 0..(MAX_FREQ_TOTAL as usize + 10) {
            let before = m.table().total();
            m.update(0).unwrap();
            if m.table().total() < before {
                rescales += 1;
            }
            assert!(m.table().total() <= MAX_FREQ_TOTAL);
        }
        assert!(rescales >= 1, "expected at least one rescale");
        // Symbol 1 must still be codeable: max(1, f/2) keeps it alive.
        assert!(m.table().freq(1) >= 1);
        // Every frequency is an integer; after rescale f0 == f0_orig/2-ish.
        let t = m.table();
        assert_eq!(t.freq(0) + t.freq(1), t.total());
    }

    #[test]
    fn adaptive_update_point_is_deterministic() {
        // Two independent models updated with the same symbol sequence must
        // produce byte-identical tables (encoder/decoder lockstep).
        let mut a = AdaptiveModel::uniform(4, 1).unwrap();
        let mut b = AdaptiveModel::uniform(4, 1).unwrap();
        for s in [0u32, 0, 3, 0, 2, 3, 3, 0, 1, 2] {
            a.update(s).unwrap();
            b.update(s).unwrap();
        }
        assert_eq!(a.table(), b.table());
    }
}
