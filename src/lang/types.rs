//! Fixed-width integer types and their wrapping semantics.

use serde::{Deserialize, Serialize};

/// Supported integer widths (unsigned, two's-complement storage).
///
/// Only the widths below are allowed so that concrete values always fit into
/// a native `u64` and exhaustible small domains stay practical.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Type {
    U8,
    U16,
    U32,
    U64,
}

impl Type {
    pub fn bits(self) -> u32 {
        match self {
            Type::U8 => 8,
            Type::U16 => 16,
            Type::U32 => 32,
            Type::U64 => 64,
        }
    }

    /// Bit mask keeping only the low `bits` of a concrete value.
    pub fn mask(self) -> u64 {
        match self {
            Type::U64 => u64::MAX,
            t => (1u64 << t.bits()) - 1,
        }
    }

    /// Number of representable values; [`u128`] because 2^64 does not fit u64.
    pub fn domain_size(self) -> u128 {
        1u128 << self.bits()
    }

    pub fn name(self) -> &'static str {
        match self {
            Type::U8 => "u8",
            Type::U16 => "u16",
            Type::U32 => "u32",
            Type::U64 => "u64",
        }
    }

    pub fn from_bits(bits: u32) -> Option<Type> {
        Some(match bits {
            8 => Type::U8,
            16 => Type::U16,
            32 => Type::U32,
            64 => Type::U64,
            _ => return None,
        })
    }
}

/// Mask for an arbitrary width (1..=64). Width 64 yields `u64::MAX`.
pub fn mask_of(bits: u32) -> u64 {
    match bits {
        64 => u64::MAX,
        b => (1u64 << b) - 1,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn masks_are_width_exact() {
        assert_eq!(Type::U8.mask(), 0xff);
        assert_eq!(Type::U16.mask(), 0xffff);
        assert_eq!(Type::U32.mask(), 0xffff_ffff);
        assert_eq!(Type::U64.mask(), u64::MAX);
        assert_eq!(mask_of(1), 1);
    }

    #[test]
    fn domain_sizes() {
        assert_eq!(Type::U8.domain_size(), 256);
        assert_eq!(Type::U16.domain_size(), 65536);
        assert_eq!(Type::U64.domain_size(), 1u128 << 64);
    }
}
