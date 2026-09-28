//! Coding configuration `(k, m)`: k data shards, m parity shards.
//!
//! Constraints, fixed by the field:
//! - `k >= 1`, `m >= 1`;
//! - `k + m <= 255`, because the Cauchy matrix construction in [`crate::matrix`]
//!   consumes `k + m` *distinct* GF(2^8) evaluation points and the field only
//!   has 256 elements.
//!
//! Up to `m` shards may be erased (missing or digest-bad) and the original
//! data recovered byte-for-byte.

use std::fmt;

use crate::error::{EcError, EcResult};
use crate::matrix::coding_row;

/// Validated erasure-coding parameters.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct CodecConfig {
    /// Number of data shards.
    k: u8,
    /// Number of parity shards.
    m: u8,
}

impl CodecConfig {
    /// Validate and construct. The only constructor, so a `CodecConfig`
    /// value is always usable.
    pub fn new(k: u16, m: u16) -> EcResult<Self> {
        if k < 1 || m < 1 {
            return Err(EcError::InvalidConfig(format!(
                "k={k} and m={m} must both be >= 1"
            )));
        }
        if k + m > 255 {
            return Err(EcError::InvalidConfig(format!(
                "k + m = {} exceeds the GF(2^8) limit of 255 distinct points",
                k + m
            )));
        }
        Ok(CodecConfig {
            k: k as u8,
            m: m as u8,
        })
    }

    /// Data shard count.
    pub fn k(&self) -> usize {
        self.k as usize
    }

    /// Parity shard count.
    pub fn m(&self) -> usize {
        self.m as usize
    }

    /// Total shard count.
    pub fn n(&self) -> usize {
        self.k() + self.m()
    }

    /// Maximum number of erasures tolerated.
    pub fn fault_tolerance(&self) -> usize {
        self.m()
    }

    /// Validate that `index` names one of the `k+m` shards.
    pub fn check_index(&self, index: u16) -> EcResult<()> {
        if (index as usize) < self.n() {
            Ok(())
        } else {
            Err(EcError::InvalidShardIndex {
                index,
                total: self.n() as u16,
            })
        }
    }

    /// The fixed coding-matrix row for shard `index`: identity (data) or
    /// Cauchy (parity). Returned as `k` bytes.
    pub fn coding_row(&self, index: usize) -> Vec<u8> {
        self.check_index(index as u16)
            .expect("coding_row: shard index out of range");
        coding_row(self.k(), index)
    }
}

impl fmt::Display for CodecConfig {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "RS(k={},m={})", self.k, self.m)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validates_bounds() {
        assert!(CodecConfig::new(0, 2).is_err());
        assert!(CodecConfig::new(3, 0).is_err());
        assert!(CodecConfig::new(200, 56).is_err()); // 256 > 255
        assert!(CodecConfig::new(200, 55).is_ok()); // 255 exactly
        let c = CodecConfig::new(4, 2).unwrap();
        assert_eq!(c.n(), 6);
        assert_eq!(c.fault_tolerance(), 2);
        assert!(c.check_index(5).is_ok());
        assert!(c.check_index(6).is_err());
    }

    #[test]
    fn data_rows_are_identity_and_parity_rows_are_cauchy() {
        let c = CodecConfig::new(3, 2).unwrap();
        assert_eq!(c.coding_row(0), vec![1, 0, 0]);
        assert_eq!(c.coding_row(2), vec![0, 0, 1]);
        // Parity row p=0: 1/(x0 - y_j) with x0 = AUGMENTED_POINTS[0],
        // y_j = point j. Just assert determinism and non-zeroness here;
        // numerical correctness of Cauchy entries is checked in matrix tests.
        let row = c.coding_row(3);
        assert_eq!(row.len(), 3);
        assert!(row.iter().all(|&v| v != 0));
        assert_eq!(c.coding_row(3), row);
    }
}
