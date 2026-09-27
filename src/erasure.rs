//! Reed-Solomon erasure-coding kernel over GF(2^8).
//!
//! Layout (fixed by this implementation, recorded in every manifest):
//!
//! - `k` data shards (indices `0..k`) and `m` parity shards (indices `k..k+m`).
//! - Input bytes are zero-padded up to `k * shard_len`; the *original*
//!   (unpadded) length is part of the authenticated manifest, never inferred
//!   from the padded shards.
//!
//! Encoding matrix:
//!
//! Start from a Vandermonde matrix `V[i][j] = alpha_i^j` with
//! `alpha_i = i + 1` (field elements 1..=k+m). Any selection of `k` rows of a
//! Vandermonde matrix with distinct nonzero evaluation points forms an
//! invertible matrix over GF(2^8) (its determinant is the product of
//! pairwise differences times 1).
//!
//! To obtain the *systematic* form (first `k` rows equal to the identity, so
//! data shards are the raw input slices), the first `k` rows of `V` are row-
//! reduced to `I`. The same elementary operations are applied to the parity
//! rows via the augmentation trick: `C = P * A^-1` where `A = V[0..k]` and
//! `P = V[k..]`. Then:
//!
//! ```text
//! [ I ] D = D_0..D_k        (data shards)
//! [ C ] D = parity shards
//! ```

use crate::gf256::{self as gf, invert_matrix, mat_mul, pow};

/// Errors raised by the coding kernel. The `code` strings are part of the
/// stable, explainable API surface.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CodeError {
    /// k or m outside `1..=256`, or `k + m > 256` (only 255 nonzero alphas).
    BadParams { reason: String },
    /// Supplied shard indices are not `k` distinct values in `0..k+m`.
    DuplicateOrInvalidIndex { index: usize },
    /// Fewer than `k` usable shards supplied for reconstruction.
    NotEnoughShards { have: usize, need: usize },
    /// The chosen rows happen to form a singular system (cannot occur for a
    /// Vandermonde matrix with distinct alphas; kept for defensiveness).
    SingularSystem,
    /// Shard lengths differ or do not match the expected length.
    ShardLengthMismatch { expected: usize, got: usize },
}

impl CodeError {
    pub fn code(&self) -> &'static str {
        match self {
            CodeError::BadParams { .. } => "BAD_PARAMS",
            CodeError::DuplicateOrInvalidIndex { .. } => "DUPLICATE_OR_INVALID_INDEX",
            CodeError::NotEnoughShards { .. } => "NOT_ENOUGH_SHARDS",
            CodeError::SingularSystem => "SINGULAR_SYSTEM",
            CodeError::ShardLengthMismatch { .. } => "SHARD_LENGTH_MISMATCH",
        }
    }
}

impl std::fmt::Display for CodeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CodeError::BadParams { reason } => write!(f, "bad coding parameters: {reason}"),
            CodeError::DuplicateOrInvalidIndex { index } => {
                write!(f, "duplicate or out-of-range shard index: {index}")
            }
            CodeError::NotEnoughShards { have, need } => {
                write!(f, "not enough shards: have {have}, need {need}")
            }
            CodeError::SingularSystem => write!(f, "singular reconstruction system"),
            CodeError::ShardLengthMismatch { expected, got } => {
                write!(f, "shard length mismatch: expected {expected}, got {got}")
            }
        }
    }
}

impl std::error::Error for CodeError {}

/// Validate `(k, m)` and the total number of shards.
pub fn validate_params(k: u8, m: u8) -> Result<(), CodeError> {
    if k == 0 {
        return Err(CodeError::BadParams {
            reason: "k must be >= 1".into(),
        });
    }
    if m == 0 {
        return Err(CodeError::BadParams {
            reason: "m must be >= 1".into(),
        });
    }
    if (k as u32) + (m as u32) > 255 {
        return Err(CodeError::BadParams {
            reason: "k + m must be <= 255 (Vandermonde needs k+m distinct nonzero alphas)"
                .into(),
        });
    }
    Ok(())
}

/// Returns true if a square GF(256) matrix is invertible (test/audit helper).
pub fn gf_invertible(a: &[Vec<u8>]) -> bool {
    invert_matrix(a).is_some()
}

/// Build the Vandermonde matrix `V[i][j] = (i+1)^j`, shape `(k+m) x k`.
pub fn vandermonde(k: u8, m: u8) -> Result<Vec<Vec<u8>>, CodeError> {
    validate_params(k, m)?;
    let n = (k + m) as usize;
    let kk = k as usize;
    let mut v = Vec::with_capacity(n);
    for i in 0..n {
        let alpha = (i + 1) as u8; // 1..=k+m, never zero
        let row: Vec<u8> = (0..kk).map(|j| pow(alpha, j as u32)).collect();
        v.push(row);
    }
    Ok(v)
}

/// Build the full systematic encoding matrix `[ I ; C ]`, shape `(k+m) x k`.
///
/// `C = P * A^-1`, with `A` the first `k` rows and `P` the last `m` rows of
/// the Vandermonde matrix.
pub fn encoding_matrix(k: u8, m: u8) -> Result<Vec<Vec<u8>>, CodeError> {
    let v = vandermonde(k, m)?;
    let kk = k as usize;
    let mm = m as usize;
    let a: Vec<Vec<u8>> = v[0..kk].to_vec();
    let p: Vec<Vec<u8>> = v[kk..kk + mm].to_vec();
    let a_inv = invert_matrix(&a).ok_or(CodeError::SingularSystem)?;
    let c = mat_mul(&p, &a_inv).ok_or(CodeError::SingularSystem)?;

    let mut em = Vec::with_capacity(kk + mm);
    for i in 0..kk {
        let mut row = vec![0u8; kk];
        row[i] = 1;
        em.push(row);
    }
    em.extend(c);
    Ok(em)
}

/// Result of encoding: ordered shards `0..k+m`, each `shard_len` bytes.
pub struct Encoded {
    pub shards: Vec<Vec<u8>>,
    pub shard_len: usize,
}

/// Zero-pad `data` and encode into `k + m` shards.
pub fn encode(data: &[u8], k: u8, m: u8) -> Result<Encoded, CodeError> {
    validate_params(k, m)?;
    let kk = k as usize;
    let shard_len = data.len().div_ceil(kk);
    let total = shard_len * kk;
    let mut padded = Vec::with_capacity(total);
    padded.extend_from_slice(data);
    padded.resize(total, 0u8); // zero padding; original length stored in manifest

    let em = encoding_matrix(k, m)?;
    let mut shards: Vec<Vec<u8>> = (0..kk + m as usize)
        .map(|_| vec![0u8; shard_len])
        .collect();

    // data shards: identity rows -> plain copies
    for i in 0..kk {
        shards[i].copy_from_slice(&padded[i * shard_len..(i + 1) * shard_len]);
    }
    // parity shards: C row dot data columns, per byte position
    for (idx, row) in em.iter().enumerate().skip(kk) {
        for t in 0..shard_len {
            let mut acc = 0u8;
            for j in 0..kk {
                let dbyte = shards[j][t];
                acc ^= gf::mul(row[j], dbyte);
            }
            shards[idx][t] = acc;
        }
    }
    Ok(Encoded { shards, shard_len })
}

/// Reconstruct *all* `k + m` shards from any `k` available shards.
///
/// Input: pairs `(shard_index, shard_bytes)` for the shards that survived
/// verification. Shards that failed their integrity check MUST already have
/// been removed by the caller — this kernel treats every supplied shard as
/// trusted and its index as an erasure position.
///
/// Returns the complete shard vector in canonical index order. It refuses
/// (`NotEnoughShards`) rather than guessing when fewer than `k` shards exist.
pub fn reconstruct(
    available: &[(u8, Vec<u8>)],
    k: u8,
    m: u8,
    shard_len: usize,
) -> Result<Vec<Vec<u8>>, CodeError> {
    validate_params(k, m)?;
    let kk = k as usize;
    let total = kk + m as usize;

    if available.len() < kk {
        return Err(CodeError::NotEnoughShards {
            have: available.len(),
            need: kk,
        });
    }

    // Validate indices: in range, distinct.
    let mut seen = vec![false; total];
    for &(idx, ref bytes) in available {
        let i = idx as usize;
        if i >= total {
            return Err(CodeError::DuplicateOrInvalidIndex { index: i });
        }
        if seen[i] {
            return Err(CodeError::DuplicateOrInvalidIndex { index: i });
        }
        if bytes.len() != shard_len {
            return Err(CodeError::ShardLengthMismatch {
                expected: shard_len,
                got: bytes.len(),
            });
        }
        seen[i] = true;
    }

    // Pick exactly k shards, preferring data shards (identity rows make the
    // direct path trivial) but any k rows work.
    let mut chosen: Vec<(u8, Vec<u8>)> = available
        .iter()
        .filter(|(idx, _)| (*idx as usize) < kk)
        .take(kk)
        .cloned()
        .collect::<Vec<_>>();
    if chosen.len() < kk {
        for pair in available.iter() {
            if (pair.0 as usize) >= kk {
                chosen.push(pair.clone());
                if chosen.len() == kk {
                    break;
                }
            }
        }
    }
    debug_assert_eq!(chosen.len(), kk);

    let em = encoding_matrix(k, m)?;
    let indices: Vec<usize> = chosen.iter().map(|(idx, _)| *idx as usize).collect();
    let a: Vec<Vec<u8>> = indices.iter().map(|&i| em[i].clone()).collect();
    let a_inv = invert_matrix(&a).ok_or(CodeError::SingularSystem)?;

    // Recover the k data shards: D = A^-1 * Y, per byte column.
    let mut data_shards: Vec<Vec<u8>> = (0..kk).map(|_| vec![0u8; shard_len]).collect();
    for t in 0..shard_len {
        let y: Vec<u8> = chosen.iter().map(|(_, b)| b[t]).collect();
        for i in 0..kk {
            let mut acc = 0u8;
            for j in 0..kk {
                acc ^= gf::mul(a_inv[i][j], y[j]);
            }
            data_shards[i][t] = acc;
        }
    }

    // Rebuild the complete shard set from recovered data.
    let mut out: Vec<Vec<u8>> = (0..total).map(|_| vec![0u8; shard_len]).collect();
    for j in 0..kk {
        out[j].copy_from_slice(&data_shards[j]);
    }
    for i in kk..total {
        for t in 0..shard_len {
            let mut acc = 0u8;
            for j in 0..kk {
                acc ^= gf::mul(em[i][j], data_shards[j][t]);
            }
            out[i][t] = acc;
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn k2_m1_hand_derived_coefficients() {
        // V for (k=2,m=1): rows [1,1],[1,2],[1,3].
        // A=[[1,1],[1,2]]; A^-1:
        //   det = 1*2 ^ 1*1 = 3 ; inv(3) = 0xf6 ... use solve: verify [I;C].
        // Systematic matrix first two rows are identity; parity C satisfies
        // C * A = P_row, i.e. C = [1,3] * A^-1. Algebraically, and verified
        // via the kernel's own construction cross-checked with byte algebra:
        let em = encoding_matrix(2, 1).unwrap();
        assert_eq!(&em[0], &[1, 0]);
        assert_eq!(&em[1], &[0, 1]);
        // Parity row computed through gf operations:
        // A^-1 rows: solve columns of I.
        // [c0,c1] such that c0*1^c1*1=1 and c0*1^c1*2=3
        //   -> c0^c1=1 ; c0^2c1=3 -> subtract: (2^1)c1 = 1^3=2
        //      3*c1 = 2 -> c1 = 2/3 ; c0 = 1 ^ c1
        let c1 = gf::div(2, 3).unwrap();
        let c0 = 1 ^ c1;
        assert_eq!(&em[2], &[c0, c1]);
        // spot check c0/c1 are nonzero and distinct
        assert_ne!(c0, 0);
        assert_ne!(c1, 0);
        assert_ne!(c0, c1);
    }

    #[test]
    fn roundtrip_small() {
        let data = b"Hello erasure coding over GF(256)!".to_vec();
        let enc = encode(&data, 3, 2).unwrap();
        assert_eq!(enc.shards.len(), 5);
        assert_eq!(enc.shard_len * 3, data.len().next_multiple_of(3));
        // Drop any 2 (=m) shards: recover and compare.
        for missing in 0u8..5 {
            for missing2 in (missing + 1)..5 {
                let avail: Vec<(u8, Vec<u8>)> = enc
                    .shards
                    .iter()
                    .enumerate()
                    .filter(|(i, _)| *i as u8 != missing && *i as u8 != missing2)
                    .map(|(i, s)| (i as u8, s.clone()))
                    .collect();
                let rec = reconstruct(&avail, 3, 2, enc.shard_len).unwrap();
                assert_eq!(rec, enc.shards, "missing {missing},{missing2}");
            }
        }
    }

    #[test]
    fn empty_input_is_supported() {
        // Empty object: shard_len 0; reconstruction must still work and yield
        // zero-length data after truncation (done by the service layer).
        let enc = encode(&[], 2, 1).unwrap();
        assert_eq!(enc.shard_len, 0);
        let avail = vec![(0u8, vec![]), (2u8, vec![])];
        let rec = reconstruct(&avail, 2, 1, 0).unwrap();
        assert_eq!(rec.len(), 3);
        assert!(rec.iter().all(|s| s.is_empty()));
    }

    #[test]
    fn refuses_below_k_and_duplicates() {
        let enc = encode(b"abcdef", 3, 2).unwrap();
        let only_two: Vec<(u8, Vec<u8>)> = vec![
            (0, enc.shards[0].clone()),
            (1, enc.shards[1].clone()),
        ];
        assert_eq!(
            reconstruct(&only_two, 3, 2, enc.shard_len).unwrap_err(),
            CodeError::NotEnoughShards { have: 2, need: 3 }
        );
        let dup = vec![
            (0, enc.shards[0].clone()),
            (0, enc.shards[0].clone()),
            (1, enc.shards[1].clone()),
        ];
        assert!(matches!(
            reconstruct(&dup, 3, 2, enc.shard_len).unwrap_err(),
            CodeError::DuplicateOrInvalidIndex { index: 0 }
        ));
        let oob = vec![
            (5, enc.shards[0].clone()),
            (0, enc.shards[0].clone()),
            (1, enc.shards[1].clone()),
        ];
        assert!(matches!(
            reconstruct(&oob, 3, 2, enc.shard_len).unwrap_err(),
            CodeError::DuplicateOrInvalidIndex { index: 5 }
        ));
    }
}
