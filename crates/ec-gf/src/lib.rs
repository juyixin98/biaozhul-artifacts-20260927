//! GF(2^8) finite field primitives used by every Reed-Solomon operation.
//!
//! # Field definition (fixed, auditable)
//!
//! - Elements are 8-bit values interpreted as polynomials over GF(2), bit `i`
//!   being the coefficient of x^i.
//! - Modulus: `x^8 + x^4 + x^3 + x + 1`, i.e. the 9-bit pattern `0x11B`
//!   (the AES/Rijndael polynomial; it is irreducible over GF(2)).
//! - Addition/subtraction are XOR (characteristic 2).
//! - Multiplication is polynomial multiplication followed by reduction modulo
//!   `0x11D`.
//! - The generator `g = 3` (the element `x + 1`) is primitive in this field,
//!   so every non-zero element equals `g^e` for a unique `e` in `0..255`.
//!   (Note: in the AES polynomial field the element `2 = x` is *not* a
//!   generator; `3` is the conventional primitive element.) Exp/log tables
//!   make multiplication O(1); [`mul_slow`] is the table-free textbook
//!   implementation kept for cross-checking and review.
//!
//! Nothing here is random or platform dependent: the tables are built at first
//! use and contain exactly the same 256 bytes on every machine.

#![forbid(unsafe_code)]

use std::sync::OnceLock;

/// Modulus polynomial, 9 bits: x^8 + x^4 + x^3 + x + 1 (AES polynomial).
pub const MODULUS: u16 = 0x11B;
/// Primitive generator used for the exp/log tables (in the AES field the
/// element 3 = x + 1 generates the whole multiplicative group).
pub const GENERATOR: u8 = 3;

struct Tables {
    /// exp[i] = g^i mod P, for i in 0..255 (exp[255] = exp[0] = 1).
    exp: [u8; 256],
    /// log[a] = unique e in 0..255 with g^e = a; log[0] is undefined and held
    /// as 0 — callers must never divide by or take logs of zero.
    log: [u8; 256],
}

static TABLES: OnceLock<Tables> = OnceLock::new();

fn tables() -> &'static Tables {
    TABLES.get_or_init(build_tables)
}

/// Constructs the exp/log tables directly from the field definition.
///
/// Doubling (`a << 1`) is multiplication by x; when the x^8 bit is set we
/// reduce by XOR-ing the modulus. The generator table is built by repeatedly
/// multiplying with [`mul_slow`] starting from `g = 3`, which enumerates the
/// whole multiplicative group (validated in tests).
fn build_tables() -> Tables {
    let mut exp = [0u8; 256];
    let mut log = [0u8; 256];

    let mut x: u8 = 1;
    for i in 0..255 {
        exp[i] = x;
        // log[0] stays 0 and is never meaningful.
        if x != 0 {
            log[x as usize] = i as u8;
        }
        x = mul_slow(x, GENERATOR);
    }
    // After 255 multiplications we must be back at the identity.
    debug_assert_eq!(x, 1, "g=3 is not a generator of GF(2^8) mod 0x11B");
    exp[255] = exp[0]; // convenience for wrap-around indexing

    Tables { exp, log }
}

/// One multiplication by x (the "carry-less shift and reduce" step).
#[inline]
fn mul_step(a: u8) -> u8 {
    let hi = a & 0x80;
    let shifted = (a << 1) as u16;
    let reduce = if hi != 0 { MODULUS ^ 0x100 } else { 0 };
    (shifted ^ reduce) as u8
}

/// Multiplication by `x` (the generator), exposed for inspection/testing.
#[inline]
pub fn mul_by_x(a: u8) -> u8 {
    mul_step(a)
}

/// Table-free textbook field multiplication: shift-and-XOR polynomial
/// multiplication with explicit modulo reduction. Intentionally slow — use
/// [`mul`] in hot paths; this function exists so the table path can be
/// independently cross-checked.
pub fn mul_slow(a: u8, b: u8) -> u8 {
    let mut result: u16 = 0;
    let mut cur = a as u16; // cur = a * x^i (unreduced accumulator, < 0x1FF)
    let mut b = b;
    for _ in 0..8 {
        if b & 1 != 0 {
            result ^= cur;
        }
        b >>= 1;
        // Multiply cur by x and reduce immediately so it always fits in 9 bits.
        cur <<= 1;
        if cur & 0x100 != 0 {
            cur ^= MODULUS;
        }
        cur &= 0xFF;
    }
    result as u8
}

/// Field multiplication using exp/log tables. `0 * x = x * 0 = 0`.
#[inline]
pub fn mul(a: u8, b: u8) -> u8 {
    if a == 0 || b == 0 {
        return 0;
    }
    let t = tables();
    let la = t.log[a as usize] as u16;
    let lb = t.log[b as usize] as u16;
    t.exp[(la + lb) as usize % 255]
}

/// Field additive inverse; in characteristic 2 this is the value itself.
#[inline]
pub fn neg(a: u8) -> u8 {
    a
}

/// Field addition = XOR.
#[inline]
pub fn add(a: u8, b: u8) -> u8 {
    a ^ b
}

/// Field division. Panics if `b == 0` (division by zero is undefined);
/// callers in matrix code only invert pivots that have been checked non-zero.
#[inline]
pub fn div(a: u8, b: u8) -> u8 {
    assert!(b != 0, "division by zero in GF(2^8)");
    mul(a, inv(b))
}

/// Multiplicative inverse. `inv(0)` is undefined and panics.
#[inline]
pub fn inv(a: u8) -> u8 {
    assert!(a != 0, "inverse of zero is undefined in GF(2^8)");
    let t = tables();
    // a = g^e  =>  a^-1 = g^(255-e)  (group order 255).
    t.exp[255 - t.log[a as usize] as usize]
}

/// Raise `a` to the power `n` by repeated multiplication (small helper used
/// in tests and matrix construction).
pub fn pow(mut a: u8, mut n: u32) -> u8 {
    let mut acc = 1u8;
    while n > 0 {
        if n & 1 != 0 {
            acc = mul(acc, a);
        }
        a = mul(a, a);
        n >>= 1;
    }
    acc
}

/// Forces lazy table construction now (handy in tests / startup checks).
pub fn prewarm() {
    let _ = tables();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generator_enumerates_full_group() {
        let t = tables();
        let mut seen = [false; 256];
        for e in 0..255usize {
            let a = t.exp[e];
            assert!(!seen[a as usize], "element {a} repeated at exponent {e}");
            seen[a as usize] = true;
            assert_eq!(t.log[a as usize], e as u8);
        }
        assert!(!seen[0]);
        assert_eq!(seen[1..].iter().filter(|&&v| v).count(), 255);
    }

    #[test]
    fn tables_match_textbook_mul_for_all_pairs() {
        // The O(1) table path must agree with the table-free definition for
        // every one of the 65,536 input pairs.
        for a in 0..=255u16 {
            for b in 0..=255u16 {
                let (a, b) = (a as u8, b as u8);
                assert_eq!(mul(a, b), mul_slow(a, b), "mul({a}, {b}) mismatch");
            }
        }
    }

    #[test]
    fn field_axioms() {
        for a in 0..=255u16 {
            let a = a as u8;
            assert_eq!(mul(a, 1), a);
            assert_eq!(mul(a, 0), 0);
            assert_eq!(add(a, 0), a);
            assert_eq!(add(a, a), 0); // char 2: self-inverse
            if a != 0 {
                assert_eq!(mul(a, inv(a)), 1);
                assert_eq!(div(a, a), 1);
                // Fermat: a^255 = 1 in the multiplicative group.
                assert_eq!(pow(a, 255), 1);
            }
        }
    }

    #[test]
    fn known_answers() {
        // A few hand-checkable values over GF(2^8) mod 0x11B (AES field):
        // 0x57 * 0x83 = 0xC1 (the canonical AES Rijndael example), and the
        // shift-reduce chain 1,2,4,8,16,32,64,128,0x1B,...
        assert_eq!(mul_slow(0x57, 0x83), 0xC1);
        assert_eq!(mul(0x57, 0x83), 0xC1);
        assert_eq!(mul_by_x(128), 0x1B);
        assert_eq!(inv(0x53), 0xCA); // 0x53 * 0xCA = 1 mod 0x11B (AES S-box example)
    }
}
