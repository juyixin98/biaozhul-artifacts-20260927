//! GF(2^8) coding matrices.
//!
//! # Coding matrix (fixed layout)
//!
//! The `n = k + m` by `k` coding matrix is systematic:
//!
//! ```text
//!        ┌──────────────┐
//!        │  I_k (k×k)   │  data shards d_0..d_{k-1}
//!    A = │──────────────│
//!        │  C   (m×k)   │  parity shards p_0..p_{m-1}
//!        └──────────────┘
//! ```
//!
//! `C` is a **Cauchy matrix**:
//!
//! ```text
//!   C[p][j] = 1 / (x_p − y_j)   in GF(2^8),
//!   y_j     = j            (data points: elements 0..k-1)
//!   x_p     = k + p        (parity points: elements k..k+m-1)
//! ```
//!
//! Subtraction in GF(2^8) is XOR. All `k + m` points are distinct (that is
//! exactly why [`crate::config::CodecConfig`] requires `k + m <= 255`) and no
//! `x_p` equals any `y_j`, so every denominator is non-zero.
//!
//! # Why every selection of k rows is invertible (MDS argument)
//!
//! Take any k shard rows: `s` parity rows plus `k-s` identity rows. Order
//! columns so the identity-row columns come first; the selected matrix has
//! block form
//!
//! ```text
//!   ┌ I_{k-s} │ 0 ┐
//!   ├─────────┼───┤
//!   │ C_a     │ C_b│
//!   └─────────┴───┘
//! ```
//!
//! Its determinant is the determinant of `C_b`, and `C_b` is a square
//! sub-matrix of a Cauchy matrix: its `x` points (parity points) and `y`
//! points (data points) are each distinct and mutually disjoint. The classic
//! Cauchy determinant formula is non-zero in a field, hence `C_b` is
//! invertible. This is exhaustively re-verified empirically in
//! `tests/matrix_test.rs` for the small configs used by the service.
//!
//! Reconstruction solves `A_sel · D = S` for the data matrix `D` with
//! Gauss–Jordan elimination over GF(2^8).

use ec_gf as gf;

use crate::error::{EcError, EcResult};

/// Field element assigned to data column `j` (`0..=254`).
#[inline]
pub fn data_point(j: usize) -> u8 {
    j as u8
}

/// Field element assigned to parity row `p` for a config with `k` data shards.
#[inline]
pub fn parity_point(k: usize, p: usize) -> u8 {
    (k + p) as u8
}

/// One Cauchy entry `1 / (x XOR y)` (subtraction is XOR in GF(2^8)).
///
/// Requires `x != y`; the point assignment in this module guarantees that for
/// every legal `(p, j)`.
#[inline]
pub fn cauchy_entry(x: u8, y: u8) -> u8 {
    debug_assert_ne!(x, y, "Cauchy denominator zero: x={x} y={y}");
    gf::inv(gf::add(x, y))
}

/// Full parity row `p` of the Cauchy block for a config with `k` data shards.
pub fn cauchy_parity_row(k: usize, p: usize) -> Vec<u8> {
    let x = parity_point(k, p);
    (0..k).map(|j| cauchy_entry(x, data_point(j))).collect()
}

/// Build the `k × k` matrix formed by the coding rows of the given shard
/// indices (in the given order). With k distinct, in-range indices this is
/// guaranteed invertible by the MDS argument above; callers learn that with
/// certainty when [`gauss_jordan_solve`] succeeds.
pub fn selection_matrix(k: usize, indices: &[usize]) -> EcResult<Vec<Vec<u8>>> {
    if indices.len() != k {
        return Err(EcError::NotReconstructable(format!(
            "selection_matrix needs exactly {k} rows, got {}",
            indices.len()
        )));
    }
    Ok(indices
        .iter()
        .map(|&idx| coding_row(k, idx))
        .collect())
}

/// Coding row for shard `idx`: identity (data) or Cauchy (parity).
pub fn coding_row(k: usize, idx: usize) -> Vec<u8> {
    if idx < k {
        let mut row = vec![0u8; k];
        row[idx] = 1;
        row
    } else {
        cauchy_parity_row(k, idx - k)
    }
}

/// Solve `A · X = B` over GF(2^8) by Gauss–Jordan elimination.
///
/// - `matrix` is the `k × k` selected coding matrix;
/// - `rhs` is `k` right-hand-side vectors of identical length `L` (the shard
///   bytes); each vector corresponds to one matrix row;
/// - returns the `k` solution vectors of length `L` (i.e. the data shards).
///
/// The elimination touches the RHS via exactly the same row operations as the
/// coefficient matrix, which is the standard augmented-matrix method. A zero
/// pivot means a singular matrix and yields [`EcError::NotReconstructable`]
/// rather than any partial/guessed result.
pub fn gauss_jordan_solve(
    matrix: &[Vec<u8>],
    rhs: &[Vec<u8>],
) -> EcResult<Vec<Vec<u8>>> {
    let k = matrix.len();
    if k == 0 || matrix.iter().any(|r| r.len() != k) || rhs.len() != k {
        return Err(EcError::Internal(
            "gauss_jordan_solve: malved matrix/rhs shapes".into(),
        ));
    }
    let shard_len = rhs[0].len();
    if rhs.iter().any(|v| v.len() != shard_len) {
        return Err(EcError::SizeMismatch {
            detail: "RHS shard vectors have differing lengths".into(),
        });
    }

    // Augmented rows: [k coefficient bytes | shard_len RHS bytes].
    let mut aug: Vec<Vec<u8>> = matrix
        .iter()
        .zip(rhs.iter())
        .map(|(a, b)| {
            let mut row = Vec::with_capacity(k + shard_len);
            row.extend_from_slice(a);
            row.extend_from_slice(b);
            row
        })
        .collect();

    for col in 0..k {
        // Partial pivot: any non-zero entry works in GF(2^8); choosing the
        // first keeps results deterministic.
        let pivot = (col..k).find(|&r| aug[r][col] != 0);
        let pivot = match pivot {
            Some(r) => r,
            None => {
                return Err(EcError::NotReconstructable(format!(
                    "singular selected coding matrix at column {col} (should not happen for distinct Cauchy/identity rows)"
                )));
            }
        };
        aug.swap(col, pivot);

        // Normalise pivot row so the pivot becomes 1.
        let inv = gf::inv(aug[col][col]);
        for cell in aug[col].iter_mut().skip(col) {
            *cell = gf::mul(*cell, inv);
        }

        // Eliminate column `col` from every other row.
        for r in 0..k {
            if r == col || aug[r][col] == 0 {
                continue;
            }
            let factor = aug[r][col];
            for c in col..k + shard_len {
                aug[r][c] ^= gf::mul(factor, aug[col][c]);
            }
        }
    }

    // Coefficient side is now I_k; the trailing slices are the data shards.
    Ok(aug.into_iter().map(|row| row[k..].to_vec()).collect())
}

/// Multiply one coding row by the ordered data shards to produce one shard:
/// `out[off] = XOR_j gf::mul(row[j], data[j][off])`.
pub fn apply_row(row: &[u8], data: &[Vec<u8>]) -> Vec<u8> {
    debug_assert_eq!(row.len(), data.len());
    let shard_len = data[0].len();
    debug_assert!(data.iter().all(|d| d.len() == shard_len));
    let mut out = vec![0u8; shard_len];
    for (j, &coef) in row.iter().enumerate() {
        if coef == 0 {
            continue;
        }
        for (off, &byte) in data[j].iter().enumerate() {
            out[off] ^= gf::mul(coef, byte);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_cauchy_entries_aes_field() {
        // k=3, parity row p=0 -> x = 3; y in {0,1,2}.
        // 1/3 = F6, 1/2 = 8D, 1/(3 XOR 2 = 1) = 1 in the AES field.
        let row = cauchy_parity_row(3, 0);
        assert_eq!(row, vec![0xF6, 0x8D, 0x01]);
    }

    #[test]
    fn points_are_distinct_and_denominators_nonzero() {
        for (k, m) in [(1, 1), (3, 2), (6, 3), (10, 4)] {
            for p in 0..m {
                for j in 0..k {
                    assert_ne!(parity_point(k, p), data_point(j));
                    assert_ne!(cauchy_entry(parity_point(k, p), data_point(j)), 0);
                }
            }
        }
    }

    #[test]
    fn every_k_row_selection_is_invertible() {
        // Exhaustive MDS check for small configs: solve A·X=I for each
        // C(n,k) selection; success proves invertibility.
        for (k, m) in [(2u8, 1u8), (3, 2), (4, 2), (5, 3)] {
            let (k, m) = (k as usize, m as usize);
            let n = k + m;
            let identity: Vec<Vec<u8>> = (0..k)
                .map(|i| {
                    let mut v = vec![0u8; k];
                    v[i] = 1;
                    v
                })
                .collect();
            for bits in 0u16..1u16 << n {
                if bits.count_ones() as usize != k {
                    continue;
                }
                let selection: Vec<usize> = (0..n).filter(|i| bits & (1 << i) != 0).collect();
                let mat = selection_matrix(k, &selection).unwrap();
                let solved = gauss_jordan_solve(&mat, &identity).unwrap();
                // Must actually be A^-1: A * A^-1 = I.
                for r in 0..k {
                    for c in 0..k {
                        let v = (0..k).fold(0u8, |acc, t| acc ^ gf::mul(mat[r][t], solved[t][c]));
                        assert_eq!(v, if r == c { 1 } else { 0 }, "k={k} selection={selection:?}");
                    }
                }
            }
        }
    }
}
