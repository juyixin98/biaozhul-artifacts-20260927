//! Adaptive frequency counts with bounded totals and well-defined rescaling.
//!
//! The on-disk container is *piecewise static*: every CHUNK segment codes
//! against one explicit [`FreqTable`] ("epoch").  This module drives when a
//! new epoch begins: after observing a symbol, the count is incremented; if
//! the running total would cross the bound, every count is halved (rounding
//! up, never to zero) in one atomic step and the rescaled counts become the
//! next epoch's table.
//!
//! **Point-in-time consistency.** A rescale only takes effect at a chunk
//! boundary *before* the symbol that would overflow.  Encoder and decoder
//! therefore never disagree about which frequencies govern a symbol: the
//! container physically carries the new TABLE segment at that point.
//!
//! **Headroom requirement.** Ceiling-halving keeps every seen symbol alive
//! (`freq ≥ 1`); with an alphabet of size A the post-halving total is at most
//! `(bound + A) / 2`, plus the triggering increment.  The constructor
//! therefore requires `A <= bound / 2`, which guarantees the rescaled total
//! stays at or below the bound for every future rescale.

use crate::error::TableError;
use crate::table::FreqTable;

/// Enforce the rescale headroom invariant (see module docs).
fn ensure_rescale_headroom(alphabet: u32, bound: u32) -> Result<(), TableError> {
    if alphabet == 0 {
        return Err(TableError::EmptyAlphabet);
    }
    if bound == 0 || alphabet > bound / 2 {
        return Err(TableError::InvalidBound { bound });
    }
    Ok(())
}

/// Count-based model producing validated [`FreqTable`] snapshots.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AdaptiveModel {
    counts: Vec<u32>,
    total: u32,
    bound: u32,
}

/// What happened when a symbol was observed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ObserveOutcome {
    /// Count incremented within the current epoch.
    Incremented,
    /// Increment would have crossed the bound; counts were rescaled and a
    /// new epoch must start before the *next* symbol.
    Rescaled { new_total: u32 },
}

impl AdaptiveModel {
    /// Uniform start: every symbol has count 1 (`total == alphabet`).
    ///
    /// Requires `1 <= alphabet <= bound / 2` so ceiling-halving rescales can
    /// never push the total back over the bound.
    pub fn new(alphabet: u32, bound: u32) -> Result<Self, TableError> {
        ensure_rescale_headroom(alphabet, bound)?;
        // Validate the seed table through FreqTable as well.
        let seed = FreqTable::uniform(alphabet, bound)?;
        Ok(Self {
            counts: seed.frequencies().to_vec(),
            total: seed.total(),
            bound,
        })
    }

    /// Start from explicit initial frequencies (e.g. a trained prior).
    ///
    /// The table must satisfy the same headroom rule: its alphabet must fit
    /// in half the bound.
    pub fn from_table(table: &FreqTable) -> Result<Self, TableError> {
        ensure_rescale_headroom(table.len() as u32, table.bound())?;
        Ok(Self {
            counts: table.frequencies().to_vec(),
            total: table.total(),
            bound: table.bound(),
        })
    }

    pub fn alphabet(&self) -> usize {
        self.counts.len()
    }

    pub fn bound(&self) -> u32 {
        self.bound
    }

    pub fn total(&self) -> u32 {
        self.total
    }

    /// Materialize the current epoch as a validated static table.
    pub fn snapshot(&self) -> FreqTable {
        FreqTable::new(&self.counts, self.bound)
            .expect("model invariants guarantee a valid snapshot")
    }

    /// Account for one already-encoded/decoded symbol.
    ///
    /// Rescaling uses ceiling division so a symbol seen at least once keeps
    /// frequency >= 1 (symbols never disappear, which keeps every future
    /// occurrence encodable).  After halving, the observed symbol's
    /// increment — which triggered the overflow — is applied to the fresh
    /// epoch's counts.
    pub fn observe(&mut self, symbol: u32) -> Result<ObserveOutcome, TableError> {
        let idx = symbol as usize;
        if idx >= self.counts.len() {
            return Err(TableError::LengthMismatch {
                declared: self.counts.len(),
                given: idx + 1,
            });
        }
        if self.total < self.bound {
            self.counts[idx] += 1;
            self.total += 1;
            return Ok(ObserveOutcome::Incremented);
        }

        // Rescale first, then apply the triggering increment.
        let mut new_total: u64 = 0;
        for c in self.counts.iter_mut() {
            // Ceiling division; minimum result for c >= 1 is 1.
            *c = (*c).div_ceil(2);
            new_total += *c as u64;
        }
        // Headroom constructor invariant guarantees this cannot overflow.
        debug_assert!(new_total < self.bound as u64);
        self.counts[idx] += 1;
        new_total += 1;
        self.total = new_total as u32;
        Ok(ObserveOutcome::Rescaled {
            new_total: self.total,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn starts_uniform() {
        let m = AdaptiveModel::new(4, 16).unwrap();
        assert_eq!(m.total(), 4);
        assert_eq!(m.snapshot().frequencies(), &[1, 1, 1, 1]);
    }

    #[test]
    fn increments_within_bound() {
        let mut m = AdaptiveModel::new(2, 8).unwrap();
        assert_eq!(m.observe(0).unwrap(), ObserveOutcome::Incremented);
        assert_eq!(m.snapshot().frequencies(), &[2, 1]);
        assert_eq!(m.total(), 3);
    }

    #[test]
    fn rescale_halves_and_keeps_symbols_alive() {
        // alphabet 4, bound 8: total starts at 4.  Four more observations
        // reach total == 8; the fifth forces a rescale.
        let mut m = AdaptiveModel::new(4, 8).unwrap();
        for s in [0, 0, 0, 1] {
            assert_eq!(m.observe(s).unwrap(), ObserveOutcome::Incremented);
        }
        // Counts [4,2,1,1], total 8.
        assert_eq!(m.snapshot().frequencies(), &[4, 2, 1, 1]);
        let out = m.observe(0).unwrap();
        assert_eq!(out, ObserveOutcome::Rescaled { new_total: 6 });
        // Halved ceiling: [2,1,1,1] (sum 5), then symbol 0 incremented -> [3,1,1,1].
        assert_eq!(m.snapshot().frequencies(), &[3, 1, 1, 1]);
        assert_eq!(m.total(), 6);
        // Every symbol still encodable.
        let snap = m.snapshot();
        for s in 0..4 {
            assert!(snap.freq(s) >= 1);
        }
    }

    #[test]
    fn rescaled_snapshot_always_within_bound() {
        let mut m = AdaptiveModel::new(256, 1024).unwrap();
        // Drive far past the bound; every snapshot must validate.
        for i in 0..10_000u32 {
            m.observe(i % 256).unwrap();
            assert!(m.total() <= m.bound());
            // Construction itself re-validates.
            let _ = m.snapshot();
        }
    }

    #[test]
    fn requires_headroom_for_rescaling() {
        // alphabet == bound leaves no room after ceiling-halving.
        let err = AdaptiveModel::new(8, 8).unwrap_err();
        assert_eq!(err, TableError::InvalidBound { bound: 8 });
        // Half is accepted.
        AdaptiveModel::new(8, 16).unwrap();
    }

    #[test]
    fn unknown_symbol_rejected() {
        let mut m = AdaptiveModel::new(4, 16).unwrap();
        assert!(m.observe(4).is_err());
    }
}
