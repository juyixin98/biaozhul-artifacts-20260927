//! Encode, rebuild and recover at the shard level.
//!
//! This module never touches digests, manifests or persistence: callers give
//! it shards they have already classified as present-and-good, and it returns
//! computed shards. The only hard integrity guards it keeps itself are shape
//! checks, duplicate-index rejection and a mutual-consistency check when more
//! than `k` shards are supplied.

use crate::config::CodecConfig;
use crate::error::{EcError, EcResult};
use crate::matrix::{apply_row, coding_row, gauss_jordan_solve};

/// One shard with its position in the fixed layout (`0..k` data, `k..k+m`
/// parity).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Shard {
    pub index: u16,
    pub data: Vec<u8>,
}

impl Shard {
    pub fn new(index: u16, data: Vec<u8>) -> Self {
        Self { index, data }
    }
}

/// Result of encoding one object: every shard has exactly `shard_len` bytes;
/// `shards[i]` is shard `i` (`0..n`).
#[derive(Debug, Clone)]
pub struct EncodedBlock {
    pub shard_len: usize,
    pub shards: Vec<Vec<u8>>,
}

/// Outcome of a reconstruction: the recovered data shards plus a full audit
/// trail of which inputs were used.
#[derive(Debug, Clone)]
pub struct Reconstruction {
    /// Recovered data shards `d_0..d_{k-1}` (length `shard_len`, unpadded by
    /// the caller using the manifest's original length).
    pub data_shards: Vec<Vec<u8>>,
    /// Shard indices, sorted, that participated in the solve.
    pub used_indices: Vec<usize>,
    /// Indices, sorted, that were rebuilt from the solution (subset of
    /// requested targets).
    pub rebuilt_indices: Vec<usize>,
}

/// Zero-pad `data` up to a multiple of `k` and return `(shard_len, shards)`.
/// The padding byte count is `k*shard_len - data.len()`; the manifest carries
/// it as `pad_len` inside the checksummed field set, so truncation is
/// authenticated.
pub fn split_padded(cfg: &CodecConfig, data: &[u8]) -> (usize, Vec<Vec<u8>>) {
    let shard_len = data.len().div_ceil(cfg.k()).max(1);
    let total = shard_len * cfg.k();
    let mut padded = data.to_vec();
    padded.resize(total, 0u8);
    let shards = (0..cfg.k())
        .map(|j| padded[j * shard_len..(j + 1) * shard_len].to_vec())
        .collect();
    (shard_len, shards)
}

/// Encode an object: split/pad the data and derive the `m` parity shards with
/// the fixed Cauchy rows.
pub fn encode(cfg: &CodecConfig, data: &[u8]) -> EncodedBlock {
    let (shard_len, mut shards) = split_padded(cfg, data);
    for p in 0..cfg.m() {
        let row = coding_row(cfg.k(), cfg.k() + p);
        shards.push(apply_row(&row, &shards[..cfg.k()]));
    }
    debug_assert_eq!(shards.len(), cfg.n());
    debug_assert!(shards.iter().all(|s| s.len() == shard_len));
    EncodedBlock {
        shard_len,
        shards,
    }
}

/// Concatenate the k data shards and truncate to `original_len` bytes.
/// Refuses lengths that the manifest layout could not contain.
pub fn truncate_to_original(
    cfg: &CodecConfig,
    data_shards: &[Vec<u8>],
    shard_len: usize,
    original_len: u64,
) -> EcResult<Vec<u8>> {
    if data_shards.len() != cfg.k() {
        return Err(EcError::SizeMismatch {
            detail: format!("expected {} data shards, got {}", cfg.k(), data_shards.len()),
        });
    }
    if data_shards.iter().any(|s| s.len() != shard_len) {
        return Err(EcError::SizeMismatch {
            detail: format!("all data shards must be {shard_len} bytes"),
        });
    }
    let capacity = (cfg.k() * shard_len) as u64;
    if original_len > capacity {
        return Err(EcError::SizeMismatch {
            detail: format!(
                "manifest original_len={original_len} exceeds padded capacity {capacity}"
            ),
        });
    }
    let mut out = Vec::with_capacity(original_len as usize);
    for s in data_shards {
        out.extend_from_slice(s);
    }
    out.truncate(original_len as usize);
    Ok(out)
}

/// Validate, de-duplicate and normalise the caller-provided shard set.
///
/// Errors (categorised, never silent):
/// - out-of-range index → [`EcError::InvalidShardIndex`],
/// - repeated index → [`EcError::DuplicateShardIndex`],
/// - wrong shard byte length → [`EcError::SizeMismatch`].
fn normalise_inputs(
    cfg: &CodecConfig,
    shards: Vec<Shard>,
    shard_len: usize,
) -> EcResult<Vec<(usize, Vec<u8>)>> {
    let mut norm: Vec<(usize, Vec<u8>)> = Vec::with_capacity(shards.len());
    for shard in shards {
        cfg.check_index(shard.index)?;
        if shard.data.len() != shard_len {
            return Err(EcError::SizeMismatch {
                detail: format!(
                    "shard {} has {} bytes, manifest shard_len is {shard_len}",
                    shard.index,
                    shard.data.len()
                ),
            });
        }
        let idx = shard.index as usize;
        if norm.iter().any(|(i, _)| *i == idx) {
            return Err(EcError::DuplicateShardIndex(shard.index));
        }
        norm.push((idx, shard.data));
    }
    // Deterministic solve regardless of input ordering.
    norm.sort_by_key(|(i, _)| *i);
    Ok(norm)
}

/// Reconstruct the k data shards from available present-and-good shards.
///
/// Requires at least `k` shards; otherwise [`EcError::InsufficientShards`] is
/// returned and **nothing is fabricated**. When more than `k` shards exist,
/// the first `k` (lowest indices) solve the system and every extra shard must
/// byte-match the value predicted from the solution — a contradiction returns
/// [`EcError::NotReconstructable`] rather than a possibly-wrong answer.
pub fn reconstruct_data(
    cfg: &CodecConfig,
    shards: Vec<Shard>,
    shard_len: usize,
) -> EcResult<Reconstruction> {
    let norm = normalise_inputs(cfg, shards, shard_len)?;

    if norm.len() < cfg.k() {
        return Err(EcError::InsufficientShards {
            available: norm.len(),
            required: cfg.k(),
        });
    }

    let used: Vec<usize> = norm[..cfg.k()].iter().map(|(i, _)| *i).collect();
    let matrix: Vec<Vec<u8>> = used
        .iter()
        .map(|&i| coding_row(cfg.k(), i))
        .collect();
    let rhs: Vec<Vec<u8>> = norm[..cfg.k()].iter().map(|(_, d)| d.clone()).collect();

    let data_shards = gauss_jordan_solve(&matrix, &rhs)?;

    // Cross-check surplus shards against the solution.
    for (idx, bytes) in &norm[cfg.k()..] {
        let predicted = apply_row(&coding_row(cfg.k(), *idx), &data_shards);
        if &predicted != bytes {
            return Err(EcError::NotReconstructable(format!(
                "extra shard {idx} contradicts the shards used for solving; input set is mutually inconsistent"
            )));
        }
    }

    Ok(Reconstruction {
        data_shards,
        used_indices: used,
        rebuilt_indices: Vec::new(),
    })
}

/// Rebuild specific missing shards (any indices in `0..n`, data or parity)
/// from available present-and-good shards. Mirrors [`reconstruct_data`]'s
/// refusal rules. Output map is keyed by requested index.
pub fn rebuild_shards(
    cfg: &CodecConfig,
    shards: Vec<Shard>,
    shard_len: usize,
    targets: &[usize],
) -> EcResult<(Reconstruction, Vec<Shard>)> {
    for &t in targets {
        cfg.check_index(t as u16)?;
    }
    let mut targets_sorted: Vec<usize> = targets.to_vec();
    targets_sorted.sort_unstable();
    targets_sorted.dedup();

    let recon = reconstruct_data(cfg, shards, shard_len)?;

    let mut rebuilt = Vec::with_capacity(targets_sorted.len());
    for idx in targets_sorted {
        // A data shard is already in the solution; parity (or any) shard is
        // produced by applying its fixed coding row.
        let bytes = if idx < cfg.k() {
            recon.data_shards[idx].clone()
        } else {
            apply_row(&coding_row(cfg.k(), idx), &recon.data_shards)
        };
        rebuilt.push(Shard::new(idx as u16, bytes));
    }

    let mut recon = recon;
    recon.rebuilt_indices = rebuilt.iter().map(|s| s.index as usize).collect();
    Ok((recon, rebuilt))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn padding_round_trip_lengths() {
        let cfg = CodecConfig::new(3, 2).unwrap();
        for len in [0usize, 1, 2, 3, 4, 7, 30, 31] {
            let data = vec![0xA5u8; len];
            let enc = encode(&cfg, &data);
            assert_eq!(enc.shards.len(), 5);
            let recon = reconstruct_data(
                &cfg,
                enc.shards
                    .iter()
                    .enumerate()
                    .map(|(i, d)| Shard::new(i as u16, d.clone()))
                    .collect(),
                enc.shard_len,
            )
            .unwrap();
            let got = truncate_to_original(&cfg, &recon.data_shards, enc.shard_len, len as u64).unwrap();
            assert_eq!(got, data, "len={len}");
        }
    }

    #[test]
    fn rejects_duplicate_and_wrong_length_and_range() {
        let cfg = CodecConfig::new(2, 1).unwrap();
        let dup = vec![
            Shard::new(0, vec![0; 4]),
            Shard::new(0, vec![0; 4]),
        ];
        assert_eq!(
            reconstruct_data(&cfg, dup, 4).unwrap_err(),
            EcError::DuplicateShardIndex(0)
        );
        let bad_len = vec![Shard::new(0, vec![0; 3]), Shard::new(1, vec![0; 4])];
        assert!(matches!(
            reconstruct_data(&cfg, bad_len, 4).unwrap_err(),
            EcError::SizeMismatch { .. }
        ));
        let oob = vec![Shard::new(3, vec![0; 4]), Shard::new(0, vec![0; 4])];
        assert!(matches!(
            reconstruct_data(&cfg, oob, 4).unwrap_err(),
            EcError::InvalidShardIndex { .. }
        ));
    }

    #[test]
    fn insufficient_shards_is_explicit_and_no_output() {
        let cfg = CodecConfig::new(3, 2).unwrap();
        let enc = encode(&cfg, b"abcdefg");
        let err = reconstruct_data(
            &cfg,
            vec![Shard::new(0, enc.shards[0].clone()), Shard::new(4, enc.shards[4].clone())],
            enc.shard_len,
        )
        .unwrap_err();
        assert_eq!(
            err,
            EcError::InsufficientShards {
                available: 2,
                required: 3
            }
        );
    }
}
