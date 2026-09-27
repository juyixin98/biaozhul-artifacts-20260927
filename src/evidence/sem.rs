//! Wrapping integer semantics shared by the concrete interpreter and the
//! native path-condition evaluator. These are plain u64 functions with no
//! dependency on either the AST types or Z3, so both independent interpreters
//! agree on exactly the same machine semantics.

use crate::lang::types::mask_of;

/// Wrap `v` into the low `bits` bits.
pub fn wrap(bits: u32, v: u128) -> u64 {
    (v as u64) & mask_of(bits)
}

pub fn add(bits: u32, a: u64, b: u64) -> u64 {
    wrap(bits, (a as u128) + (b as u128))
}
pub fn sub(bits: u32, a: u64, b: u64) -> u64 {
    wrap(bits, (a as u128).wrapping_sub(b as u128) & ((1u128 << bits) - 1))
}
pub fn mul(bits: u32, a: u64, b: u64) -> u64 {
    wrap(bits, (a as u128) * (b as u128))
}
pub fn neg(bits: u32, a: u64) -> u64 {
    let m = if bits == 64 { u128::MAX } else { (1u128 << bits) - 1 };
    wrap(bits, ((!(a as u128)) & m) + 1)
}
pub fn bitnot(bits: u32, a: u64) -> u64 {
    (!a) & mask_of(bits)
}

/// Shift semantics follow SMT-LIB `bvshl`/`bvlshr`: shifting by an amount
/// greater than or equal to the width yields 0 (every bit is shifted out).
pub fn shl(bits: u32, a: u64, amount: u64) -> u64 {
    if amount >= bits as u64 {
        0
    } else {
        wrap(bits, (a as u128) << amount)
    }
}

pub fn shr(bits: u32, a: u64, amount: u64) -> u64 {
    if amount >= bits as u64 {
        0
    } else {
        a >> amount
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wrap_arithmetic() {
        assert_eq!(add(8, 255, 1), 0);
        assert_eq!(sub(8, 0, 1), 255);
        assert_eq!(mul(8, 16, 16), 0);
        assert_eq!(mul(8, 17, 15), 255);
        assert_eq!(neg(8, 1), 255);
        assert_eq!(neg(8, 0), 0);
        assert_eq!(neg(32, 1), u32::MAX as u64);
        assert_eq!(bitnot(8, 0), 255);
        assert_eq!(bitnot(8, 0xff), 0);
    }

    #[test]
    fn shift_beyond_width_is_zero() {
        assert_eq!(shl(8, 1, 7), 128);
        assert_eq!(shl(8, 1, 8), 0);
        assert_eq!(shl(8, 1, 9), 0);
        assert_eq!(shr(8, 0x80, 7), 1);
        assert_eq!(shr(8, 0x80, 8), 0);
        assert_eq!(shl(64, 1, 63), 1u64 << 63);
        assert_eq!(shl(64, 1, 64), 0);
    }
}
