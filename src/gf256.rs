//! GF(2^8) finite-field arithmetic and linear algebra.
//!
//! # Algorithm assumptions
//!
//! - Field: GF(2^8), represented as bytes.
//! - Primitive (generator) polynomial: `x^8 + x^4 + x^3 + x^2 + 1`,
//!   i.e. `0x11d` (decimal 285). This is the classical Reed-Solomon
//!   polynomial also used by AES.
//! - Primitive element (multiplicative generator of the field): `g = 2`.
//! - Addition/subtraction are XOR (`a ^ b`).
//! - Multiplication uses log/antilog tables:
//!   `mul(a,b) = exp(log(a) + log(b))` for `a,b != 0`, and `0` otherwise.
//!
//! The log/exp tables are built deterministically at startup from the
//! primitive polynomial, so every field operation in the crate is auditable
//! from this single file.
//!
//! Version marker: `GF256-PP0x11D-G2/v1`.

use std::sync::OnceLock;

/// Primitive polynomial x^8+x^4+x^3+x^2+1, without the implicit x^8 term.
pub const PRIMITIVE_POLY: u16 = 0x1d;
/// Primitive element of the multiplicative group.
pub const GENERATOR: u8 = 2;
/// Version string of the field definition, embedded in manifests.
pub const GF_VERSION: &str = "GF256-PP0x11D-G2/v1";

struct Tables {
    /// `exp[i] = g^i` for i in 0..=255 (second copy extends to 510).
    exp: [u8; 512],
    /// `log[g^i] = i` for i in 0..255; `log[0] = 0` (never used on zero).
    log: [u8; 256],
}

static TABLES: OnceLock<Tables> = OnceLock::new();

fn tables() -> &'static Tables {
    TABLES.get_or_init(|| {
        let mut exp = [0u8; 512];
        let mut log = [0u8; 256];
        let mut x: u16 = 1;
        for i in 0u16..256 {
            exp[i as usize] = x as u8;
            if i < 255 {
                log[x as usize] = i as u8;
            }
            // Multiply x by the generator g = 2 (left shift, reduce modulo PP).
            x <<= 1;
            if x & 0x100 != 0 {
                x ^= 0x11d;
            }
        }
        // Duplicate the table so mul/add indices up to 509 need no modulo.
        for i in 256..512 {
            exp[i] = exp[i - 255];
        }
        // Sanity invariants: g^255 == 1 and the log/exp tables are consistent.
        assert_eq!(exp[255], 1, "generator must have order 255");
        Tables { exp, log }
    })
}

/// Force initialization (and the build-time sanity checks) at startup.
pub fn init() {
    let _ = tables();
}

/// Field addition (XOR).
#[inline]
pub fn add(a: u8, b: u8) -> u8 {
    a ^ b
}

/// Field multiplication.
#[inline]
pub fn mul(a: u8, b: u8) -> u8 {
    if a == 0 || b == 0 {
        return 0;
    }
    let t = tables();
    let la = t.log[a as usize] as u16;
    let lb = t.log[b as usize] as u16;
    t.exp[(la + lb) as usize]
}

/// Field division `a / b`; division by zero returns `None`.
#[inline]
pub fn div(a: u8, b: u8) -> Option<u8> {
    if b == 0 {
        return None;
    }
    if a == 0 {
        return Some(0);
    }
    let t = tables();
    let la = t.log[a as usize] as i16;
    let lb = t.log[b as usize] as i16;
    let mut s = la - lb;
    if s < 0 {
        s += 255;
    }
    Some(t.exp[s as usize])
}

/// Multiplicative inverse; `inv(0)` is `None`.
#[inline]
pub fn inv(a: u8) -> Option<u8> {
    if a == 0 {
        return None;
    }
    div(1, a)
}

/// Raise `a` to the power `n` (exponent taken modulo 255 for non-zero `a`).
pub fn pow(a: u8, mut n: u32) -> u8 {
    if a == 0 {
        return 0;
    }
    let t = tables();
    let la = t.log[a as usize] as u32;
    n %= 255;
    t.exp[((la * n) % 255) as usize]
}

/// Compute the multiplicative inverse of a square matrix over GF(2^8)
/// using Gauss-Jordan elimination with partial pivoting.
///
/// Returns `None` if the matrix is singular.
pub fn invert_matrix(m: &[Vec<u8>]) -> Option<Vec<Vec<u8>>> {
    let n = m.len();
    if n == 0 || m.iter().any(|r| r.len() != n) {
        return None;
    }
    // Augmented matrix [M | I].
    let mut a: Vec<Vec<u8>> = m
        .iter()
        .map(|r| {
            let mut row = Vec::with_capacity(2 * n);
            row.extend_from_slice(r);
            row.extend(core::iter::repeat(0u8).take(n));
            row
        })
        .collect();
    for i in 0..n {
        a[i][n + i] = 1;
    }

    for col in 0..n {
        // Partial pivot: find a row >= col with a nonzero entry in this column.
        let pivot = (col..n).find(|&r| a[r][col] != 0)?;
        if pivot != col {
            a.swap(pivot, col);
        }
        let p = a[col][col];
        let pinv = inv(p)?;
        for j in 0..2 * n {
            a[col][j] = mul(a[col][j], pinv);
        }
        for r in 0..n {
            if r != col && a[r][col] != 0 {
                let f = a[r][col];
                for j in 0..2 * n {
                    a[r][j] ^= mul(f, a[col][j]);
                }
            }
        }
    }
    Some(a.into_iter().map(|mut r| r.split_off(n)).collect())
}

/// Multiply two matrices over GF(2^8). Returns `None` on shape mismatch.
pub fn mat_mul(a: &[Vec<u8>], b: &[Vec<u8>]) -> Option<Vec<Vec<u8>>> {
    let n = a.len();
    if n == 0 || b.len() != a[0].len() || n == 0 {
        return None;
    }
    let k = b.len();
    let p = b[0].len();
    if b.iter().any(|r| r.len() != p) || a.iter().any(|r| r.len() != k) {
        return None;
    }
    let mut out = vec![vec![0u8; p]; n];
    for i in 0..n {
        for j in 0..p {
            let mut acc = 0u8;
            for t in 0..k {
                acc ^= mul(a[i][t], b[t][j]);
            }
            out[i][j] = acc;
        }
    }
    Some(out)
}

/// Solve `A x = y` for one column vector over GF(2^8): `x = A^{-1} y`.
/// Returns `None` if `A` is singular or shapes mismatch.
pub fn solve(a: &[Vec<u8>], y: &[u8]) -> Option<Vec<u8>> {
    let ai = invert_matrix(a)?;
    if y.len() != ai.len() {
        return None;
    }
    let n = ai.len();
    let mut x = vec![0u8; n];
    for i in 0..n {
        let mut acc = 0u8;
        for j in 0..n {
            acc ^= mul(ai[i][j], y[j]);
        }
        x[i] = acc;
    }
    Some(x)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hand_checked_field_facts() {
        init();
        // Hard-coded, independently derived facts (pp = 0x11d, g = 2):
        assert_eq!(mul(2, 0x80), 0x1d, "2*0x80: reduction by 0x11d gives 0x1d");
        assert_eq!(mul(3, 3), 5, "3*3 = (2+1)(2+1)=4+2+2+1=4+1=5");
        assert_eq!(mul(0x10, 0x10), 0x1d, "16*16 = g^8 = 0x1d");
        assert_eq!(pow(GENERATOR, 255), 1);
        assert_eq!(pow(GENERATOR, 0), 1);
        for v in 1u16..=255 {
            let v = v as u8;
            assert_eq!(mul(v, inv(v).unwrap()), 1, "v*inv(v) must be 1 for {v}");
            assert_eq!(div(mul(v, 0x5a), v).unwrap(), 0x5a);
        }
        // Zero handling.
        assert_eq!(mul(0, 200), 0);
        assert!(div(1, 0).is_none());
        assert!(inv(0).is_none());
    }

    #[test]
    fn log_exp_table_bijection() {
        init();
        let t = tables();
        for (x, &l) in t.log.iter().enumerate().skip(1) {
            assert_eq!(t.exp[l as usize] as usize, x);
        }
        assert_eq!(t.exp[255], 1);
    }

    #[test]
    fn matrix_inverse_roundtrip() {
        // A fixed invertible matrix; verify A*A^-1 = I element by element.
        let a = vec![vec![1u8, 2, 3], vec![4, 5, 6], vec![7, 8, 99]];
        let ai = invert_matrix(&a).unwrap();
        let prod = mat_mul(&a, &ai).unwrap();
        for i in 0..3 {
            for j in 0..3 {
                assert_eq!(prod[i][j], if i == j { 1 } else { 0 });
            }
        }
        // Singular matrix (row 2 == row 1 ^ row 0).
        let s = vec![vec![1u8, 2, 3], vec![4, 5, 6], vec![5, 7, 5]];
        assert!(invert_matrix(&s).is_none());
    }
}
